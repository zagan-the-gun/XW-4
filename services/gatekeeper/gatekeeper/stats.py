"""ダッシュボード用の集計（純粋ロジック。DB 行のリストを受け取り、JSON にできる dict を返す）。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

PENDING = "pending"


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def who(v: dict) -> Optional[str]:
    """来訪のまとめに使う主体: 顔で特定した人物 > 見た目で推定した人物 > 見た目グループ。"""
    return v.get("person_id") or v.get("appearance_person") or v.get("appearance_group")


def group_visits(visits: Iterable[dict], gap_seconds: float = 120.0) -> List[dict]:
    """同一人物（見た目の推定・グループも含む）の連続イベント（間隔 gap 以内）を 1 回の来訪にまとめる。
    完全に未特定のものは 1 イベント = 1 来訪のまま。"""
    rows = sorted((v for v in visits if v.get("method") != PENDING), key=lambda v: v["start_time"])
    last_by_person: Dict[str, dict] = {}
    out: List[dict] = []
    for v in rows:
        pid = who(v)
        if pid and pid in last_by_person:
            prev = last_by_person[pid]
            prev_end = prev.get("end_time") or prev["start_time"]
            if v["start_time"] - prev_end <= gap_seconds:
                prev["end_time"] = max(prev_end, v.get("end_time") or v["start_time"])
                prev["events"] += 1
                prev["event_ids"].append(v["event_id"])
                # 性別・確信度・年齢は同じ推定から揃えて持ち、より確信度の高いものを採用する
                if v.get("gender") and (not prev.get("gender") or (v.get("gender_score") or 0) > (prev.get("gender_score") or 0)):
                    prev["gender"], prev["gender_score"], prev["age"] = v["gender"], v.get("gender_score"), v.get("age")
                if not prev.get("face_file") and v.get("face_file"):
                    prev["face_file"] = v["face_file"]
                if not prev.get("person_id") and v.get("person_id"):
                    prev["person_id"], prev["method"], prev["score"] = v["person_id"], v["method"], v.get("score")
                continue
        g = dict(v)
        g["events"] = 1
        g["event_ids"] = [v["event_id"]]
        out.append(g)
        if pid:
            last_by_person[pid] = g
    for g in out:
        g["duration"] = (g.get("end_time") or g["start_time"]) - g["start_time"]
    return out


def daily_counts(visits: Iterable[dict], days: int, now: float, gap_seconds: float = 120.0) -> List[dict]:
    """日別: 検知数（全 person イベント）、特定できた数、来訪数（グループ化後）。"""
    rows = [v for v in visits if v.get("method") != PENDING]
    start_day = (_local(now) - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets = {(start_day + timedelta(days=i)).strftime("%Y-%m-%d"): {"date": (start_day + timedelta(days=i)).strftime("%Y-%m-%d"), "events": 0, "identified": 0, "visits": 0, "persons": set()} for i in range(days)}
    for v in rows:
        key = _local(v["start_time"]).strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key]["events"] += 1
            if v.get("person_id") or v.get("appearance_person"):
                buckets[key]["identified"] += 1
                buckets[key]["persons"].add(v.get("person_id") or v["appearance_person"])
    for g in group_visits(rows, gap_seconds):
        key = _local(g["start_time"]).strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key]["visits"] += 1
    out = []
    for b in buckets.values():
        b["persons"] = len(b["persons"])
        out.append(b)
    return out


def hourly_counts(visits: Iterable[dict], day: Optional[str] = None) -> List[int]:
    """指定日（YYYY-MM-DD、None なら全期間）の時間帯別イベント数（24 要素）。"""
    counts = [0] * 24
    for v in visits:
        if v.get("method") == PENDING:
            continue
        t = _local(v["start_time"])
        if day and t.strftime("%Y-%m-%d") != day:
            continue
        counts[t.hour] += 1
    return counts


def heatmap(visits: Iterable[dict], identified_only: bool = False) -> List[List[int]]:
    """曜日（月=0 … 日=6）× 時間帯（0〜23）のイベント数。"""
    grid = [[0] * 24 for _ in range(7)]
    for v in visits:
        if v.get("method") == PENDING:
            continue
        if identified_only and not (v.get("person_id") or v.get("appearance_person")):
            continue
        t = _local(v["start_time"])
        grid[t.weekday()][t.hour] += 1
    return grid


def gender_ratio(visits: Iterable[dict], gap_seconds: float = 120.0) -> Dict[str, int]:
    """来訪（グループ化後）単位の推定性別の内訳。顔が見えなかったものは unknown。"""
    c = Counter()
    for g in group_visits(visits, gap_seconds):
        c[g.get("gender") or "unknown"] += 1
    return {"male": c.get("male", 0), "female": c.get("female", 0), "unknown": c.get("unknown", 0)}


def person_summaries(persons: Iterable[dict], visits: Iterable[dict], gap_seconds: float = 120.0) -> List[dict]:
    """人物ごと: 来訪回数、検知回数、最終訪問、推定性別（多数決）、代表画像。"""
    rows = [v for v in visits if (v.get("person_id") or v.get("appearance_person")) and v.get("method") != PENDING]
    by_person: Dict[str, List[dict]] = defaultdict(list)
    for v in rows:
        by_person[v.get("person_id") or v["appearance_person"]].append(v)
    grouped = Counter(who(g) for g in group_visits(rows, gap_seconds) if who(g))
    grouped_face = Counter(who(g) for g in group_visits(rows, gap_seconds) if g.get("person_id"))
    out = []
    for p in persons:
        vs = by_person.get(p["id"], [])
        # 確信度で重み付けした多数決。年齢は勝った性別の推定だけで平均する
        weights: Dict[str, float] = defaultdict(float)
        genders = Counter()
        for v in vs:
            if v.get("gender"):
                genders[v["gender"]] += 1
                weights[v["gender"]] += float(v.get("gender_score") or 0.5)
        gender = max(weights, key=lambda k: (weights[k], k)) if weights else None
        ages = [v["age"] for v in vs if v.get("age") and v.get("gender") == gender]
        latest = max(vs, key=lambda v: v["start_time"]) if vs else None
        with_face = [v for v in vs if v.get("face_file")]
        face = max(with_face, key=lambda v: v["start_time"])["face_file"] if with_face else None
        out.append({
            "id": p["id"],
            "display_name": p.get("display_name"),
            "visits": grouped.get(p["id"], 0),
            "face_visits": grouped_face.get(p["id"], 0),
            "appearance_events": len([v for v in vs if not v.get("person_id")]),
            "events": len(vs),
            "last_seen": p.get("last_seen") or (latest["start_time"] if latest else None),
            "first_seen": min((v["start_time"] for v in vs), default=p.get("created_at")),
            "gender": gender,
            "gender_votes": dict(genders),
            "age": int(round(sum(ages) / len(ages))) if ages else None,
            "face_file": face,
        })
    out.sort(key=lambda r: (r["last_seen"] or 0), reverse=True)
    return out


def appearance_groups(visits: Iterable[dict], gap_seconds: float = 120.0) -> List[dict]:
    """顔で特定できず、見た目で同じと推定された来訪のグループ（2 回以上来たもの）。"""
    rows = [v for v in visits if v.get("appearance_group") and not v.get("person_id") and not v.get("appearance_person")
            and v.get("method") != PENDING]
    by_group: Dict[str, List[dict]] = defaultdict(list)
    for g in group_visits(rows, gap_seconds):
        by_group[g["appearance_group"]].append(g)
    out = []
    for gid, gs in by_group.items():
        gs.sort(key=lambda g: g["start_time"])
        with_face = [g for g in gs if g.get("face_file")]
        out.append({
            "id": gid,
            "visits": len(gs),
            "events": sum(g["events"] for g in gs),
            "first_seen": gs[0]["start_time"],
            "last_seen": gs[-1].get("end_time") or gs[-1]["start_time"],
            "event_ids": [g["event_id"] for g in gs],
            "face_file": with_face[-1]["face_file"] if with_face else None,
            "gender": next((g["gender"] for g in reversed(gs) if g.get("gender")), None),
        })
    out.sort(key=lambda r: (r["visits"], r["last_seen"]), reverse=True)
    return out
