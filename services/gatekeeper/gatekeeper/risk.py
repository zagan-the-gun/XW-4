"""危険度スコア（純粋ロジック）。表情ではなく行動を主にする。

対象は「来訪」（同一人物の連続イベントをまとめたもの）。表示名を付けた人物（家族・知人）は対象外。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

LEVELS = ("low", "medium", "high")
LEVEL_JA = {"low": "低", "medium": "中", "high": "高"}


@dataclass
class Risk:
    score: int
    level: str
    reasons: List[str] = field(default_factory=list)


def level_of(score: int, medium: int = 3, high: int = 5) -> str:
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"


def is_night(ts: float, start_hour: int = 22, end_hour: int = 5) -> bool:
    h = datetime.fromtimestamp(ts).hour
    return h >= start_hour or h < end_hour


def assess(
    visit: dict,
    display_names: Dict[str, Optional[str]],
    repeat_visits_24h: int,
    expression: Optional[str] = None,
    expression_score: float = 0.0,
    now: Optional[float] = None,
) -> Risk:
    """visit は stats.group_visits の 1 要素（events, duration, person_id, appearance_person, appearance_group…）。"""
    reasons: List[str] = []
    score = 0
    pid = visit.get("person_id") or visit.get("appearance_person")
    if pid and display_names.get(pid):
        return Risk(0, "low", [f"登録済み: {display_names[pid]}"])
    if not pid:
        score += 1
        reasons.append("顔を特定できない")
    else:
        score += 1
        reasons.append(f"名前のない ID {pid}" + ("（見た目で推定）" if not visit.get("person_id") else ""))

    end = visit.get("end_time") or visit["start_time"]
    if now is not None and now > end and visit.get("in_progress"):
        end = now
    dwell = max(0.0, end - visit["start_time"])
    if dwell >= 180:
        score += 3
        reasons.append(f"滞在 {int(dwell)} 秒")
    elif dwell >= 90:
        score += 2
        reasons.append(f"滞在 {int(dwell)} 秒")
    elif dwell >= 30:
        score += 1
        reasons.append(f"滞在 {int(dwell)} 秒")

    if is_night(visit["start_time"]):
        score += 1
        reasons.append("夜間")

    if repeat_visits_24h >= 4:
        score += 2
        reasons.append(f"24 時間で {repeat_visits_24h} 回目")
    elif repeat_visits_24h >= 2:
        score += 1
        reasons.append(f"24 時間で {repeat_visits_24h} 回目")

    if expression in ("angry", "fearful", "disgust") and expression_score >= 0.6:
        score += 1
        reasons.append(f"表情: {expression}（{expression_score:.2f}）")

    return Risk(score, level_of(score), reasons)
