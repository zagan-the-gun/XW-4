import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from gatekeeper.config import Settings
from gatekeeper.db import Database
from gatekeeper.web import Api, make_server, period_start

NOW = time.time()  # 集計は暦日基準なので実時刻で作る


class FakeGk:
    def __init__(self):
        self.calls = []
        self.lock = threading.RLock()

    def merge(self, src, dst):
        self.calls.append(("merge", src, dst))
        return {"images_moved": 1, "visits_moved": 2}

    def purge(self, pid):
        self.calls.append(("purge", pid))
        return {"images_deleted": 1, "visits_cleared": 1}


class FakeClient:
    def event_snapshot(self, eid, crop=True, quality=90):
        return b"JPG" if eid == "1.0-abc" else None

    def event_clip_stream(self, eid, chunk_size=1024):
        return iter([b"MP4-", b"DATA"]) if eid == "1.0-abc" else None


def seed(path):
    db = Database(path)
    db.ensure_person("p0001"); db.rename_person("p0001", "田中")
    db.record_visit("1.0-abc", "entrance", "p0001", NOW - 100, NOW - 90, "frigate", score=0.9, face_file="faces/a.jpg")
    db.set_visit_gender("1.0-abc", "male", 0.9, 30)
    db.record_visit("1.1-def", "entrance", None, NOW - 50, NOW - 45, "none", reason="no face")
    db.record_visit("1.2-ghi", "entrance", "p0001", NOW - 3 * 86400, NOW - 3 * 86400 + 5, "snapshot")
    db.refresh_person("p0001")
    return db


def api(tmp_path, gk=None):
    p = str(tmp_path / "g.db")
    seed(p)
    cfg = Settings(data_dir=str(tmp_path))
    return Api(cfg, p, gk, now=lambda: NOW)


def test_summary_traffic_heatmap(tmp_path):
    a = api(tmp_path)
    st, s = a.dispatch("GET", "/api/summary", {"days": "7"}, None)
    assert st == 200 and s["events"] == 3 and s["visits"] == 3 and s["identified_visits"] == 2 and s["persons"] == 1
    assert s["gender"] == {"male": 1, "female": 0, "unknown": 2}
    assert s["today_events"] == 2 and s["today_visits"] == 2 and s["tz"] and s["today"]
    st, t = a.dispatch("GET", "/api/traffic", {"days": "7"}, None)
    assert st == 200 and len(t["daily"]) == 7 and sum(d["events"] for d in t["daily"]) == 3 and len(t["hourly_today"]) == 24
    # KPI と日別グラフは同じ暦日の窓
    assert sum(d["visits"] for d in t["daily"]) == s["visits"]
    st, h = a.dispatch("GET", "/api/heatmap", {}, None)
    assert st == 200 and sum(map(sum, h["all"])) == 3 and sum(map(sum, h["identified"])) == 2


def test_summary_period_is_calendar_aligned(tmp_path):
    a = api(tmp_path)
    st, s = a.dispatch("GET", "/api/summary", {"days": "1"}, None)
    assert s["period_start"] == period_start(NOW, 1) and s["events"] == 2  # 3 日前の分は含まない


def test_param_validation(tmp_path):
    a = api(tmp_path)
    assert a.dispatch("GET", "/api/summary", {"days": "0"}, None)[0] == 400
    assert a.dispatch("GET", "/api/summary", {"days": "700000"}, None)[0] == 400
    assert a.dispatch("GET", "/api/traffic", {"days": "abc"}, None)[0] == 400
    assert a.dispatch("GET", "/api/visits", {"limit": "-5"}, None)[0] == 400
    assert a.dispatch("GET", "/api/visits", {"limit": "5000"}, None)[0] == 400
    assert a.dispatch("GET", "/api/visits", {"person": "bad id"}, None)[0] == 400
    assert a.dispatch("GET", "/api/visits", {"limit": "500"}, None)[0] == 200


def test_persons_and_visits(tmp_path):
    a = api(tmp_path)
    st, p = a.dispatch("GET", "/api/persons", {}, None)
    assert st == 200 and p["persons"][0]["id"] == "p0001" and p["persons"][0]["display_name"] == "田中"
    assert p["persons"][0]["gender"] == "male" and p["persons"][0]["face_file"] == "faces/a.jpg"
    st, v = a.dispatch("GET", "/api/visits", {"limit": "10"}, None)
    assert st == 200 and [x["event_id"] for x in v["visits"]] == ["1.1-def", "1.0-abc", "1.2-ghi"]
    assert v["visits"][1]["display_name"] == "田中" and v["visits"][1]["gender"] == "male" and v["visits"][1]["gender_score"] == 0.9
    st, v = a.dispatch("GET", "/api/visits", {"person": "p0001"}, None)
    assert st == 200 and len(v["visits"]) == 2


def test_visits_limit_counts_whole_visits(tmp_path):
    p = str(tmp_path / "g.db"); db = Database(p)
    db.ensure_person("p0001")
    for i in range(30):  # 1 人が 10 秒おきに 30 イベント = 1 回の来訪
        db.record_visit(f"2.{i}-x", "entrance", "p0001", NOW - 1000 + i * 10, NOW - 1000 + i * 10 + 5, "frigate")
    for i in range(3):
        db.record_visit(f"3.{i}-y", "entrance", None, NOW - 5000 - i * 1000, NOW - 5000 - i * 1000 + 5, "none")
    a = Api(Settings(data_dir=str(tmp_path)), p, None, now=lambda: NOW)
    st, v = a.dispatch("GET", "/api/visits", {"limit": "5"}, None)
    assert st == 200 and len(v["visits"]) == 4
    assert v["visits"][0]["events"] == 30 and v["visits"][0]["event_id"] == "2.0-x"


def test_rename_validation(tmp_path):
    a = api(tmp_path)
    st, r = a.dispatch("PUT", "/api/persons/p0001", {}, {"display_name": " 佐藤 "})
    assert st == 200 and r["display_name"] == "佐藤"
    st, _ = a.dispatch("PUT", "/api/persons/p0001", {}, {"display_name": ""})
    assert st == 200 and Database(a.db_path).persons()[0]["display_name"] is None
    assert a.dispatch("PUT", "/api/persons/p9999", {}, {"display_name": "x"})[0] == 404
    assert a.dispatch("PUT", "/api/persons/bad id", {}, {"display_name": "x"})[0] == 400
    assert a.dispatch("PUT", "/api/persons/../x", {}, {"display_name": "x"})[0] == 404
    assert a.dispatch("PUT", "/api/persons/p0001", {}, {"display_name": "あ" * 51})[0] == 400
    # HTML 記号・制御文字は拒否（XSS 対策）
    for bad in ("<img src=x onerror=alert(1)>", "a&b", 'x"y', "a\x00b"):
        assert a.dispatch("PUT", "/api/persons/p0001", {}, {"display_name": bad})[0] == 400
    assert a.dispatch("PUT", "/api/persons/p0001", {}, {"display_name": 123})[0] == 400
    assert Database(a.db_path).persons()[0]["display_name"] is None


def test_merge_and_purge_go_through_gatekeeper_with_lock(tmp_path):
    gk = FakeGk()
    a = api(tmp_path, gk)
    assert a.dispatch("POST", "/api/persons/p0002/merge", {}, {"into": "p0001"}) == (200, {"images_moved": 1, "visits_moved": 2})
    assert a.dispatch("POST", "/api/persons/p0002/merge", {}, {"into": "bad id"})[0] == 400
    assert a.dispatch("POST", "/api/persons/p0002/merge", {}, {"into": 5})[0] == 400
    assert a.dispatch("POST", "/api/persons/p0002/purge", {}, None)[0] == 200
    assert gk.calls == [("merge", "p0002", "p0001"), ("purge", "p0002")]
    assert api(tmp_path, None).dispatch("POST", "/api/persons/p0002/purge", {}, None)[0] == 503


def test_mutation_waits_for_processing_lock(tmp_path):
    gk = FakeGk()
    a = api(tmp_path, gk)
    started = threading.Event()

    def hold():  # 処理ループがロックを持っている状態を再現
        with gk.lock:
            started.set()
            time.sleep(0.3)

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(1)
    st, _ = a.dispatch("POST", "/api/persons/p0002/purge", {}, None)
    holder.join()
    assert st == 200 and gk.calls == [("purge", "p0002")]


def _req(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "X-Requested-With": "gatekeeper"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


@pytest.fixture
def server(tmp_path):
    p = str(tmp_path / "g.db"); seed(p)
    (tmp_path / "faces").mkdir(); (tmp_path / "faces" / "a.jpg").write_bytes(b"IMG"); (tmp_path / "faces" / "b.webp").write_bytes(b"W")
    cfg = Settings(data_dir=str(tmp_path))
    srv = make_server(cfg, p, FakeGk(), FakeClient(), port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_http_get_endpoints(server):
    st, html, h = _req(server, "/")
    assert st == 200 and "門番くん 統計" in html.decode() and "X-Requested-With" in html.decode()
    assert h["Content-Security-Policy"].startswith("default-src 'self'") and h["X-Content-Type-Options"] == "nosniff"
    assert _req(server, "/faces/a.jpg")[1] == b"IMG"
    assert _req(server, "/faces/b.webp")[2]["Content-Type"] == "image/webp"
    assert _req(server, "/api/events/1.0-abc/snapshot.jpg")[1] == b"JPG"
    # 記録に無いイベント（他カメラなど）は取り出せない
    assert _req(server, "/api/events/9.9-zzz/snapshot.jpg")[0] == 404
    assert json.loads(_req(server, "/api/summary?days=100")[1])["events"] == 3
    for bad in ("/faces/../g.db", "/faces/nope.jpg", "/api/events/x/snapshot.jpg", "/api/nothing"):
        assert _req(server, bad)[0] in (400, 404), bad


def test_http_mutations_require_header_json_and_same_site(server):
    # 正常
    st, body, _ = _req(server, "/api/persons/p0001", "PUT", {"display_name": "山田"})
    assert st == 200 and json.loads(body)["display_name"] == "山田"
    # 独自ヘッダなし → 403（フォームからの CSRF を防ぐ）
    st, _, _ = _req(server, "/api/persons/p0001", "PUT", {"display_name": "x"}, {"X-Requested-With": ""})
    assert st == 403
    # フォームの Content-Type → 403
    req = urllib.request.Request(server + "/api/persons/p0001/purge", data=b"a=b", method="POST",
                                 headers={"Content-Type": "text/plain", "X-Requested-With": "gatekeeper"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    # cross-site → 403
    assert _req(server, "/api/persons/p0001", "PUT", {"display_name": "x"}, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _req(server, "/api/persons/p0001", "PUT", {"display_name": "x"}, {"Origin": "http://evil.example"})[0] == 403
    # 壊れた本文 → 400（名前は消えない）
    req = urllib.request.Request(server + "/api/persons/p0001", data=b"{bad", method="PUT",
                                 headers={"Content-Type": "application/json", "X-Requested-With": "gatekeeper"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
    assert json.loads(_req(server, "/api/persons")[1])["persons"][0]["display_name"] == "山田"
    # 配列は拒否、巨大な本文は 413
    req = urllib.request.Request(server + "/api/persons/p0001", data=b"[1]", method="PUT",
                                 headers={"Content-Type": "application/json", "X-Requested-With": "gatekeeper"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
    req = urllib.request.Request(server + "/api/persons/p0001", data=b"{}", method="PUT",
                                 headers={"Content-Type": "application/json", "X-Requested-With": "gatekeeper", "Content-Length": "999999999"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 413


def test_appearance_groups_endpoint_and_visit_fields(tmp_path):
    a = api(tmp_path)
    db = Database(a.db_path)
    db.record_visit("5.0-aaa", "entrance", None, NOW - 400, NOW - 390, "none")
    db.record_visit("5.1-bbb", "entrance", None, NOW - 200, NOW - 190, "none")
    db.set_appearance("5.0-aaa", None, "a0001", 0.9, "x"); db.set_appearance("5.1-bbb", None, "a0001", 0.9, "5.0-aaa")
    db.record_visit("5.2-ccc", "entrance", None, NOW - 3000, NOW - 2990, "none")  # 顔の来訪(NOW-100)とは離れている
    db.set_appearance("5.2-ccc", "p0001", None, 0.91, "1.0-abc")
    st, g = a.dispatch("GET", "/api/appearance-groups", {"days": "7"}, None)
    assert st == 200 and g["groups"][0]["id"] == "a0001" and g["groups"][0]["visits"] == 2
    st, v = a.dispatch("GET", "/api/visits", {"limit": "10"}, None)
    row = next(x for x in v["visits"] if x["event_id"] == "5.2-ccc")
    assert row["appearance_person"] == "p0001" and row["display_name"] == "田中" and row["person_id"] is None
    st, s = a.dispatch("GET", "/api/summary", {"days": "7"}, None)
    assert s["appearance_visits"] == 1 and s["grouped_visits"] == 2
    st, v = a.dispatch("GET", "/api/visits", {"person": "a0001"}, None)
    assert st == 200 and len(v["visits"]) == 2


def test_similar_events_client_shapes():
    from tests.test_frigate_client import Resp, client
    c = client({("GET", "/api/events/search"): Resp(200, json_body=[{"id": "x", "search_distance": 0.1}])})
    assert c.similar_events("e", 1.0, 2.0) == [{"id": "x", "search_distance": 0.1}]
    assert c.session.calls[0][2]["params"]["search_type"] == "similarity"
    assert client({("GET", "/api/events/search"): Resp(400, json_body={"message": "Semantic search is not enabled"})}).similar_events("e", 1, 2) is None
    assert client({("GET", "/api/events/search"): Resp(404)}).similar_events("e", 1, 2) == []


def test_clip_proxy_streams_known_events_only(server):
    st, body, h = _req(server, "/api/events/1.0-abc/clip.mp4")
    assert st == 200 and body == b"MP4-DATA" and h["Content-Type"] == "video/mp4"
    assert _req(server, "/api/events/1.1-def/clip.mp4")[0] == 404      # 記録はあるが録画が無い
    assert _req(server, "/api/events/9.9-zzz/clip.mp4")[0] == 404      # 記録に無いイベント
    assert _req(server, "/api/events/bad/clip.mp4")[0] == 400


def test_alerts_endpoint_and_risk_on_visits(tmp_path):
    a = api(tmp_path)
    db = Database(a.db_path)
    db.upsert_alert("1.1-def", None, "medium", 3, ["顔を特定できない", "滞在 60 秒"], NOW - 50, NOW, True)
    db.upsert_alert("1.0-abc", "p0001", "low", 1, ["名前のない ID p0001"], NOW - 100, NOW, False)
    st, r = a.dispatch("GET", "/api/alerts", {"days": "7"}, None)
    assert st == 200 and [x["event_id"] for x in r["alerts"]] == ["1.1-def"] and r["alerts"][0]["reasons"][1] == "滞在 60 秒"
    st, r = a.dispatch("GET", "/api/alerts", {"days": "7", "min": "low"}, None)
    assert st == 200 and len(r["alerts"]) == 2 and r["alerts"][1]["display_name"] == "田中"
    assert a.dispatch("GET", "/api/alerts", {"min": "x"}, None)[0] == 400
    st, v = a.dispatch("GET", "/api/visits", {"limit": "10"}, None)
    row = next(x for x in v["visits"] if x["event_id"] == "1.1-def")
    assert row["risk_level"] == "medium" and row["risk_score"] == 3
