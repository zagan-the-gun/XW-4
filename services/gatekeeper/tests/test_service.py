"""偽 Frigate に対する結合テスト。偽物は実 Frigate 0.18 の挙動（ファイル名の付け替え、空フォルダの削除、
5 秒キャッシュなし、分類器のクリア）に寄せてある。"""
import pytest

from gatekeeper.config import Settings
from gatekeeper.db import Database
from gatekeeper.service import Gatekeeper

NOW = 2_000_000.0
sizes = {}


class FakeFrigate:
    def __init__(self):
        self.faces = {}          # name -> [files]
        self.train = {}          # file -> True
        self.events = {}         # id -> event dict
        self.sub_labels = {}
        self.registered = []     # (name, filename)
        self.recognize_result = {"success": False, "message": "No face was detected."}
        self.deleted = []
        self.delete_calls = 0
        self.seq = 0
        self.model_building = False   # True: 変更のたびに次の recognize が「構築中」を返す
        self.cleared = False
        self.detect = (1280, 720)

    def _ts(self):
        self.seq += 1
        return f"{NOW + self.seq:.3f}"

    def _clear(self):
        if self.model_building:
            self.cleared = True

    # --- API 互換 ---
    def faces_api(self):
        out = {k: list(v) for k, v in self.faces.items()}
        out["train"] = list(self.train)
        return out

    def detect_size(self, camera):
        return self.detect

    def list_events(self, camera, after=None, before=None, in_progress=0, limit=100, label="person", sort=None):
        evs = [e for e in self.events.values() if e["camera"] == camera]
        evs = [e for e in evs if (e["end_time"] is None) == bool(in_progress)]
        if after is not None:
            evs = [e for e in evs if e["start_time"] > after]
        if before is not None:
            evs = [e for e in evs if e["start_time"] < before]
        return [dict(e) for e in sorted(evs, key=lambda e: e["start_time"], reverse=(sort != "date_asc"))[:limit]]

    def get_event(self, eid):
        e = self.events.get(eid)
        return dict(e) if e else None

    def event_snapshot(self, eid, crop=True, quality=90):
        return b"JPEGDATA" if self.events[eid].get("has_snapshot") else None

    def set_sub_label(self, eid, label, score=None):
        self.sub_labels[eid] = (label, score)
        return True

    similar = {}            # event_id -> [(other_id, distance)]
    semantic_enabled = True

    def similar_events(self, eid, after, before, limit=20):
        if not self.semantic_enabled:
            return None
        return [{"id": o, "search_distance": d, "start_time": self.events.get(o, {}).get("start_time", 0)}
                for o, d in self.similar.get(eid, [])]

    def attempt_image(self, file):
        return b"WEBP:" + file.encode() if file in self.train else None

    def face_image(self, name, file):
        if name == "train":
            return self.attempt_image(file)
        return b"FACE:" + file.encode() if file in self.faces.get(name, []) else None

    def classify_attempt(self, name, file):
        if file not in self.train:
            return False
        self.faces.setdefault(name, []).append(f"{name}-{self._ts()}.webp")
        del self.train[file]
        self._clear()
        return True

    def register_face(self, name, image, filename="face.jpg"):
        self.registered.append((name, filename))
        self.faces.setdefault(name, []).append(f"{name}_{self._ts()}.webp")
        self._clear()
        return {"success": True}

    def recognize(self, image, filename="face.jpg"):
        if self.cleared:
            self.cleared = False
            return {"success": False, "message": "No face was recognized."}
        return dict(self.recognize_result)

    def delete_faces(self, name, ids):
        self.delete_calls += 1
        for i in ids:
            self.deleted.append((name, i))
            if name == "train":
                self.train.pop(i, None)
            elif i in self.faces.get(name, []):
                self.faces[name].remove(i)
        if name != "train" and name in self.faces and not self.faces[name]:
            del self.faces[name]  # 実 Frigate は空フォルダを消す
        self._clear()

    def reclassify(self, name, file, new_name):
        if name == new_name or file not in self.faces.get(name, []):
            return False  # 実 Frigate は 400/404
        self.faces[name].remove(file)
        self.faces.setdefault(new_name, []).append(f"{new_name}-{self._ts()}.webp")
        if not self.faces[name]:
            del self.faces[name]
        self._clear()
        return True


class Client:
    """Gatekeeper が呼ぶ名前に合わせた薄いラッパ。"""

    def __init__(self, fake):
        self.f = fake

    def faces(self):
        return self.f.faces_api()

    def __getattr__(self, item):
        return getattr(self.f, item)


class FakeFaceCheck:
    """image bytes -> (ok, reason)。未指定の画像は合格。"""

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.enabled = True

    def acceptable(self, image, min_px):
        return self.verdicts.get(image, (True, "ok"))


def make(tmp_path, facecheck=None, **cfg):
    fake = FakeFrigate()
    cfg.setdefault("reinforce_per_event", 1)   # 既存テストは補強ありの挙動で書かれている
    cfg.setdefault("merge_score", 0.8)
    cfg.setdefault("reinforce_min_score", 0.85)
    settings = Settings(data_dir=str(tmp_path), min_attempts_new=2, min_face_px=40, new_id_images=3, **cfg)
    db = Database(":memory:")
    gk = Gatekeeper(Client(fake), db, settings, measure=lambda b: sizes.get(b, (100, 100)),
                    now=lambda: NOW, sleep=lambda s: None, facecheck=facecheck)
    return fake, db, gk


def ev(eid, start, end, sub_label=None, has_snapshot=True, camera="entrance", box_h=0.6):
    return {
        "id": eid, "camera": camera, "label": "person", "sub_label": sub_label,
        "start_time": start, "end_time": end, "has_snapshot": has_snapshot, "zones": [],
        "data": {"sub_label_score": 0.95 if sub_label else None, "box": [0.3, 0.1, 0.3, box_h]},
    }


def attempts(fake, eid, names_scores):
    for i, (name, score) in enumerate(names_scores, 1):
        fake.train[f"{eid}-{i}.0-{name}-{score}.webp"] = True


# ------------------------------------------------------------------ 基本経路

def test_bootstrap_registers_first_person_from_snapshot(tmp_path):
    fake, db, gk = make(tmp_path)
    # 登録ゼロのとき、顔があれば実 Frigate は「No face was recognized.」を返す
    fake.recognize_result = {"success": False, "message": "No face was recognized."}
    fake.events["e1"] = ev("e1", NOW - 600, NOW - 590)
    stats = gk.process_once()
    assert stats == {"events": 1, "identified": 0, "new": 1, "none": 0}
    assert fake.registered == [("p0001", "e1.jpg")]
    assert fake.sub_labels["e1"][0] == "p0001"
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "snapshot-new" and v["face_file"] == "faces/e1.jpg"
    assert (tmp_path / "faces" / "e1.jpg").read_bytes() == b"JPEGDATA"
    assert db.persons()[0]["visit_count"] == 1
    assert db.get_state("max_person_seq") == "1"


def test_bootstrap_without_face_does_not_burn_an_id(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.events["e1"] = ev("e1", NOW - 600, NOW - 590)
    fake.events["e2"] = ev("e2", NOW - 500, NOW - 490)
    calls = []

    def recognize(image, filename="face.jpg"):
        calls.append(filename)
        return {"success": False, "message": "No face was detected."} if filename == "e1.jpg" else \
               {"success": False, "message": "No face was recognized."}

    fake.recognize = recognize
    gk.process_once()
    visits = {v["event_id"]: v for v in db.visits()}
    assert visits["e1"]["person_id"] is None and "No face" in visits["e1"]["reason"]
    assert visits["e2"]["person_id"] == "p0001"
    assert fake.registered == [("p0001", "e2.jpg")]


def test_snapshot_recognizes_existing_person_and_reinforces(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["p0001_1.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["e2"] = ev("e2", NOW - 600, NOW - 590)
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "snapshot" and v["score"] == 0.9
    assert ("p0001", "e2.jpg") in fake.registered


def test_snapshot_unknown_face_creates_new_id(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.4}
    fake.events["e3"] = ev("e3", NOW - 600, NOW - 590)
    gk.process_once()
    assert db.visits()[0]["person_id"] == "p0002"
    assert ("p0002", "e3.jpg") in fake.registered


def test_snapshot_without_face_is_recorded_as_none(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e4"] = ev("e4", NOW - 600, NOW - 590)
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and v["method"] == "none" and "No face" in v["reason"]
    assert fake.registered == []


def test_small_person_box_skips_snapshot_path(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["e12"] = ev("e12", NOW - 600, NOW - 590, box_h=0.2)  # 0.2 * 720 = 144 px < 250
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "too small" in v["reason"]
    assert fake.registered == []


def test_attempts_create_new_id_and_cleanup(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e5"] = ev("e5", NOW - 600, NOW - 590)
    attempts(fake, "e5", [("unknown", 0.2), ("unknown", 0.3), ("unknown", 0.1), ("unknown", 0.1)])
    sizes.clear()
    sizes[b"WEBP:e5-4.0-unknown-0.1.webp"] = (20, 20)  # 小さすぎる
    gk.process_once()
    sizes.clear()
    v = db.visits()[0]
    assert v["person_id"] == "p0002" and v["method"] == "new"
    assert len(fake.faces["p0002"]) == 3
    assert fake.train == {} and fake.delete_calls == 1
    assert ("train", "e5-4.0-unknown-0.1.webp") in fake.deleted
    assert fake.sub_labels["e5"] == ("p0002", None)
    assert v["face_file"] == "faces/e5.webp"
    assert db.get_state("max_person_seq") == "2"


def test_new_id_is_not_committed_when_nothing_could_be_classified(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e5"] = ev("e5", NOW - 600, NOW - 590)
    attempts(fake, "e5", [("unknown", 0.2), ("unknown", 0.3)])
    fake.classify_attempt = lambda name, f: False
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and v["method"] == "none"
    assert "p0002" not in fake.faces and "p0002" not in db.person_ids()
    assert db.get_state("max_person_seq") is None


def test_frigate_assigned_event_is_recorded_without_relabel(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e6"] = ev("e6", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e6", [("p0001", 0.95)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "frigate" and v["score"] == 0.95
    assert "e6" not in fake.sub_labels
    assert len(fake.faces["p0001"]) == 2 and fake.train == {}


def test_reinforce_respects_max_images(tmp_path):
    fake, db, gk = make(tmp_path, max_images_per_person=1)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e7"] = ev("e7", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e7", [("p0001", 0.95)])
    gk.process_once()
    assert fake.faces["p0001"] == ["x.webp"]
    assert fake.train == {}


def test_reinforce_failure_keeps_identity(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e7"] = ev("e7", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e7", [("p0001", 0.95)])

    def boom(name, f):
        raise RuntimeError("502")

    fake.classify_attempt = boom
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "frigate"


def test_train_deletes_are_batched_per_pass(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    for e in ("a", "b"):
        fake.events[e] = ev(e, NOW - 600, NOW - 590, sub_label="p0001")
        attempts(fake, e, [("p0001", 0.5), ("p0001", 0.6)])  # 補強には低すぎる → 残骸
    gk.process_once()
    assert fake.train == {} and fake.delete_calls == 1


def test_orphan_attempts_are_deleted_when_event_gone(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.train["gone-1.0-unknown-0.2.webp"] = True  # timestamp 1.0 は十分古い
    gk.process_once()
    assert fake.train == {}


def test_missing_attempt_images_fall_back_to_snapshot(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["e10"] = ev("e10", NOW - 600, NOW - 590)
    fake.faces_api = lambda: {"p0001": ["x.webp"], "train": ["e10-1.0-unknown-0.2.webp", "e10-2.0-unknown-0.2.webp"]}
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "snapshot"


# ------------------------------------------------------------ 判定の細部

def test_vote_tie_is_not_attributed(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.faces["p0002"] = ["y.webp"]
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    attempts(fake, "e", [("p0001", 0.85), ("p0002", 0.85)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "tie" in v["reason"]


def test_person_cap_blocks_new_ids(tmp_path):
    fake, db, gk = make(tmp_path, max_persons=1)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "cap" in v["reason"] and fake.registered == []


# ------------------------------------------------------ 取りこぼし・再開・再試行

def test_in_progress_event_is_processed_after_it_ends(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["done"] = ev("done", NOW - 300, NOW - 290)
    fake.events["live"] = ev("live", NOW - 500, None)
    attempts(fake, "live", [("unknown", 0.2), ("unknown", 0.2)])
    gk.process_once()
    assert len(fake.train) == 2                       # 進行中の試行画像は残す
    assert "live" in db.get_json("deferred", {})
    fake.events["live"]["end_time"] = NOW - 100
    stats = gk.process_once()
    assert stats["events"] == 1 and db.has_visit("live")
    assert db.visits("p0002")[0]["event_id"] == "live"
    assert "live" not in db.get_json("deferred", {})


def test_event_that_ends_during_a_pass_is_not_lost(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["X"] = ev("X", NOW - 500, None)
    fake.events["Y"] = ev("Y", NOW - 300, NOW - 290)
    orig = fake.event_snapshot

    def snapshot(eid, crop=True, quality=90):
        fake.events["X"]["end_time"] = NOW - 200  # Y の処理中に X が終わる
        return orig(eid)

    fake.event_snapshot = snapshot
    gk.process_once()
    fake.event_snapshot = orig
    gk.process_once()
    assert db.has_visit("X") and db.has_visit("Y")


def test_short_event_between_passes_is_covered_by_overlap(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["a"] = ev("a", NOW - 300, NOW - 290)
    gk.process_once()
    assert float(db.get_state("cursor")) == NOW - 300
    # カーソルより前に始まり、周期の合間に始まって終わった短いイベント
    fake.events["b"] = ev("b", NOW - 310, NOW - 305)
    gk.process_once()
    assert db.has_visit("b")


def test_same_start_time_twins_across_page_boundary(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    for i in range(99):
        fake.events[f"e{i}"] = ev(f"e{i}", NOW - 3600 + i, NOW - 3600 + i + 0.5)
    t = NOW - 3600 + 99
    fake.events["twin-a"] = ev("twin-a", t, t + 1)
    fake.events["twin-b"] = ev("twin-b", t, t + 1)
    stats = gk.process_once()
    assert stats["events"] == 101 and db.has_visit("twin-a") and db.has_visit("twin-b")


def test_long_lived_event_does_not_block_newer_events(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["stuck"] = ev("stuck", NOW - 3000, None)
    fake.events["n1"] = ev("n1", NOW - 200, NOW - 190)
    gk.process_once()
    assert float(db.get_state("cursor")) == NOW - 200
    fake.events["n2"] = ev("n2", NOW - 100, NOW - 90)
    stats = gk.process_once()
    assert stats["events"] == 1 and db.has_visit("n2")
    assert "stuck" in db.get_json("deferred", {})


def test_read_failure_is_retried_next_pass(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["bad"] = ev("bad", NOW - 700, NOW - 690)
    fake.events["good"] = ev("good", NOW - 600, NOW - 590)
    orig = fake.event_snapshot
    fake.event_snapshot = lambda eid, crop=True, quality=90: (_ for _ in ()).throw(RuntimeError("502")) if eid == "bad" else orig(eid)
    stats = gk.process_once()
    assert stats["errors"] == 1 and stats["events"] == 1
    assert not db.has_visit("bad") and db.has_visit("good")
    fake.event_snapshot = orig
    assert gk.process_once()["events"] == 1
    assert db.has_visit("bad")


def test_failure_after_registration_resumes_with_same_id(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["bad"] = ev("bad", NOW - 600, NOW - 590)
    orig_register = fake.register_face

    def register_then_crash(name, image, filename="face.jpg"):
        res = orig_register(name, image, filename)
        raise RuntimeError("frigate restarted")

    fake.register_face = register_then_crash
    stats = gk.process_once()
    assert stats["errors"] == 1
    assert db.visit_pending("bad")["person_id"] == "p0002"
    # 再開: 登録済みの p0002 と照合できるので同じ ID で完了し、別 ID は作らない
    fake.register_face = orig_register
    fake.recognize_result = {"success": True, "face_name": "p0002", "score": 0.95}
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0002" and v["method"] == "snapshot"
    assert "p0003" not in fake.faces and db.persons()[-1]["visit_count"] == 1


def test_classifier_still_building_is_retried_within_pass(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.model_building = True
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["e8"] = ev("e8", NOW - 600, NOW - 590)
    fake.cleared = True  # 直前の変更で構築中
    stats = gk.process_once()
    assert stats["events"] == 1 and db.visits()[0]["person_id"] == "p0001"


def test_retry_gives_up_after_limit(tmp_path):
    fake, db, gk = make(tmp_path, retry_limit=2)
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": False, "message": "No face was recognized."}
    fake.events["e11"] = ev("e11", NOW - 600, NOW - 590)
    assert gk.process_once().get("retry") == 1
    assert gk.process_once().get("retry") == 1
    stats = gk.process_once()
    assert stats["none"] == 1 and "retry limit" in db.visits()[0]["reason"]
    assert "e11" not in db.get_json("deferred", {})


def test_persistent_error_gives_up_after_limit(tmp_path):
    fake, db, gk = make(tmp_path, retry_limit=1)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    fake.event_snapshot = lambda eid, crop=True, quality=90: (_ for _ in ()).throw(RuntimeError("500"))
    gk.process_once()
    stats = gk.process_once()
    assert stats["events"] == 1 and db.visits()[0]["method"] == "none"


def test_events_are_paged_oldest_first_without_skipping(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    for i in range(150):
        fake.events[f"e{i}"] = ev(f"e{i}", NOW - 3600 + i * 10, NOW - 3600 + i * 10 + 5)
    stats = gk.process_once()
    assert stats["events"] == 150 and len(db.visits(limit=1000)) == 150


# ------------------------------------------------------------ 統合・別名・保守

def test_merge_moves_images_and_visits_and_records_alias(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    fake.faces["p0002"] = ["b.webp", "c.webp"]
    db.ensure_person("p0001"); db.ensure_person("p0002")
    db.record_visit("e1", "entrance", "p0002", NOW - 100, NOW - 90, "new")
    db.record_visit("e2", "entrance", "p0001", NOW - 50, NOW - 40, "frigate")
    res = gk.merge("p0002", "p0001")
    assert res == {"images_moved": 2, "visits_moved": 1}
    assert "p0002" not in fake.faces and len(fake.faces["p0001"]) == 3
    assert {v["person_id"] for v in db.visits()} == {"p0001"}
    assert [p["id"] for p in db.persons()] == ["p0001"] and db.persons()[0]["visit_count"] == 2
    assert fake.sub_labels["e1"][0] == "p0001"
    assert db.resolve_alias("p0002") == "p0001"


def test_merge_same_or_unknown_id_is_rejected(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    db.ensure_person("p0001")
    with pytest.raises(ValueError):
        gk.merge("p0001", "p0001")
    with pytest.raises(ValueError):
        gk.merge("p0009", "p0001")
    with pytest.raises(ValueError):
        gk.merge("p0001", "typo")
    assert fake.faces["p0001"] == ["a.webp"] and db.person_ids() == {"p0001"}


def test_merge_survives_delete_failure(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    fake.faces["p0002"] = ["b.webp"]
    db.ensure_person("p0002")
    db.record_visit("e1", "entrance", "p0002", NOW - 100, NOW - 90, "new")
    orig = fake.reclassify
    fake.reclassify = lambda name, f, new: (orig(name, f, new), fake.faces.setdefault(name, []).append("ghost.webp"))[0]

    def boom(name, ids):
        raise RuntimeError("500")

    fake.delete_faces = boom
    res = gk.merge("p0002", "p0001")
    assert res["images_moved"] == 1 and res["visits_moved"] == 1
    assert db.visits()[0]["person_id"] == "p0001"


def test_merged_id_is_never_reissued(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    fake.faces["p0002"] = ["b.webp"]
    db.ensure_person("p0001"); db.ensure_person("p0002")
    gk.merge("p0002", "p0001")
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["e9"] = ev("e9", NOW - 600, NOW - 590)
    gk.process_once()
    assert db.visits()[0]["person_id"] == "p0003"


def test_old_labels_of_merged_id_map_to_target(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    fake.faces["p0002"] = ["b.webp"]
    db.ensure_person("p0001"); db.ensure_person("p0002")
    gk.merge("p0002", "p0001")
    # 統合前に Frigate が付けたラベル / 試行画像の名前が p0002 のまま届く
    fake.events["e1"] = ev("e1", NOW - 600, NOW - 590, sub_label="p0002")
    fake.events["e2"] = ev("e2", NOW - 500, NOW - 490)
    attempts(fake, "e2", [("p0002", 0.9), ("p0002", 0.88)])
    gk.process_once()
    visits = {v["event_id"]: v for v in db.visits()}
    assert visits["e1"]["person_id"] == "p0001" and visits["e1"]["method"] == "frigate"
    assert visits["e2"]["person_id"] == "p0001" and visits["e2"]["method"] == "attempts"
    assert "p0003" not in fake.faces


def test_prune_removes_old_face_copies_and_expired_persons(tmp_path):
    fake, db, gk = make(tmp_path, retain_days=1)
    import os
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir()
    old_file = faces_dir / "old.jpg"; old_file.write_bytes(b"x")
    os.utime(old_file, (NOW - 3 * 86400, NOW - 3 * 86400))
    new_file = faces_dir / "new.jpg"; new_file.write_bytes(b"y")
    os.utime(new_file, (NOW - 100, NOW - 100))
    orphan = faces_dir / "orphan.jpg"; orphan.write_bytes(b"z")
    os.utime(orphan, (NOW - 3 * 86400, NOW - 3 * 86400))
    db.record_visit("old", "entrance", "p0001", NOW - 3 * 86400, NOW - 3 * 86400 + 10, "snapshot-new", face_file="faces/old.jpg")
    db.record_visit("new", "entrance", "p0002", NOW - 100, NOW - 90, "snapshot-new", face_file="faces/new.jpg")
    fake.faces["p0001"] = ["a.webp"]; fake.faces["p0002"] = ["b.webp"]
    db.ensure_person("p0001", created_at=NOW - 3 * 86400); db.refresh_person("p0001")
    db.ensure_person("p0002"); db.refresh_person("p0002")
    gk.process_once()
    assert not old_file.exists() and not orphan.exists() and new_file.exists()
    files = {v["event_id"]: v["face_file"] for v in db.visits()}
    assert files == {"old": None, "new": "faces/new.jpg"}
    # 一度しか来ていない古い p0001 は Frigate からも消える。p0002 は最近なので残る
    assert "p0001" not in fake.faces and "p0001" not in db.person_ids()
    assert "p0002" in fake.faces


def test_next_person_id_considers_frigate_names_and_visits():
    db = Database(":memory:")
    assert db.peek_person_id("p", 4, ["p0007", "train", "john"]) == "p0008"
    db.ensure_person("p0010")
    assert db.peek_person_id("p", 4, []) == "p0011"
    db.record_visit("e", "cam", "p0020", 1.0, 2.0, "new")   # 既存 DB の訪問履歴からも復元
    assert db.peek_person_id("p", 4, []) == "p0021"


# ------------------------------------------------------------ 顔品質チェック・purge

def test_facecheck_blocks_snapshot_registration_of_back_of_head(tmp_path):
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheck({b"JPEGDATA": (False, "face landmarks implausible")}))
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "implausible" in v["reason"] and fake.registered == []


def test_facecheck_allows_good_snapshot(tmp_path):
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheck())
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "unknown", "score": 0.3}
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    gk.process_once()
    assert db.visits()[0]["person_id"] == "p0002"


def test_facecheck_blocks_new_id_from_junk_attempts(tmp_path):
    bad = {b"WEBP:e5-1.0-unknown-0.2.webp": (False, "no face"), b"WEBP:e5-2.0-unknown-0.3.webp": (False, "no face")}
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheck(bad))
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e5"] = ev("e5", NOW - 600, NOW - 590)
    attempts(fake, "e5", [("unknown", 0.2), ("unknown", 0.3)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "attempts: no face" in v["reason"]
    assert "p0002" not in fake.faces and fake.train == {}


def test_purge_removes_person_and_clears_visits(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]
    fake.faces["p0002"] = ["b.webp", "c.webp"]
    db.ensure_person("p0001"); db.ensure_person("p0002")
    db.record_visit("e1", "entrance", "p0002", NOW - 100, NOW - 90, "snapshot-new", face_file="faces/e1.jpg")
    res = gk.purge("p0002")
    assert res == {"images_deleted": 2, "visits_cleared": 1}
    assert "p0002" not in fake.faces and "p0002" not in db.person_ids()
    v = db.visits()[0]
    assert v["person_id"] is None and v["method"] == "none" and "purged" in v["reason"] and v["face_file"] == "faces/e1.jpg"
    assert fake.sub_labels["e1"] == ("", None)
    assert db.peek_person_id("p", 4, []) == "p0003"   # 番号は再利用しない
    with pytest.raises(ValueError):
        gk.purge("p0009")


# ------------------------------------------------------------ 性別推定

class FakeGender:
    enabled = True

    def __init__(self, result):
        self.result = result

    def estimate(self, image, box):
        return self.result


class FakeFaceCheckWithBox(FakeFaceCheck):
    def inspect(self, image, min_px):
        ok, why = self.acceptable(image, min_px)
        from gatekeeper.facecheck import FaceInfo
        return ok, why, (FaceInfo(80, 100, 0.9, True, (1, 2, 80, 100)) if ok else None)


def test_gender_is_estimated_and_stored_for_quality_faces(tmp_path):
    from gatekeeper.gender import GenderResult
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheckWithBox(), )
    gk.gender = FakeGender(GenderResult("male", 0.91, 34))
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    gk.process_once()
    v = db.visits()[0]
    assert v["gender"] == "male" and v["gender_score"] == 0.91 and v["age"] == 34


def test_low_confidence_gender_is_not_stored(tmp_path):
    from gatekeeper.gender import GenderResult
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheckWithBox())
    gk.gender = FakeGender(GenderResult("female", 0.6, 30))
    fake.faces["p0001"] = ["x.webp"]
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.9}
    fake.events["e"] = ev("e", NOW - 600, NOW - 590)
    gk.process_once()
    assert db.visits()[0]["gender"] is None


def test_backfill_gender(tmp_path):
    from gatekeeper.gender import GenderResult
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheckWithBox({b"BAD": (False, "no face")}))
    gk.gender = FakeGender(GenderResult("male", 0.95, 40))
    (tmp_path / "faces").mkdir()
    (tmp_path / "faces" / "good.jpg").write_bytes(b"GOOD")
    (tmp_path / "faces" / "bad.jpg").write_bytes(b"BAD")
    db.record_visit("g", "entrance", "p0001", NOW - 100, NOW - 90, "snapshot", face_file="faces/good.jpg")
    db.record_visit("b", "entrance", None, NOW - 80, NOW - 70, "none", face_file="faces/bad.jpg")
    db.record_visit("n", "entrance", None, NOW - 60, NOW - 50, "none")
    assert gk.backfill_gender() == {"visits": 2, "estimated": 1}
    got = {v["event_id"]: v["gender"] for v in db.visits()}
    assert got == {"g": "male", "b": None, "n": None}


def test_reinforcement_disabled_by_default(tmp_path):
    fake, db, gk = make(tmp_path, reinforce_per_event=0)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e6"] = ev("e6", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e6", [("p0001", 0.99)])
    fake.recognize_result = {"success": True, "face_name": "p0001", "score": 0.99}
    fake.events["e7"] = ev("e7", NOW - 500, NOW - 490)
    gk.process_once()
    assert fake.faces["p0001"] == ["x.webp"] and fake.registered == [] and fake.train == {}
    assert {v["person_id"] for v in db.visits()} == {"p0001"}


def test_clean_library_deletes_failing_images_but_keeps_best(tmp_path):
    verdicts = {b"FACE:bad1.webp": (False, "no face"), b"FACE:bad2.webp": (False, "no face"),
                b"FACE:only.webp": (False, "no face")}
    fc = FakeFaceCheckWithBox(verdicts)
    fake, db, gk = make(tmp_path, facecheck=fc)
    fake.faces["p0001"] = ["good.webp", "bad1.webp", "bad2.webp"]
    fake.faces["p0002"] = ["only.webp"]
    rep = gk.clean_library(dry_run=True)
    assert rep["p0001"] == {"total": 3, "kept": 1, "deleted": 2} and fake.faces["p0001"] == ["good.webp", "bad1.webp", "bad2.webp"]
    rep = gk.clean_library()
    assert fake.faces["p0001"] == ["good.webp"]
    assert fake.faces["p0002"] == ["only.webp"] and rep["p0002"]["deleted"] == 0


def test_refresh_person_recomputes_from_visits():
    db = Database(":memory:")
    db.ensure_person("p0001")
    db.record_visit("a", "cam", "p0001", 100.0, 110.0, "frigate")
    db.record_visit("b", "cam", "p0001", 200.0, 210.0, "frigate")
    db.refresh_person("p0001")
    assert db.persons()[0]["visit_count"] == 2 and db.persons()[0]["last_seen"] == 210.0
    # 後の訪問を取り消すと最終訪問も戻る
    db.record_visit("b", "cam", None, 200.0, 210.0, "none", reason="reset")
    db.refresh_person("p0001")
    assert db.persons()[0]["visit_count"] == 1 and db.persons()[0]["last_seen"] == 110.0


def test_frigate_match_without_quality_face_is_not_trusted(tmp_path):
    bad = {b"WEBP:e6-1.0-p0001-0.99.webp": (False, "no face")}
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheck(bad))
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e6"] = ev("e6", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e6", [("p0001", 0.99)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] is None and "unverified frigate match p0001" in v["reason"]
    assert fake.train == {}


def test_attempts_match_with_quality_face_is_trusted(tmp_path):
    fake, db, gk = make(tmp_path, facecheck=FakeFaceCheck())
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e6"] = ev("e6", NOW - 600, NOW - 590)
    attempts(fake, "e6", [("p0001", 0.9), ("p0001", 0.88)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["method"] == "attempts"


# ------------------------------------------------------------ 見た目による紐付け

def test_appearance_links_unidentified_visit_to_identified_person(tmp_path):
    fake, db, gk = make(tmp_path, reinforce_per_event=0)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["known"] = ev("known", NOW - 3000, NOW - 2990, sub_label="p0001")
    attempts(fake, "known", [("p0001", 0.99)])
    fake.events["back"] = ev("back", NOW - 2000, NOW - 1990)   # 顔なし
    fake.similar = {"back": [("known", 0.08), ("other", 0.20)]}
    stats = gk.process_once()
    v = db.visit("back")
    assert v["person_id"] is None and v["appearance_person"] == "p0001" and v["appearance_ref"] == "known"
    assert abs(v["appearance_score"] - 0.92) < 0.001 and v["appearance_checked"] == 1
    assert stats["appearance"] == 1
    # 顔ライブラリには何も追加しない
    assert fake.faces["p0001"] == ["x.webp"] and fake.registered == []


def test_appearance_groups_unidentified_visits_together(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 5000, NOW - 4990)
    fake.events["b"] = ev("b", NOW - 3000, NOW - 2990)
    fake.events["c"] = ev("c", NOW - 1000, NOW - 990)
    fake.similar = {"a": [], "b": [("a", 0.08)], "c": [("b", 0.07), ("a", 0.08)]}
    gk.process_once()
    a, b, c = db.visit("a"), db.visit("b"), db.visit("c")
    assert a["appearance_group"] == b["appearance_group"] == c["appearance_group"] == "a0001"
    assert a["appearance_checked"] == 1 and c["appearance_ref"] == "b"


def test_appearance_group_requires_similarity_to_anchor(tmp_path):
    """A≈B, B≈C でも A と C が似ていなければ C はグループに入れない（数珠つなぎ防止）。"""
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 5000, NOW - 4990)
    fake.events["b"] = ev("b", NOW - 3000, NOW - 2990)
    fake.events["c"] = ev("c", NOW - 1000, NOW - 990)
    fake.similar = {"a": [], "b": [("a", 0.08)], "c": [("b", 0.07), ("a", 0.20)]}
    gk.process_once()
    assert db.visit("b")["appearance_group"] == "a0001"
    c = db.visit("c")
    assert c["appearance_group"] is None and c["appearance_checked"] == 1


def test_appearance_person_link_only_from_face_identified_visit(tmp_path):
    """推定同士の連鎖で人物に流れない: 相手が「見た目で推定」なだけなら人物には紐付けない。"""
    fake, db, gk = make(tmp_path, reinforce_per_event=0)
    fake.faces["p0001"] = ["x.webp"]
    db.record_visit("est", "entrance", None, NOW - 5000, NOW - 4990, "none"); db.set_appearance("est", "p0001", None, 0.9, "z")
    fake.events["est"] = ev("est", NOW - 5000, NOW - 4990)
    fake.events["n"] = ev("n", NOW - 1000, NOW - 990)
    fake.similar = {"n": [("est", 0.05)]}
    gk.process_once()
    n = db.visit("n")
    assert n["appearance_person"] is None and n["appearance_group"] is None and n["appearance_checked"] == 1


def test_relink_resets_and_redoes(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 5000, NOW - 4990)
    fake.events["b"] = ev("b", NOW - 3000, NOW - 2990)
    fake.similar = {"b": [("a", 0.08)]}
    gk.process_once()
    assert db.visit("b")["appearance_group"] == "a0001"
    assert gk.relink_appearance(24) == 2
    assert db.visit("b")["appearance_group"] is None and db.visit("b")["appearance_checked"] == 0
    fake.similar = {"b": [("a", 0.30)]}
    gk.process_once()
    assert db.visit("b")["appearance_group"] is None and db.visit("b")["appearance_checked"] == 1


def test_appearance_ignores_far_or_pending_candidates(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 3000, NOW - 2990)
    fake.events["b"] = ev("b", NOW - 1000, NOW - 990)
    fake.similar = {"b": [("a", 0.25), ("nonexistent", 0.01)]}
    gk.process_once()
    b = db.visit("b")
    assert b["appearance_group"] is None and b["appearance_person"] is None and b["appearance_checked"] == 1


def test_appearance_waits_until_embedding_is_ready_and_disables_when_unavailable(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["fresh"] = ev("fresh", NOW - 100, NOW - 30)  # 終了から 60 秒未満
    gk.process_once()
    assert db.visit("fresh")["appearance_checked"] == 0
    fake.semantic_enabled = False
    fake.events["old"] = ev("old", NOW - 95, NOW - 70)  # カーソル以降で、終了から 60 秒以上
    gk.process_once()
    assert db.visit("old")["appearance_checked"] == 0 and gk._appearance_disabled is True


def test_merge_and_purge_follow_appearance_links(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["a.webp"]; fake.faces["p0002"] = ["b.webp"]
    db.ensure_person("p0001"); db.ensure_person("p0002")
    db.record_visit("e", "entrance", None, NOW - 100, NOW - 90, "none")
    db.set_appearance("e", "p0002", None, 0.9, "x")
    gk.merge("p0002", "p0001")
    assert db.visit("e")["appearance_person"] == "p0001"
    gk.purge("p0001")
    assert db.visit("e")["appearance_person"] is None


def test_group_is_promoted_when_a_member_is_later_linked_to_a_person(tmp_path):
    fake, db, gk = make(tmp_path, reinforce_per_event=0, appearance_batch=1)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 6000, NOW - 5990)                      # 未特定
    fake.events["b"] = ev("b", NOW - 4000, NOW - 3990)                      # 未特定
    fake.events["k"] = ev("k", NOW - 2000, NOW - 1990, sub_label="p0001")   # 顔で特定
    attempts(fake, "k", [("p0001", 0.99)])
    fake.similar = {"a": [("b", 0.08)], "b": [("k", 0.09), ("a", 0.08)]}
    gk.process_once()   # a を確認 → b とグループ a0001
    assert db.visit("a")["appearance_group"] == "a0001" and db.visit("b")["appearance_group"] == "a0001"
    gk.process_once()   # b を確認 → k(p0001) と一致 → グループごと p0001 に格上げ
    assert db.visit("b")["appearance_person"] == "p0001"
    assert db.visit("a")["appearance_person"] == "p0001" and db.visit("a")["appearance_group"] == "a0001"


def test_group_threshold_is_stricter_than_person_threshold(tmp_path):
    fake, db, gk = make(tmp_path)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["a"] = ev("a", NOW - 5000, NOW - 4990)
    fake.events["b"] = ev("b", NOW - 3000, NOW - 2990)
    fake.similar = {"b": [("a", 0.10)]}   # 人物なら許容、未特定同士では厳しすぎる
    gk.process_once()
    assert db.visit("b")["appearance_group"] is None and db.visit("b")["appearance_checked"] == 1


def test_face_copy_is_kept_for_matches_even_without_reinforcement(tmp_path):
    fake, db, gk = make(tmp_path, reinforce_per_event=0)
    fake.faces["p0001"] = ["x.webp"]
    fake.events["e6"] = ev("e6", NOW - 600, NOW - 590, sub_label="p0001")
    attempts(fake, "e6", [("p0001", 0.95)])
    gk.process_once()
    v = db.visits()[0]
    assert v["person_id"] == "p0001" and v["face_file"] == "faces/e6.webp"
    assert fake.faces["p0001"] == ["x.webp"] and fake.train == {}


def test_person_ids_do_not_run_out_at_four_digits():
    db = Database(":memory:")
    db.set_state("max_person_seq", "9999")
    pid = db.peek_person_id("p", 4, [])
    assert pid == "p10000"
    db.commit_person_seq(pid, "p")
    assert db.peek_person_id("p", 4, []) == "p10001"
    assert db.expired_persons("p", 0, 1) == []  # 5 桁でも ID の形式として扱える
    db.ensure_person("p10000"); db.refresh_person("p10000")
    assert db.expired_persons("p", 10**12, 1) == ["p10000"]


# ------------------------------------------------------------ 危険度アラート

class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, description, level, fields, image=None, link=None):
        self.sent.append((title, level, dict(fields), image))
        return True


def test_alert_is_sent_for_loitering_stranger_and_not_repeated(tmp_path):
    fake, db, gk = make(tmp_path)
    gk.notifier = FakeNotifier()
    fake.faces["p0001"] = ["x.webp"]
    fake.events["s"] = ev("s", NOW - 400, NOW - 250)   # 150 秒滞在の未特定（昼夜は NOW 依存なので滞在だけで中以上にする）
    stats = gk.process_once()
    a = db.get_alert("s")
    assert a is not None and a["level"] in ("medium", "high") and a["notified_at"] is not None and stats.get("alerts") == 1
    assert gk.notifier.sent[0][3] == b"JPEGDATA" and "滞在 150 秒" in gk.notifier.sent[0][2]["理由"]
    # 同じ来訪は再通知しない
    assert gk.process_once().get("alerts", 0) == 0 and db.get_alert("s")["notify_count"] == 1


def test_short_passerby_is_recorded_low_without_notification(tmp_path):
    fake, db, gk = make(tmp_path)
    gk.notifier = FakeNotifier()
    fake.faces["p0001"] = ["x.webp"]
    fake.events["q"] = ev("q", NOW - 300, NOW - 295)
    gk.process_once()
    a = db.get_alert("q")
    assert a is not None and a["level"] == "low" and a["notified_at"] is None and gk.notifier.sent == []


def test_alert_escalates_while_visit_continues(tmp_path):
    fake, db, gk = make(tmp_path)
    gk.notifier = FakeNotifier()
    fake.faces["p0001"] = ["x.webp"]
    fake.events["live"] = ev("live", NOW - 40, None)   # 進行中、まだ 40 秒
    gk.process_once()
    assert gk.notifier.sent == [] and (db.get_alert("live") or {}).get("level", "low") == "low"
    gk.now = lambda: NOW + 200                          # 240 秒経過してもまだ居る
    gk.process_once()
    a = db.get_alert("live")
    assert a["level"] in ("medium", "high") and a["notified_at"] is not None and len(gk.notifier.sent) == 1
    assert "継続中" in gk.notifier.sent[0][2]["滞在"]


def test_named_person_never_alerts(tmp_path):
    fake, db, gk = make(tmp_path)
    gk.notifier = FakeNotifier()
    fake.faces["p0001"] = ["x.webp"]
    db.ensure_person("p0001"); db.rename_person("p0001", "田中")
    fake.events["k"] = ev("k", NOW - 900, NOW - 300, sub_label="p0001")
    attempts(fake, "k", [("p0001", 0.99)])
    gk.process_once()
    assert db.get_alert("k") is None and gk.notifier.sent == []


def test_alert_cooldown_per_subject(tmp_path):
    fake, db, gk = make(tmp_path, alert_cooldown_seconds=600)
    gk.notifier = FakeNotifier()
    fake.faces["p0001"] = ["x.webp"]
    db.ensure_person("p0009")
    fake.events["a"] = ev("a", NOW - 1500, NOW - 1300, sub_label="p0009")
    attempts(fake, "a", [("p0009", 0.99)])
    fake.faces["p0009"] = ["y.webp"]
    gk.process_once()
    assert len(gk.notifier.sent) == 1
    fake.events["b"] = ev("b", NOW - 400, NOW - 200, sub_label="p0009")
    attempts(fake, "b", [("p0009", 0.99)])
    gk.process_once()
    assert len(gk.notifier.sent) == 1 and db.get_alert("b")["notified_at"] is None
