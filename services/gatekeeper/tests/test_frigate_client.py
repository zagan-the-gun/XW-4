"""FrigateClient の HTTP 契約（偽セッション）と Settings.from_env。"""
import pytest

from gatekeeper.config import Settings
from gatekeeper.frigate import FrigateClient, FrigateError


class Resp:
    def __init__(self, status, json_body=None, content=b"", text=""):
        self.status_code = status
        self._json = json_body
        self.content = content
        self.text = text or (str(json_body) if json_body is not None else "")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, routes):
        self.routes = routes   # (method, path) -> Resp or callable(kw)
        self.headers = {}
        self.calls = []

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        path = url.split("http://f:5000", 1)[1]
        r = self.routes[(method, path)]
        return r(kw) if callable(r) else r


def client(routes):
    c = FrigateClient.__new__(FrigateClient)
    c.base = "http://f:5000"
    c.timeout = 1
    c.session = FakeSession(routes)
    c.session.headers["X-Cache-Bypass"] = "1"
    return c


def test_real_client_sets_cache_bypass_header():
    pytest.importorskip("requests")
    c = FrigateClient("http://f:5000/")
    assert c.session.headers["X-Cache-Bypass"] == "1" and c.base == "http://f:5000"


def test_event_snapshot_requests_clean_crop():
    c = client({("GET", "/api/events/e1/snapshot.jpg"): Resp(200, content=b"JPG")})
    assert c.event_snapshot("e1") == b"JPG"
    kw = c.session.calls[0][2]
    assert kw["params"] == {"crop": 1, "bbox": 0, "timestamp": 0, "quality": 90}


def test_event_snapshot_404_is_none_and_500_raises():
    c = client({("GET", "/api/events/e1/snapshot.jpg"): Resp(404)})
    assert c.event_snapshot("e1") is None
    c = client({("GET", "/api/events/e1/snapshot.jpg"): Resp(500, text="boom")})
    with pytest.raises(FrigateError):
        c.event_snapshot("e1")


def test_list_events_params():
    c = client({("GET", "/api/events"): Resp(200, json_body=[])})
    c.list_events("cam", after=1.5, in_progress=0, limit=50, sort="date_asc")
    params = c.session.calls[0][2]["params"]
    assert params == {"cameras": "cam", "labels": "person", "limit": 50, "include_thumbnails": 0,
                      "in_progress": 0, "after": 1.5, "sort": "date_asc"}


def test_recognize_and_register_return_400_bodies_as_dicts():
    body = {"success": False, "message": "No face was detected."}
    c = client({("POST", "/api/faces/recognize"): Resp(400, json_body=body),
                ("POST", "/api/faces/p0001/register"): Resp(400, json_body=body)})
    assert c.recognize(b"img") == body
    assert c.register_face("p0001", b"img") == body
    files = c.session.calls[0][2]["files"]
    assert files["file"][0] == "face.jpg" and files["file"][1] == b"img"


def test_classify_attempt_false_on_404_and_400():
    c = client({("POST", "/api/faces/train/p0001/classify"): Resp(404, json_body={"success": False, "message": "x"})})
    assert c.classify_attempt("p0001", "f.webp") is False
    c = client({("POST", "/api/faces/train/p0001/classify"): Resp(200, json_body={"success": True})})
    assert c.classify_attempt("p0001", "f.webp") is True
    assert c.session.calls[0][2]["json"] == {"training_file": "f.webp"}


def test_set_sub_label_drops_out_of_range_score():
    c = client({("POST", "/api/events/e1/sub_label"): Resp(200, json_body={})})
    c.set_sub_label("e1", "p0001", 0.0)
    assert c.session.calls[0][2]["json"] == {"subLabel": "p0001"}
    c.set_sub_label("e1", "p0001", 0.9)
    assert c.session.calls[1][2]["json"] == {"subLabel": "p0001", "subLabelScore": 0.9}


def test_delete_faces_skips_empty_and_sends_ids():
    c = client({("POST", "/api/faces/train/delete"): Resp(200, json_body={})})
    c.delete_faces("train", [])
    assert c.session.calls == []
    c.delete_faces("train", ["a", "b"])
    assert c.session.calls[0][2]["json"] == {"ids": ["a", "b"]}


def test_detect_size_reads_config_and_tolerates_failure():
    c = client({("GET", "/api/config"): Resp(200, json_body={"cameras": {"cam": {"detect": {"width": 640, "height": 360}}}})})
    assert c.detect_size("cam") == (640, 360)
    assert c.detect_size("other") is None
    c = client({("GET", "/api/config"): Resp(500)})
    assert c.detect_size("cam") is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL", "30")
    monkeypatch.setenv("MIN_ATTEMPTS_NEW", "3.0")
    monkeypatch.setenv("CAMERA", " door ")
    cfg = Settings.from_env()
    assert cfg.poll_interval == 30.0 and cfg.min_attempts_new == 3 and cfg.camera == "door"
    monkeypatch.setenv("MAX_IMAGES_PER_PERSON", "many")
    with pytest.raises(SystemExit) as e:
        Settings.from_env()
    assert "MAX_IMAGES_PER_PERSON" in str(e.value)
    monkeypatch.delenv("MAX_IMAGES_PER_PERSON")
    monkeypatch.setenv("POLL_INTERVAL", "0")
    with pytest.raises(SystemExit):
        Settings.from_env()


def test_error_text_is_plain_and_short():
    c = client({("GET", "/api/events/e1/snapshot.jpg"): Resp(502, text="<html><body><h1>502 Bad Gateway</h1><script>x</script></body></html>")})
    with pytest.raises(FrigateError) as e:
        c.event_snapshot("e1")
    assert "<" not in str(e.value) and "502 Bad Gateway" in str(e.value)


def test_event_clip_stream_uses_streaming_get():
    class R:
        def __init__(self, status): self.status_code = status
        def iter_content(self, chunk_size): return iter([b"a", b"b"])
        def close(self): pass

    class S:
        headers = {}
        def __init__(self): self.calls = []
        def get(self, url, stream=False, timeout=None):
            self.calls.append((url, stream)); return R(200 if "ok" in url else 404)

    c = FrigateClient.__new__(FrigateClient); c.base = "http://f:5000"; c.session = S()
    assert list(c.event_clip_stream("ok-1")) == [b"a", b"b"] and c.session.calls[0][1] is True
    assert c.event_clip_stream("missing-1") is None
