import json
import time
from datetime import datetime

from gatekeeper import risk
from gatekeeper.notify import DiscordNotifier

DAY = time.mktime(datetime(2026, 9, 21, 14, 0, 0).timetuple())
NIGHT = time.mktime(datetime(2026, 9, 21, 23, 30, 0).timetuple())


def visit(start, dur, pid=None, ap=None, group=None):
    return {"event_id": "e", "start_time": start, "end_time": start + dur, "events": 1,
            "person_id": pid, "appearance_person": ap, "appearance_group": group}


def test_named_person_is_never_risky():
    r = risk.assess(visit(NIGHT, 600, pid="p0001"), {"p0001": "田中"}, 5)
    assert r.score == 0 and r.level == "low" and "登録済み" in r.reasons[0]


def test_short_daytime_passerby_is_low():
    r = risk.assess(visit(DAY, 5), {}, 1)
    assert r.score == 1 and r.level == "low" and r.reasons == ["顔を特定できない"]


def test_loitering_at_night_repeat_is_high():
    r = risk.assess(visit(NIGHT, 200, group="a0001"), {}, 4)
    assert r.score == 1 + 3 + 1 + 2 and r.level == "high"
    assert any("滞在 200 秒" in x for x in r.reasons) and "夜間" in r.reasons and any("4 回目" in x for x in r.reasons)


def test_unnamed_auto_id_with_medium_dwell_is_medium():
    r = risk.assess(visit(DAY, 100, ap="p0009"), {"p0009": None}, 1)
    assert r.score == 1 + 2 and r.level == "medium" and "見た目で推定" in r.reasons[0]


def test_in_progress_visit_counts_dwell_until_now():
    v = visit(DAY, 10); v["in_progress"] = True
    r = risk.assess(v, {}, 1, now=DAY + 100)
    assert any("滞在 100 秒" in x for x in r.reasons)


def test_expression_adds_one_point_only_when_confident():
    assert risk.assess(visit(DAY, 5), {}, 1, "angry", 0.9).score == 2
    assert risk.assess(visit(DAY, 5), {}, 1, "angry", 0.4).score == 1
    assert risk.assess(visit(DAY, 5), {}, 1, "happy", 0.9).score == 1


class FakeSession:
    def __init__(self, status=204):
        self.status = status
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))

        class R:
            status_code = self.status
            text = ""

        return R()


def test_discord_notifier_sends_embed_with_image():
    s = FakeSession()
    n = DiscordNotifier("https://discord.com/api/webhooks/x", session=s)
    assert n.enabled
    assert n.send("t", "d", "high", [("a", "1")], image=b"JPG", link="http://x/") is True
    url, kw = s.calls[0]
    payload = json.loads(kw["data"]["payload_json"])
    assert payload["embeds"][0]["title"] == "t" and payload["embeds"][0]["image"]["url"] == "attachment://snapshot.jpg"
    assert kw["files"]["file"][1] == b"JPG" and payload["embeds"][0]["url"] == "http://x/"
    assert n.send("t", "d", "low", []) is True and "json" in s.calls[1][1]


def test_discord_notifier_disabled_without_url_and_handles_errors():
    assert DiscordNotifier("").enabled is False and DiscordNotifier("").send("t", "d", "low", []) is False
    n = DiscordNotifier("https://x", session=FakeSession(status=400))
    assert n.send("t", "d", "low", []) is False
