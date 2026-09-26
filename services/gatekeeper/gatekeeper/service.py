"""処理ループ: 終了した person イベントごとに「誰か」を決めて記録する。

取りこぼし防止の考え方:
- 「カーソル」は処理し終えた位置だけを表し、そこから overlap_seconds だけ重ねて取り直す（重複は記録済み判定で除外）
- 進行中・保留・失敗したイベントは ID で控え（deferred）、毎周期 Frigate に個別に問い合わせる
- Frigate 側を変更する直前に「処理中」行を書き、途中で落ちても同じ ID で続きから再開する
"""
from __future__ import annotations

import io
import logging
import os
import re
import signal
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import risk as riskmod
from . import stats
from .attempts import UNKNOWN, Attempt, group_by_event
from .config import Settings
from .db import PENDING, Database
from .decide import Decision, decide

log = logging.getLogger(__name__)

TRAIN = "train"
PAGE = 100
MAX_PAGES = 20
EPS = 0.001
DEFAULT_DETECT = (1280, 720)


def measure_image(data: bytes) -> Tuple[int, int]:
    """画像の (幅, 高さ)。Pillow が無い・壊れている場合は (0, 0)。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (0, 0)


class Gatekeeper:
    def __init__(
        self,
        client,
        db: Database,
        cfg: Settings,
        measure: Callable[[bytes], Tuple[int, int]] = measure_image,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        facecheck=None,
        gender=None,
        notifier=None,
    ):
        self.client = client
        self.db = db
        self.cfg = cfg
        self.measure = measure
        self.now = now
        self.sleep = sleep
        # 登録前の顔品質チェック（None ならチェックなし）
        self.facecheck = facecheck
        # 性別・年齢の推定器（None なら推定しない）
        self.gender = gender
        # 通知先（None なら通知せず、判定と記録だけ）
        self.notifier = notifier
        self.faces_dir = os.path.join(cfg.data_dir, "faces")
        self.stop_requested = False
        # 処理ループと管理操作（merge / purge / clean）を同時に走らせないためのロック
        self.lock = threading.RLock()
        self._to_delete: List[str] = []
        self._detect_size: Optional[Tuple[int, int]] = None
        self._id_pattern = re.compile(rf"^{re.escape(cfg.id_prefix)}\d+$")

    # ------------------------------------------------------------------ loop
    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            log.info("停止要求 (%s)。処理中のイベントを終えてから止まります", signum)
            self.stop_requested = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    def run_forever(self) -> None:
        log.info(
            "gatekeeper 開始: frigate=%s camera=%s interval=%ss",
            self.cfg.frigate_url, self.cfg.camera, self.cfg.poll_interval,
        )
        failures = 0
        while not self.stop_requested:
            try:
                stats = self.process_once()
                if failures:
                    log.info("Frigate との通信が回復しました")
                failures = 0
                if stats["events"] or stats.get("errors"):
                    log.info("処理: %s", stats)
            except Exception as e:  # noqa: BLE001
                failures += 1
                if failures == 1:
                    log.exception("処理中にエラー。再試行します")
                elif failures in (5, 20) or failures % 100 == 0:
                    log.warning("%d 回連続で失敗中: %s", failures, e)
            delay = min(self.cfg.poll_interval * (2 ** min(failures, 4)), 120.0) if failures else self.cfg.poll_interval
            end = self.now() + delay
            while not self.stop_requested and self.now() < end:
                self.sleep(min(1.0, max(0.0, end - self.now())))
        log.info("gatekeeper 停止")

    # ------------------------------------------------------------------ pass
    def process_once(self) -> Dict[str, int]:
        with self.lock:
            return self._process_once()

    def _process_once(self) -> Dict[str, int]:
        now = self.now()
        faces = self.client.faces()
        for name in self._known(faces):
            self.db.ensure_person(name)
        by_event = group_by_event(faces.get(TRAIN, []))
        deferred: Dict[str, dict] = self.db.get_json("deferred", {})

        # 1) 進行中のイベントを先に控える（この後で終わっても ID で追える）
        for e in self.client.list_events(self.cfg.camera, in_progress=1, limit=PAGE):
            deferred.setdefault(e["id"], {"start": e["start_time"], "first": now, "tries": 0})

        # 2) 終了済みイベントをカーソル以降（重なり付き）で古い順に
        events = self._fetch_ended(now)

        # 3) 控えていたイベントの再確認
        for eid, info in list(deferred.items()):
            if eid in events:
                continue
            ev = self.client.get_event(eid)
            if ev is None:
                deferred.pop(eid)
                continue
            if ev.get("end_time") is None:
                if now - info["first"] > self.cfg.lookback_hours * 3600:
                    log.warning("event %s は %.0f 時間以上進行中のため追跡をやめます", eid, self.cfg.lookback_hours)
                    deferred.pop(eid)
                continue
            events[eid] = ev

        stats = {"events": 0, "identified": 0, "new": 0, "none": 0}
        self._to_delete = []
        processed_starts: List[float] = []

        for ev in sorted(events.values(), key=lambda e: e["start_time"]):
            if self.stop_requested:
                break
            eid = ev["id"]
            if self.db.has_visit(eid):
                deferred.pop(eid, None)
                processed_starts.append(ev["start_time"])
                continue
            attempts = by_event.pop(eid, [])
            info = deferred.get(eid) or {"start": ev["start_time"], "first": now, "tries": 0}
            try:
                d = self._process_event(ev, attempts, faces)
            except Exception as e:  # noqa: BLE001
                d = Decision("retry", reason=f"error: {e}")
                if info["tries"] == 0:
                    log.exception("event %s の処理に失敗", eid)
                else:
                    log.warning("event %s の処理に再失敗 (%d/%d): %s", eid, info["tries"] + 1, self.cfg.retry_limit, e)
                stats["errors"] = stats.get("errors", 0) + 1

            if d.method == "retry":
                info["tries"] += 1
                if info["tries"] <= self.cfg.retry_limit:
                    deferred[eid] = info
                    stats["retry"] = stats.get("retry", 0) + 1
                    continue
                d = self._give_up(ev, d.reason)

            deferred.pop(eid, None)
            processed_starts.append(ev["start_time"])
            stats["events"] += 1
            if d.is_new:
                stats["new"] += 1
            elif d.person_id:
                stats["identified"] += 1
            else:
                stats["none"] += 1

        self._cleanup_attempts(by_event)
        self._flush_deletes()
        self.db.set_json("deferred", deferred)
        if processed_starts:
            cursor = self.db.get_state("cursor")
            new_cursor = max(processed_starts)
            if cursor is None or new_cursor > float(cursor):
                self.db.set_state("cursor", repr(new_cursor))
        linked = self._link_appearance(now)
        if linked:
            stats["appearance"] = linked
        alerted = self._evaluate_alerts(now)
        if alerted:
            stats["alerts"] = alerted
        self._prune(faces, now)
        return stats

    # ----------------------------------------------------------------- alerts
    def _evaluate_alerts(self, now: float) -> int:
        """直近の来訪（進行中を含む）に危険度を付け、閾値以上なら通知する。"""
        cfg = self.cfg
        since = now - cfg.alert_window_minutes * 60
        rows = self.db.all_visits(since - cfg.visit_gap_seconds)
        # 進行中のイベントも滞在時間に含める
        try:
            active = self.client.list_events(cfg.camera, in_progress=1, limit=PAGE)
        except Exception:  # noqa: BLE001
            active = []
        if not rows and not active:
            return 0
        for e in active:
            if not any(r["event_id"] == e["id"] for r in rows):
                rows.append({"event_id": e["id"], "camera": e.get("camera"), "person_id": None, "start_time": e["start_time"],
                             "end_time": None, "method": "live", "in_progress": True,
                             "appearance_person": None, "appearance_group": None})
        groups = [g for g in stats.group_visits(rows, cfg.visit_gap_seconds) if g["start_time"] >= since]
        if not groups:
            return 0
        names = {p["id"]: p.get("display_name") for p in self.db.persons()}
        day_rows = self.db.all_visits(now - 86400)
        day_groups = stats.group_visits(day_rows, cfg.visit_gap_seconds)
        min_level = riskmod.LEVELS.index(cfg.alert_min_level) if cfg.alert_min_level in riskmod.LEVELS else 1
        sent = 0
        for g in groups:
            subject = stats.who(g)
            repeats = sum(1 for d in day_groups if subject and stats.who(d) == subject) if subject else 0
            r = riskmod.assess(g, names, repeats, now=now)
            if r.score == 0:
                continue
            prev = self.db.get_alert(g["event_id"])
            prev_level = riskmod.LEVELS.index(prev["level"]) if prev else -1
            level_idx = riskmod.LEVELS.index(r.level)
            # 通知するのは、初めて評価した来訪か、まだ続いている来訪が格上げされたとき。
            # 終わった来訪が後から（再来訪の加点などで）上がっても再通知はしない
            escalation_ok = prev is None or g.get("in_progress")
            should_notify = (
                self.notifier is not None and self.notifier.enabled
                and level_idx >= min_level and level_idx > prev_level and escalation_ok
                and self.db.notifications_since(now - 3600) < cfg.alert_max_per_hour
                and not (self.db.last_notified(subject, now - cfg.alert_cooldown_seconds) and prev is None)
            )
            notified = False
            if should_notify:
                notified = self._notify(g, r, subject, names, now)
                sent += 1 if notified else 0
            if prev is None or level_idx > prev_level or notified:
                self.db.upsert_alert(g["event_id"], subject, r.level, r.score, r.reasons, g["start_time"], now, notified)
        return sent

    def _notify(self, g: dict, r, subject: Optional[str], names: Dict[str, Optional[str]], now: float) -> bool:
        who = (f"{subject} {names.get(subject) or ''}".strip() if subject else "未特定の人物")
        title = f"[{riskmod.LEVEL_JA[r.level]}] 玄関前: {who}"
        t = time.strftime("%m/%d %H:%M", time.localtime(g["start_time"]))
        end = now if g.get("in_progress") else (g.get("end_time") or g["start_time"])
        dwell = int(max(0.0, end - g["start_time"]))
        fields = [("時刻", t), ("滞在", f"{dwell} 秒" + ("（継続中）" if g.get("in_progress") else "")),
                  ("スコア", f"{r.score}"), ("理由", "、".join(r.reasons) or "-")]
        image = None
        try:
            image = self.client.event_snapshot(g["event_id"], crop=True, quality=80)
        except Exception:  # noqa: BLE001
            image = None
        link = (self.cfg.dashboard_url.rstrip("/") + "/") if self.cfg.dashboard_url else None
        ok = self.notifier.send(title, f"検知 {g['events']} 件、{len(r.reasons)} 項目が該当", r.level, fields, image, link)
        log.info("アラート %s %s score=%d %s -> %s", r.level, g["event_id"], r.score, r.reasons, "通知" if ok else "未通知")
        return ok

    # ------------------------------------------------------------ appearance
    def _link_appearance(self, now: float) -> int:
        """顔で特定できなかった来訪を、見た目（Frigate のサムネイル埋め込み）で前後の来訪と結び付ける。

        - 似ている相手が顔で特定済みなら appearance_person（推定の人物）
        - 相手も未特定なら共通の appearance_group（同じ見た目のグループ a0001…）
        顔の学習には一切使わない。
        """
        if not self.cfg.appearance_enabled or getattr(self, "_appearance_disabled", False):
            return 0
        window = self.cfg.appearance_window_hours * 3600
        # 終了から 60 秒以上経ったもの（Frigate の埋め込みが済んでいる）だけ
        rows = self.db.unchecked_visits(now - window, now - 60, self.cfg.appearance_batch)
        linked = 0
        for v in rows:
            eid = v["event_id"]
            try:
                res = self.client.similar_events(eid, v["start_time"] - window, v["start_time"] + window, limit=20)
            except Exception as e:  # noqa: BLE001
                log.warning("event %s: 類似検索に失敗（次回再試行）: %s", eid, e)
                return linked
            if res is None:
                log.warning("Frigate のセマンティック検索が無効のため、見た目による紐付けを停止します")
                self._appearance_disabled = True
                return linked
            thr = self.cfg.appearance_max_distance
            thr_group = min(self.cfg.appearance_group_max_distance, thr)
            dists = {e["id"]: float(e["search_distance"]) for e in res
                     if e.get("id") and e.get("id") != eid and e.get("search_distance") is not None}
            # 1) 顔で特定済みの来訪との直接一致を最優先（推定の連鎖で別人に流れないよう、推定同士は使わない）
            face_best = None
            group_best = None
            for oid, dist in sorted(dists.items(), key=lambda kv: kv[1]):
                if dist > thr:
                    break
                cand = self.db.visit(oid)
                if not cand or cand["method"] == PENDING:
                    continue
                if cand.get("person_id") and face_best is None:
                    face_best = (dist, cand)
                elif (dist <= thr_group and not cand.get("person_id") and not cand.get("appearance_person")
                      and group_best is None):
                    group_best = (dist, cand)
            if face_best is not None:
                dist, cand = face_best
                person = cand["person_id"]
                self.db.set_appearance(eid, person, v.get("appearance_group"), round(1.0 - dist, 3), cand["event_id"])
                log.info("event %s -> 見た目で %s と推定 (距離 %.3f, 相手 %s)", eid, person, dist, cand["event_id"])
                if v.get("appearance_group"):
                    n = self.db.promote_group(v["appearance_group"], person)
                    if n:
                        log.info("見た目グループ %s の %d 件を %s に格上げ", v["appearance_group"], n, person)
                linked += 1
                continue
            if group_best is None:
                self.db.mark_appearance_checked(eid)
                continue
            # 2) 未特定同士のグループ。数珠つなぎで別人が混ざらないよう、グループの起点とも似ていることを要求する
            dist, cand = group_best
            group = cand.get("appearance_group")
            if group:
                anchor = self.db.group_anchor(group)
                if anchor and anchor != cand["event_id"] and dists.get(anchor, 9.0) > thr_group:
                    log.info("event %s: グループ %s の起点と似ていないため参加しない (相手 %s)", eid, group, cand["event_id"])
                    self.db.mark_appearance_checked(eid)
                    continue
            else:
                group = self.db.next_group_id()
                self.db.set_appearance_group(cand["event_id"], group)
            self.db.set_appearance(eid, None, group, round(1.0 - dist, 3), cand["event_id"])
            log.info("event %s -> 見た目グループ %s (距離 %.3f, 相手 %s)", eid, group, dist, cand["event_id"])
            linked += 1
        return linked

    def relink_appearance(self, hours: float) -> int:
        """期間内の見た目の紐付けを消して、次の周期からやり直す（しきい値を変えたときなど）。"""
        with self.lock:
            self._appearance_disabled = False
            return self.db.reset_appearance(self.now() - hours * 3600)

    def _fetch_ended(self, now: float) -> Dict[str, dict]:
        floor = now - self.cfg.lookback_hours * 3600
        cursor = self.db.get_state("cursor")
        after = max(float(cursor) - self.cfg.overlap_seconds, floor) if cursor else floor
        events: Dict[str, dict] = {}
        for _ in range(MAX_PAGES):
            page = self.client.list_events(self.cfg.camera, after=after, in_progress=0, limit=PAGE, sort="date_asc")
            new = [e for e in page if e["id"] not in events]
            for e in page:
                events[e["id"]] = e
            if len(page) < PAGE or not new:
                break
            after = max(e["start_time"] for e in page) - EPS
        else:
            log.warning("未処理イベントが多いため今回は %d 件まで。残りは次の周期で処理します", len(events))
        return events

    def _give_up(self, ev: dict, reason: str) -> Decision:
        d = Decision("none", reason=f"retry limit: {reason}")
        pending = self.db.visit_pending(ev["id"]) or {}
        self.db.record_visit(
            event_id=ev["id"], camera=ev.get("camera", self.cfg.camera), person_id=None,
            start_time=ev["start_time"], end_time=ev.get("end_time"), method=d.method,
            face_file=pending.get("face_file"), zones=ev.get("zones") or [], reason=d.reason,
        )
        log.warning("event %s -> 諦め (%s)", ev["id"], d.reason)
        return d

    # ----------------------------------------------------------------- decide
    @staticmethod
    def _known(faces: Dict[str, List[str]]) -> List[str]:
        return [n for n in faces if n != TRAIN]

    @staticmethod
    def _library_empty(faces: Dict[str, List[str]]) -> bool:
        return not any(faces.get(n) for n in faces if n != TRAIN)

    def _process_event(self, ev: dict, attempts: List[Attempt], faces: Dict[str, List[str]]) -> Decision:
        eid = ev["id"]
        known = self._known(faces)
        data = ev.get("data") or {}
        pending = self.db.visit_pending(eid) or {}

        # 統合済みの古い名前は現在の ID に読み替える
        sub_label = self.db.resolve_alias(ev.get("sub_label"))
        for a in attempts:
            a.name = self.db.resolve_alias(a.name) or a.name

        # 読み取りだけ先に済ませる。画像が取れない試行（Frigate の上限で消えた等）は無かったものとして扱う
        usable: List[Attempt] = []
        for a in attempts:
            px = self._face_px(a)
            if px is None:
                continue
            a.face_px = px
            usable.append(a)
        attempts = usable

        d = decide(sub_label, data.get("sub_label_score"), attempts, known, self.cfg)
        # Frigate の照合結果（frigate / attempts）も、品質を満たす顔が試行画像に 1 枚も無ければ信じない。
        # 顔でない領域（後頭部など）を高スコアで既存 ID に一致させてしまう事故があったため
        if d.method in ("frigate", "attempts") and self.facecheck is not None:
            if not self._verified_attempt(attempts):
                d = Decision("none", reason=f"unverified {d.method} match {d.person_id}")
        face_file: Optional[str] = pending.get("face_file")
        if d.method == "none" and not attempts:
            d, snap_file = self._decide_by_snapshot(ev, faces)
            face_file = snap_file or face_file
        if d.method == "retry":
            return d

        person_id = d.person_id
        if d.method == "new" and self.facecheck is not None:
            verdicts = [self.facecheck.acceptable(self.client.attempt_image(f) or b"", self.cfg.min_face_px) for f in d.train_files]
            if not any(ok for ok, _ in verdicts):
                d = Decision("none", reason="attempts: " + (verdicts[0][1] if verdicts else "no face"))
        if d.method == "new":
            if self._person_cap_reached(faces):
                d = Decision("none", reason=f"person cap {self.cfg.max_persons}")
                person_id = None
            else:
                person_id = pending.get("person_id") or self.db.peek_person_id(self.cfg.id_prefix, self.cfg.id_digits, known)
                d.person_id = person_id

        classified = set()
        # 顔で一致した来訪は、補強学習をしなくても画面用に顔の切り抜きを 1 枚控えておく
        if face_file is None and person_id and attempts and not d.train_files:
            best_attempt = max(attempts, key=lambda a: (a.name == person_id, a.score, a.face_px))
            face_file = self._save_attempt_copy(eid, best_attempt.file)
        if d.train_files and person_id:
            if face_file is None:
                face_file = self._save_attempt_copy(eid, d.train_files[0])
            self._mark_pending(ev, person_id if d.method == "new" else None, face_file)
            count = len(faces.get(person_id, []))
            for f in d.train_files:
                if d.method != "new" and count >= self.cfg.max_images_per_person:
                    break
                try:
                    ok = self.client.classify_attempt(person_id, f)
                except Exception as e:  # noqa: BLE001
                    if d.method == "new":
                        raise
                    log.warning("event %s: 補強画像の登録に失敗（記録は続行）: %s", eid, e)
                    break
                if ok:
                    faces.setdefault(person_id, []).append(f)
                    classified.add(f)
                    count += 1
            if d.method == "new":
                if not classified:
                    d = Decision("none", reason="no attempt could be classified")
                    person_id = None
                else:
                    self.db.commit_person_seq(person_id, self.cfg.id_prefix)

        seen_at = ev.get("end_time") or ev["start_time"]
        self.db.record_visit(
            event_id=eid,
            camera=ev.get("camera", self.cfg.camera),
            person_id=person_id,
            start_time=ev["start_time"],
            end_time=ev.get("end_time"),
            method=d.method,
            score=d.score,
            face_file=face_file,
            zones=ev.get("zones") or [],
            reason=d.reason,
        )
        self._estimate_gender(eid, face_file)
        if person_id:
            self.db.refresh_person(person_id, seen_at)
            if ev.get("sub_label") != person_id:
                try:
                    self.client.set_sub_label(eid, person_id, d.score)
                except Exception as e:  # noqa: BLE001
                    log.warning("event %s: Frigate へのラベル書き戻しに失敗（記録は完了）: %s", eid, e)
        self._to_delete.extend(a.file for a in attempts if a.file not in classified)

        log.info("event %s -> %s (%s%s)", eid, person_id or "-", d.method, f", {d.reason}" if d.reason else "")
        return d

    def _mark_pending(self, ev: dict, person_id: Optional[str], face_file: Optional[str]) -> None:
        """Frigate 側を変更する直前に「処理中」として記録する。途中で落ちても次回は同じ ID で続きから再開する。"""
        self.db.record_visit(
            event_id=ev["id"], camera=ev.get("camera", self.cfg.camera), person_id=person_id,
            start_time=ev["start_time"], end_time=ev.get("end_time"), method=PENDING,
            face_file=face_file, zones=ev.get("zones") or [],
        )

    def _decide_by_snapshot(self, ev: dict, faces: Dict[str, List[str]]) -> Tuple[Decision, Optional[str]]:
        """試行画像が無いイベント（登録ゼロの時期のものなど）はスナップショットで判定する。"""
        eid = ev["id"]
        if not ev.get("has_snapshot"):
            return Decision("none", reason="no snapshot"), None
        if not self._person_big_enough(ev):
            return Decision("none", reason="person too small"), None
        img = self.client.event_snapshot(eid, crop=True)
        if not img:
            return Decision("none", reason="snapshot unavailable"), None
        if self.facecheck is not None:
            ok, why = self.facecheck.acceptable(img, self.cfg.min_face_px)
            if not ok:
                return Decision("none", reason=f"snapshot: {why}"), None
        known = self._known(faces)

        res = self.client.recognize(img, filename=f"{eid}.jpg")
        if not res.get("success"):
            msg = res.get("message", "")
            if "recognized" not in msg:
                return Decision("none", reason=f"snapshot: {msg}"), None
            # 顔はあるが分類器が無い（登録ゼロ）か構築中
            if self._library_empty(faces):
                return self._register_new(ev, img, faces, known)
            self.sleep(self.cfg.recognize_retry_delay)
            res = self.client.recognize(img, filename=f"{eid}.jpg")
            if not res.get("success"):
                msg = res.get("message", "")
                if "recognized" in msg:
                    return Decision("retry", reason=msg), None
                return Decision("none", reason=f"snapshot: {msg}"), None

        name, score = self.db.resolve_alias(res.get("face_name")), float(res.get("score") or 0)
        if name in known and name != UNKNOWN and score >= self.cfg.merge_score:
            if (self.cfg.reinforce_per_event > 0 and score >= self.cfg.reinforce_min_score
                    and len(faces.get(name, [])) < self.cfg.max_images_per_person):
                try:
                    if self.client.register_face(name, img, filename=f"{eid}.jpg").get("success"):
                        faces.setdefault(name, []).append("registered")
                except Exception as e:  # noqa: BLE001
                    log.warning("event %s: 補強登録に失敗（記録は続行）: %s", eid, e)
            return Decision("snapshot", name, score, []), self._save_bytes(eid, img, "jpg")
        return self._register_new(ev, img, faces, known)

    def _register_new(self, ev: dict, img: bytes, faces: Dict[str, List[str]], known: List[str]):
        eid = ev["id"]
        if self._person_cap_reached(faces):
            return Decision("none", reason=f"person cap {self.cfg.max_persons}"), None
        pending = self.db.visit_pending(eid) or {}
        pid = pending.get("person_id") or self.db.peek_person_id(self.cfg.id_prefix, self.cfg.id_digits, known)
        face_file = self._save_bytes(eid, img, "jpg")
        self._mark_pending(ev, pid, face_file)
        res = self.client.register_face(pid, img, filename=f"{eid}.jpg")
        if not res.get("success"):
            return Decision("none", reason=f"register: {res.get('message', '')}"), face_file
        self.db.commit_person_seq(pid, self.cfg.id_prefix)
        faces[pid] = faces.get(pid, []) + ["registered"]
        return Decision("snapshot-new", pid, None, [], is_new=True), face_file

    # ---------------------------------------------------------------- helpers
    def _verified_attempt(self, attempts: List[Attempt]) -> bool:
        """試行画像（スコア順に最大 5 枚）のどれかが品質チェックに通るか。"""
        for a in sorted(attempts, key=lambda a: a.score, reverse=True)[:5]:
            data = self.client.attempt_image(a.file)
            if data and self.facecheck.acceptable(data, self.cfg.min_face_px)[0]:
                return True
        return False

    def estimate_gender_from_file(self, face_file: Optional[str]):
        """保存済みの顔画像コピーから (gender, score, age) を推定。条件を満たさなければ (None, None, None)。"""
        if not face_file or self.gender is None or self.facecheck is None:
            return None, None, None
        try:
            with open(os.path.join(self.cfg.data_dir, face_file), "rb") as fh:
                data = fh.read()
        except OSError:
            return None, None, None
        ok, _why, info = self.facecheck.inspect(data, self.cfg.min_face_px)
        if not ok or info is None:
            return None, None, None
        res = self.gender.estimate(data, info.box)
        if res is None or res.confidence < 0.75:
            return None, None, None
        return res.gender, round(res.confidence, 2), res.age

    def _estimate_gender(self, eid: str, face_file: Optional[str]) -> None:
        try:
            gender, score, age = self.estimate_gender_from_file(face_file)
        except Exception as e:  # noqa: BLE001
            log.warning("event %s: 性別推定に失敗: %s", eid, e)
            return
        if gender:
            self.db.set_visit_gender(eid, gender, score, age)

    def backfill_gender(self) -> Dict[str, int]:
        """既存の訪問記録（顔画像コピーあり）に性別・年齢推定を付け直す。"""
        done = est = 0
        for v in self.db.visits_with_faces():
            gender, score, age = self.estimate_gender_from_file(v["face_file"])
            self.db.set_visit_gender(v["event_id"], gender, score, age)
            done += 1
            est += 1 if gender else 0
        return {"visits": done, "estimated": est}

    def _person_cap_reached(self, faces: Dict[str, List[str]]) -> bool:
        auto = [n for n in self._known(faces) if self._id_pattern.match(n)]
        if len(auto) >= self.cfg.max_persons:
            log.warning("自動発行の人物数が上限 %d に達しています。新規 ID は作りません", self.cfg.max_persons)
            return True
        return False

    def _person_big_enough(self, ev: dict) -> bool:
        box = (ev.get("data") or {}).get("box")
        if not box or len(box) < 4:
            return True
        if self._detect_size is None:
            self._detect_size = self.client.detect_size(self.cfg.camera) or DEFAULT_DETECT
        return box[3] * self._detect_size[1] >= self.cfg.min_person_px

    def _face_px(self, a: Attempt) -> Optional[int]:
        """顔画像の短辺 px。画像が取得できなければ None。"""
        data = self.client.attempt_image(a.file)
        if not data:
            return None
        w, h = self.measure(data)
        return min(w, h)

    def _save_attempt_copy(self, eid: str, file: str) -> Optional[str]:
        data = self.client.attempt_image(file)
        return self._save_bytes(eid, data, "webp") if data else None

    def _save_bytes(self, eid: str, data: bytes, ext: str) -> Optional[str]:
        try:
            os.makedirs(self.faces_dir, exist_ok=True)
            rel = os.path.join("faces", f"{eid}.{ext}")
            with open(os.path.join(self.cfg.data_dir, rel), "wb") as fh:
                fh.write(data)
            return rel
        except OSError:
            log.warning("顔画像の保存に失敗: %s", eid)
            return None

    def _cleanup_attempts(self, leftovers: Dict[str, List[Attempt]]) -> None:
        """今回のイベント一覧に無かった試行画像: イベントが終了済み or 消滅していれば削除対象。進行中なら残す。"""
        now = self.now()
        for eid, attempts in leftovers.items():
            newest = max(a.timestamp for a in attempts)
            if now - newest < 60:
                continue  # 直近のもの（進行中の可能性大）は次回に回す
            ev = self.client.get_event(eid)
            stale = now - newest > self.cfg.stale_attempt_hours * 3600
            if ev is None or (ev.get("end_time") and (self.db.has_visit(eid) or stale)):
                self._to_delete.extend(a.file for a in attempts)

    def _flush_deletes(self) -> None:
        """train の削除は 1 周期に 1 回まとめる（Frigate は削除のたびに分類器を作り直すため）。"""
        if not self._to_delete:
            return
        ids = list(dict.fromkeys(self._to_delete))
        self._to_delete = []
        try:
            self.client.delete_faces(TRAIN, ids)
        except Exception as e:  # noqa: BLE001
            log.warning("試行画像 %d 件の削除に失敗（次回再試行）: %s", len(ids), e)

    def _prune(self, faces: Dict[str, List[str]], now: float) -> None:
        """1 時間に 1 回: 保持期限切れの顔画像コピーと、一度しか来ていない古い自動 ID を消す。"""
        last = float(self.db.get_state("last_prune", "0") or 0)
        if now - last < 3600:
            return
        self.db.set_state("last_prune", repr(now))
        cutoff = now - self.cfg.retain_days * 86400

        removed = []
        if os.path.isdir(self.faces_dir):
            for name in os.listdir(self.faces_dir):
                path = os.path.join(self.faces_dir, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed.append(os.path.join("faces", name))
                except OSError:
                    pass
        self.db.null_face_files(removed)
        if removed:
            log.info("保持期限切れの顔画像を削除: %d 件", len(removed))

        expired = self.db.expired_persons(self.cfg.id_prefix, cutoff, self.cfg.expire_max_visits)
        for pid in expired:
            try:
                files = faces.get(pid, [])
                if files:
                    self.client.delete_faces(pid, files)
                faces.pop(pid, None)
                self.db.delete_person(pid)
                log.info("長く見ていない自動 ID を削除: %s", pid)
            except Exception as e:  # noqa: BLE001
                log.warning("%s の削除に失敗: %s", pid, e)

    # ------------------------------------------------------------------ admin
    def clean_library(self, dry_run: bool = False) -> Dict[str, Dict[str, int]]:
        """各人物の登録画像を品質チェックにかけ、通らないものを削除する。
        全滅する場合は検出スコアが最も高い 1 枚を残して ID を維持する。"""
        with self.lock:
            return self._clean_library(dry_run)

    def _clean_library(self, dry_run: bool) -> Dict[str, Dict[str, int]]:
        if self.facecheck is None:
            raise ValueError("顔品質チェックが無効のため掃除できません（FACE_MODEL 未設定）")
        faces = self.client.faces()
        report: Dict[str, Dict[str, int]] = {}
        for name in self._known(faces):
            files = faces.get(name, [])
            keep, drop, scored = [], [], []
            for f in files:
                data = self.client.face_image(name, f)
                if not data:
                    drop.append(f)
                    continue
                ok, _why, info = self.facecheck.inspect(data, self.cfg.min_face_px)
                (keep if ok else drop).append(f)
                scored.append((info.score if info else 0.0, f))
            if not keep and scored:
                best = max(scored)[1]
                drop = [f for f in drop if f != best]
                keep = [best]
            report[name] = {"total": len(files), "kept": len(keep), "deleted": len(drop)}
            if drop and not dry_run:
                self.client.delete_faces(name, drop)
                log.info("clean %s: %d 枚中 %d 枚を削除", name, len(files), len(drop))
        return report

    def purge(self, pid: str) -> Dict[str, int]:
        """誤って作られた ID を消す: Frigate の登録画像、persons 行。訪問は未特定に戻す。"""
        with self.lock:
            return self._purge(pid)

    def _purge(self, pid: str) -> Dict[str, int]:
        faces = self.client.faces()
        if pid not in faces and pid not in self.db.person_ids():
            raise ValueError(f"{pid} は存在しません")
        files = faces.get(pid, [])
        if files:
            self.client.delete_faces(pid, files)
        visits = 0
        for v in self.db.visits(pid, limit=100000):
            self.db.record_visit(
                event_id=v["event_id"], camera=v["camera"], person_id=None, start_time=v["start_time"],
                end_time=v["end_time"], method="none", face_file=v["face_file"], reason=f"purged {pid}",
            )
            try:
                self.client.set_sub_label(v["event_id"], "", None)
            except Exception:  # noqa: BLE001
                pass
            visits += 1
        self.db.delete_person(pid)
        self.db.clear_appearance_for_person(pid)
        self.db.commit_person_seq(pid, self.cfg.id_prefix)  # 番号は再利用しない
        log.info("purge %s: 画像 %d 枚, 訪問 %d 件", pid, len(files), visits)
        return {"images_deleted": len(files), "visits_cleared": visits}

    def merge(self, src: str, dst: str) -> Dict[str, int]:
        """src の登録画像を dst に移し、訪問記録も付け替え、src を別名として記録する。

        Frigate は最後の画像を移した時点で空フォルダを自分で消すので、
        残骸が残った場合だけ削除を試み、失敗しても DB 側の統合は続ける。
        """
        with self.lock:
            return self._merge(src, dst)

    def _merge(self, src: str, dst: str) -> Dict[str, int]:
        if src == dst:
            raise ValueError("統合元と統合先が同じです")
        faces = self.client.faces()
        if src not in faces and src not in self.db.person_ids():
            raise ValueError(f"{src} は存在しません")
        if dst not in faces and dst not in self.db.person_ids() and not self._id_pattern.match(dst):
            raise ValueError(f"{dst} は存在しません（新しい名前にはできません）")
        moved = 0
        for f in faces.get(src, []):
            if self.client.reclassify(src, f, dst):
                moved += 1
        remaining = self.client.faces().get(src, [])
        if remaining:
            try:
                self.client.delete_faces(src, remaining)
            except Exception as e:  # noqa: BLE001
                log.warning("%s の残り画像の削除に失敗（続行）: %s", src, e)
        visits = self.db.merge_person(src, dst, self.cfg.id_prefix)
        self.db.rename_appearance_person(src, dst)
        relabeled = 0
        for eid in self.db.merged_event_ids(dst):
            try:
                if self.client.set_sub_label(eid, dst, None):
                    relabeled += 1
            except Exception:  # noqa: BLE001
                pass
        log.info("merge %s -> %s: 画像 %d 枚, 訪問 %d 件, ラベル書き換え %d 件", src, dst, moved, visits, relabeled)
        return {"images_moved": moved, "visits_moved": visits}
