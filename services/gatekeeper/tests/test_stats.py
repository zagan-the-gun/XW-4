import time
from datetime import datetime

from gatekeeper import stats

T0 = time.mktime(datetime(2026, 9, 21, 8, 0, 0).timetuple())  # 月曜 08:00 (ローカル)


def v(eid, start, dur=5, pid=None, gender=None, method=None, face=None):
    return {"event_id": eid, "start_time": start, "end_time": start + dur, "person_id": pid,
            "method": method or ("frigate" if pid else "none"), "gender": gender, "face_file": face, "age": None}


def test_group_visits_merges_consecutive_events_of_same_person():
    rows = [v("a", T0, 10, "p1"), v("b", T0 + 30, 10, "p1"), v("c", T0 + 500, 10, "p1"),
            v("d", T0 + 35, 10, "p2"), v("e", T0 + 40, 5), v("f", T0 + 45, 5)]
    g = stats.group_visits(rows, gap_seconds=120)
    by = {x["event_id"]: x for x in g}
    assert by["a"]["events"] == 2 and by["a"]["event_ids"] == ["a", "b"] and by["a"]["duration"] == 40
    assert by["c"]["events"] == 1
    assert by["d"]["events"] == 1
    assert "e" in by and "f" in by  # 未特定はまとめない
    assert len(g) == 5


def test_group_visits_skips_pending_and_keeps_first_gender_and_face():
    rows = [v("a", T0, 10, "p1", face=None), v("b", T0 + 20, 10, "p1", gender="male", face="faces/b.jpg"),
            {"event_id": "p", "start_time": T0 + 25, "end_time": None, "person_id": "p1", "method": "pending"}]
    g = stats.group_visits(rows)
    assert len(g) == 1 and g[0]["gender"] == "male" and g[0]["face_file"] == "faces/b.jpg"


def test_daily_and_hourly_counts():
    now = T0 + 2 * 86400 + 3600  # 水曜 09:00
    rows = [v("a", T0, 10, "p1"), v("b", T0 + 30, 10, "p1"), v("c", T0 + 86400, 5), v("d", now - 60, 5, "p2")]
    daily = stats.daily_counts(rows, 3, now)
    assert [d["date"][5:] for d in daily] == ["09-21", "09-22", "09-23"]
    assert [d["events"] for d in daily] == [2, 1, 1]
    assert [d["visits"] for d in daily] == [1, 1, 1]
    assert [d["identified"] for d in daily] == [2, 0, 1]
    assert [d["persons"] for d in daily] == [1, 0, 1]
    assert stats.hourly_counts(rows, "2026-09-23")[8] == 1
    assert sum(stats.hourly_counts(rows)) == 4


def test_heatmap_by_weekday_and_hour():
    rows = [v("a", T0, 10, "p1"), v("b", T0 + 3600 * 5), v("c", T0 + 86400 * 6 + 3600 * 15)]  # 日曜 23:00
    grid = stats.heatmap(rows)
    assert grid[0][8] == 1 and grid[0][13] == 1 and grid[6][23] == 1 and sum(map(sum, grid)) == 3
    only = stats.heatmap(rows, identified_only=True)
    assert sum(map(sum, only)) == 1


def test_gender_ratio_counts_visits_not_events():
    rows = [v("a", T0, 10, "p1", gender="male"), v("b", T0 + 20, 10, "p1", gender="male"),
            v("c", T0 + 100, 5, gender="female"), v("d", T0 + 200, 5)]
    assert stats.gender_ratio(rows) == {"male": 1, "female": 1, "unknown": 1}


def test_person_summaries():
    persons = [{"id": "p1", "display_name": "田中", "last_seen": T0 + 30, "created_at": T0 - 100},
               {"id": "p2", "display_name": None, "last_seen": None, "created_at": T0}]
    rows = [v("a", T0, 10, "p1", gender="male", face="faces/a.jpg"), v("b", T0 + 20, 10, "p1", gender="female", face="faces/b.jpg"),
            v("c", T0 + 1000, 10, "p1", gender="male")]
    rows[2]["age"] = 40; rows[0]["age"] = 30
    out = stats.person_summaries(persons, rows)
    assert [o["id"] for o in out] == ["p1", "p2"]
    p1 = out[0]
    assert p1["visits"] == 2 and p1["events"] == 3 and p1["gender"] == "male" and p1["age"] == 35
    assert p1["face_file"] == "faces/b.jpg" and p1["first_seen"] == T0 and p1["display_name"] == "田中"
    assert out[1]["visits"] == 0 and out[1]["gender"] is None


def test_group_visits_keeps_gender_triple_from_best_estimate():
    a = v("a", T0, 10, "p1"); a["gender"] = "female"; a["gender_score"] = 0.76; a["age"] = 30
    b = v("b", T0 + 20, 10, "p1"); b["gender"] = "male"; b["gender_score"] = 0.95; b["age"] = 40
    g = stats.group_visits([a, b])
    assert len(g) == 1 and (g[0]["gender"], g[0]["gender_score"], g[0]["age"]) == ("male", 0.95, 40)


def test_daily_and_gender_ratio_honor_gap():
    rows = [v("a", T0, 10, "p1", gender="male"), v("b", T0 + 300, 10, "p1", gender="male")]
    assert stats.daily_counts(rows, 1, T0 + 3600, gap_seconds=120)[0]["visits"] == 2
    assert stats.daily_counts(rows, 1, T0 + 3600, gap_seconds=600)[0]["visits"] == 1
    assert stats.gender_ratio(rows, 120)["male"] == 2 and stats.gender_ratio(rows, 600)["male"] == 1


def test_person_gender_vote_is_weighted_by_confidence():
    persons = [{"id": "p1", "display_name": None, "last_seen": None, "created_at": T0}]
    a = v("a", T0, 10, "p1"); a["gender"] = "female"; a["gender_score"] = 0.76; a["age"] = 30
    b = v("b", T0 + 1000, 10, "p1"); b["gender"] = "male"; b["gender_score"] = 0.99; b["age"] = 60
    out = stats.person_summaries(persons, [a, b])
    assert out[0]["gender"] == "male" and out[0]["age"] == 60


def test_appearance_person_groups_and_counts_like_identified():
    a = v("a", T0, 10, None); a["appearance_person"] = "p1"
    b = v("b", T0 + 30, 10, None); b["appearance_person"] = "p1"
    c = v("c", T0 + 40, 10, "p1")
    g = stats.group_visits([a, b, c])
    assert len(g) == 1 and g[0]["events"] == 3 and g[0]["person_id"] == "p1"  # 顔で特定した情報が代表になる
    daily = stats.daily_counts([a, b, c], 1, T0 + 3600)
    assert daily[0]["identified"] == 3 and daily[0]["persons"] == 1
    persons = [{"id": "p1", "display_name": None, "last_seen": None, "created_at": T0}]
    out = stats.person_summaries(persons, [a, b, c])
    assert out[0]["visits"] == 1 and out[0]["events"] == 3 and out[0]["appearance_events"] == 2


def test_appearance_groups_summary():
    rows = []
    for i, gid in enumerate(["a1", "a1", "a2", None]):
        r = v(f"e{i}", T0 + i * 1000, 5); r["appearance_group"] = gid
        rows.append(r)
    rows[1]["face_file"] = "faces/e1.jpg"
    out = stats.appearance_groups(rows)
    assert [o["id"] for o in out] == ["a1", "a2"]
    assert out[0]["visits"] == 2 and out[0]["face_file"] == "faces/e1.jpg" and out[0]["event_ids"] == ["e0", "e1"]
