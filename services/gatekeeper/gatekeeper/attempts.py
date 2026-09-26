"""Frigate が /media/frigate/clips/faces/train に書く試行画像の扱い。

ファイル名の形式（Frigate 0.18, frigate/data_processing/real_time/face.py write_face_attempt）:
    {event_id}-{timestamp}-{sub_label}-{score}.webp
event_id は "1790348534.612203-88vx2v" のように '-' を 1 つ含む。
sub_label 内の '-' は '_' に置換されているので、右から 3 回分割すれば復元できる。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

UNKNOWN = "unknown"


@dataclass
class Attempt:
    file: str
    event_id: str
    timestamp: float
    name: str
    score: float
    # 顔画像の短辺 px。未計測は 0
    face_px: int = field(default=0)


def parse_attempt(file: str) -> Optional[Attempt]:
    if not file.endswith(".webp"):
        return None
    stem = file[: -len(".webp")]
    parts = stem.rsplit("-", 3)
    if len(parts) != 4:
        return None
    event_id, ts, name, score = parts
    if not event_id or not name:
        return None
    try:
        return Attempt(file=file, event_id=event_id, timestamp=float(ts), name=name, score=float(score))
    except ValueError:
        return None


def group_by_event(files: Iterable[str]) -> Dict[str, List[Attempt]]:
    grouped: Dict[str, List[Attempt]] = defaultdict(list)
    for f in files:
        a = parse_attempt(f)
        if a is not None:
            grouped[a.event_id].append(a)
    return dict(grouped)
