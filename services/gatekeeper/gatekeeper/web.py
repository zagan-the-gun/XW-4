"""ダッシュボード: 集計 API と静的ページ（標準ライブラリの http.server）。

認証は無いが LAN 内での安全策として、変更系の API は JSON と独自ヘッダを必須にし（CSRF 対策）、
表示名に HTML 記号を許さず、CSP と nosniff を付ける。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import stats
from .config import Settings
from .db import Database

log = logging.getLogger(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,50}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
EVENT_ID = re.compile(r"^[0-9.]+-[A-Za-z0-9]+$")
BAD_NAME_CHARS = re.compile(r"[<>&\"'\x00-\x1f\x7f]")
REQUIRED_HEADER = ("X-Requested-With", "gatekeeper")
MAX_BODY = 65536
MAX_DAYS = 366
MAX_LIMIT = 500
CSP = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
mimetypes.add_type("image/webp", ".webp")


def _int_param(q: Dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = q.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{key} は整数で指定してください") from None
    if not lo <= v <= hi:
        raise ValueError(f"{key} は {lo}〜{hi} の範囲で指定してください")
    return v


def _local_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def period_start(now: float, days: int) -> float:
    """日別グラフと同じ暦日境界（今日を含む days 日分の初日 0:00）。"""
    return (datetime.fromtimestamp(_local_midnight(now)) - timedelta(days=days - 1)).timestamp()


class Api:
    """HTTP 非依存の API 本体。dispatch() が (status, payload) を返す。"""

    def __init__(self, cfg: Settings, db_path: str, gatekeeper=None, now=time.time):
        self.cfg = cfg
        self.db_path = db_path
        self.gk = gatekeeper
        self.now = now
        self._local = threading.local()
        self._persons_cache: Tuple[float, Any] = (0.0, None)

    def _db(self) -> Database:
        db = getattr(self._local, "db", None)
        if db is None:
            db = Database(self.db_path)
            self._local.db = db
        return db

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Optional[dict]) -> Tuple[int, Any]:
        try:
            return self._route(method, path, query, body if isinstance(body, dict) else {})
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("API エラー %s %s", method, path)
            return 500, {"error": str(e)}

    # ------------------------------------------------------------ queries
    def _route(self, method: str, path: str, q: Dict[str, str], body: dict) -> Tuple[int, Any]:
        db = self._db()
        now = self.now()
        gap = self.cfg.visit_gap_seconds
        tz = time.strftime("%Z")
        today = time.strftime("%Y-%m-%d", time.localtime(now))

        if method == "GET" and path == "/api/summary":
            days = _int_param(q, "days", 7, 1, MAX_DAYS)
            visits = db.all_visits(period_start(now, days))
            grouped = stats.group_visits(visits, gap)
            today_groups = [g for g in grouped if time.strftime("%Y-%m-%d", time.localtime(g["start_time"])) == today]
            return 200, {
                "days": days,
                "period_start": period_start(now, days),
                "tz": tz,
                "today": today,
                "events": len([v for v in visits if v["method"] != "pending"]),
                "visits": len(grouped),
                "identified_visits": len([g for g in grouped if g.get("person_id")]),
                "appearance_visits": len([g for g in grouped if not g.get("person_id") and g.get("appearance_person")]),
                "grouped_visits": len([g for g in grouped if not g.get("person_id") and not g.get("appearance_person") and g.get("appearance_group")]),
                "persons": len({g.get("person_id") or g.get("appearance_person") for g in grouped if g.get("person_id") or g.get("appearance_person")}),
                "today_events": sum(g["events"] for g in today_groups),
                "today_visits": len(today_groups),
                "gender": stats.gender_ratio(visits, gap),
                "generated_at": now,
            }
        if method == "GET" and path == "/api/traffic":
            days = _int_param(q, "days", 7, 1, MAX_DAYS)
            visits = db.all_visits(period_start(now, days))
            return 200, {
                "tz": tz,
                "daily": stats.daily_counts(visits, days, now, gap),
                "hourly_today": stats.hourly_counts(visits, today),
                "hourly_all": stats.hourly_counts(visits),
            }
        if method == "GET" and path == "/api/heatmap":
            days = _int_param(q, "days", 28, 1, MAX_DAYS)
            visits = db.all_visits(period_start(now, days))
            return 200, {"days": days, "tz": tz, "all": stats.heatmap(visits), "identified": stats.heatmap(visits, identified_only=True)}
        if method == "GET" and path == "/api/alerts":
            days = _int_param(q, "days", 7, 1, MAX_DAYS)
            min_level = q.get("min", "medium")
            if min_level not in ("low", "medium", "high"):
                raise ValueError("min は low / medium / high")
            order = {"low": 0, "medium": 1, "high": 2}
            names = {p["id"]: p.get("display_name") for p in db.persons()}
            out = []
            for a in db.alerts(period_start(now, days), limit=2000):
                if order[a["level"]] < order[min_level]:
                    continue
                a["display_name"] = names.get(a.get("subject"))
                out.append(a)
            return 200, {"alerts": out[:100], "tz": tz, "min": min_level}
        if method == "GET" and path == "/api/appearance-groups":
            days = _int_param(q, "days", 7, 1, MAX_DAYS)
            visits = db.all_visits(period_start(now, days))
            return 200, {"groups": stats.appearance_groups(visits, gap)[:50], "tz": tz}
        if method == "GET" and path == "/api/persons":
            ts, cached = self._persons_cache
            if cached is None or now - ts > 30:
                cached = stats.person_summaries(db.persons(), db.all_visits(), gap)
                self._persons_cache = (now, cached)
            return 200, {"persons": cached}
        if method == "GET" and path == "/api/visits":
            limit = _int_param(q, "limit", 50, 1, MAX_LIMIT)
            person = q.get("person") or None
            if person and not SAFE_ID.match(person):
                raise ValueError("person が不正です")
            # 行数ではなく期間で切り出してからまとめる（長い滞在が途中で切れないように）
            rows = db.all_visits(now - 30 * 86400)
            if person:
                rows = [r for r in rows if r.get("person_id") == person or r.get("appearance_person") == person
                        or r.get("appearance_group") == person]
            grouped = stats.group_visits(rows, gap)
            grouped.sort(key=lambda g: g["start_time"], reverse=True)
            names = {p["id"]: p.get("display_name") for p in db.persons()}
            alerts = {a["event_id"]: a for a in db.alerts(now - 30 * 86400, limit=5000)}
            out = []
            for g in grouped[:limit]:
                al = alerts.get(g["event_id"])
                out.append({
                    "risk_level": al["level"] if al else None, "risk_score": al["score"] if al else None,
                    "risk_reasons": al["reasons"] if al else None,
                    "event_id": g["event_id"], "event_ids": g["event_ids"], "start_time": g["start_time"],
                    "end_time": g.get("end_time"), "duration": g.get("duration"), "events": g["events"],
                    "person_id": g.get("person_id"), "display_name": names.get(g.get("person_id") or g.get("appearance_person")),
                    "appearance_person": g.get("appearance_person"), "appearance_group": g.get("appearance_group"),
                    "appearance_score": g.get("appearance_score"),
                    "method": g["method"], "score": g.get("score"), "reason": g.get("reason"),
                    "gender": g.get("gender"), "gender_score": g.get("gender_score"), "age": g.get("age"),
                    "face_file": g.get("face_file"),
                })
            return 200, {"visits": out, "tz": tz}

        # --------------------------------------------------------- mutations
        m = re.match(r"^/api/persons/([^/]+)$", path)
        if method == "PUT" and m:
            pid = m.group(1)
            if not SAFE_ID.match(pid):
                raise ValueError("invalid id")
            raw = body.get("display_name")
            if raw is not None and not isinstance(raw, str):
                raise ValueError("display_name は文字列で指定してください")
            name = (raw or "").strip() or None
            if name and len(name) > 50:
                raise ValueError("表示名は 50 文字まで")
            if name and BAD_NAME_CHARS.search(name):
                raise ValueError("表示名に < > & \" ' や制御文字は使えません")
            if not db.rename_person(pid, name):
                return 404, {"error": f"{pid} が見つかりません"}
            self._persons_cache = (0.0, None)
            return 200, {"id": pid, "display_name": name}
        m = re.match(r"^/api/persons/([^/]+)/(merge|purge)$", path)
        if method == "POST" and m:
            pid, action = m.group(1), m.group(2)
            if not SAFE_ID.match(pid):
                raise ValueError("invalid id")
            if self.gk is None:
                return 503, {"error": "この操作は run モードでのみ使えます"}
            if action == "merge":
                dst = body.get("into")
                if not isinstance(dst, str) or not SAFE_ID.match(dst):
                    raise ValueError("統合先の ID が不正です")
            # 処理ループと同時に走らせない（ループが持つ人物一覧と食い違い、消した ID が復活するため）
            if not self.gk.lock.acquire(timeout=60):
                return 409, {"error": "処理中です。少し待ってからやり直してください"}
            try:
                result = self.gk.merge(pid, dst) if action == "merge" else self.gk.purge(pid)
            finally:
                self.gk.lock.release()
            self._persons_cache = (0.0, None)
            return 200, result
        return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    api: Api = None  # type: ignore[assignment]
    client = None
    data_dir = ""
    db_path = ""
    timeout = 30  # 遅い/途中で止まる接続でスレッドを占有しない

    def log_message(self, fmt, *args):  # アクセスログは静かに
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, ctype: str, cache: Optional[str] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "same-origin")
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", "no-store")

    def _read_json(self) -> Tuple[Optional[dict], Optional[Tuple[int, str]]]:
        """(本文, エラー)。本文は JSON オブジェクトのみ。"""
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len or 0)
        except ValueError:
            return None, (400, "Content-Length が不正です")
        if n < 0 or n > MAX_BODY:
            return None, (413, "本文が大きすぎます")
        if n == 0:
            return {}, None
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, "本文が JSON ではありません")
        if not isinstance(data, dict):
            return None, (400, "本文は JSON オブジェクトにしてください")
        return data, None

    def _mutation_allowed(self) -> Optional[str]:
        """変更系の要求に対する CSRF 対策。ブラウザのフォームでは送れない条件を要求する。"""
        if self.headers.get(REQUIRED_HEADER[0]) != REQUIRED_HEADER[1]:
            return f"{REQUIRED_HEADER[0]}: {REQUIRED_HEADER[1]} ヘッダが必要です"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if int(self.headers.get("Content-Length") or 0) > 0 and ctype != "application/json":
            return "Content-Type は application/json にしてください"
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return "cross-site request"
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and host and urlparse(origin).netloc != host:
            return "origin mismatch"
        return None

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._static("index.html")
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])
        if u.path.startswith("/faces/"):
            name = u.path[len("/faces/"):]
            if not SAFE_FILE.match(name):
                return self._json(400, {"error": "bad name"})
            p = os.path.join(self.data_dir, "faces", name)
            if not os.path.isfile(p):
                return self._json(404, {"error": "not found"})
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), mimetypes.guess_type(p)[0] or "application/octet-stream", "max-age=86400")
        m = re.match(r"^/api/events/([^/]+)/clip\.mp4$", u.path)
        if m:
            eid = m.group(1)
            if not EVENT_ID.match(eid):
                return self._json(400, {"error": "bad id"})
            if self.client is None or self.api._db().visit_method(eid) is None:
                return self._json(404, {"error": "unknown event"})
            try:
                chunks = self.client.event_clip_stream(eid)
            except Exception:  # noqa: BLE001
                return self._json(502, {"error": "clip unavailable"})
            if chunks is None:
                return self._json(404, {"error": "no clip (recording not retained)"})
            # Frigate は長さ不明のまま流してくるので、そのまま中継して接続を閉じる
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in chunks:
                    if chunk:
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None
        m = re.match(r"^/api/events/([^/]+)/snapshot\.jpg$", u.path)
        if m:
            eid = m.group(1)
            if not EVENT_ID.match(eid):
                return self._json(400, {"error": "bad id"})
            # このサービスが記録したイベントだけ Frigate から取り出す（他カメラの画像を LAN に出さない）
            if self.client is None or self.api._db().visit_method(eid) is None:
                return self._json(404, {"error": "unknown event"})
            try:
                img = self.client.event_snapshot(eid, crop=True, quality=70)
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"error": "snapshot unavailable"})
            if not img:
                return self._json(404, {"error": "no snapshot"})
            return self._send(200, img, "image/jpeg", "max-age=3600")
        status, payload = self.api.dispatch("GET", u.path, q, None)
        self._json(status, payload)

    def _mutate(self, method: str) -> None:
        u = urlparse(self.path)
        why = self._mutation_allowed()
        if why:
            return self._json(403, {"error": why})
        body, err = self._read_json()
        if err:
            return self._json(err[0], {"error": err[1]})
        status, payload = self.api.dispatch(method, u.path, {}, body)
        self._json(status, payload)

    def do_PUT(self):  # noqa: N802
        self._mutate("PUT")

    def do_POST(self):  # noqa: N802
        self._mutate("POST")

    def _static(self, name: str) -> None:
        if not SAFE_FILE.match(name):
            return self._json(400, {"error": "bad name"})
        p = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(p):
            return self._json(404, {"error": "not found"})
        with open(p, "rb") as fh:
            self._send(200, fh.read(), mimetypes.guess_type(p)[0] or "application/octet-stream", "no-cache")


def make_server(cfg: Settings, db_path: str, gatekeeper=None, client=None, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {})
    handler.api = Api(cfg, db_path, gatekeeper)
    handler.client = client
    handler.data_dir = cfg.data_dir
    handler.db_path = db_path
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    return server


def start_web(cfg: Settings, db_path: str, gatekeeper=None, client=None) -> Optional[ThreadingHTTPServer]:
    if not cfg.web_port:
        return None
    server = make_server(cfg, db_path, gatekeeper, client, cfg.web_port)
    t = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    t.start()
    log.info("ダッシュボード: http://0.0.0.0:%d/", cfg.web_port)
    return server
