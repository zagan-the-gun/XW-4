"""イベント 1 件に対して「誰か」を決める純粋ロジック（I/O なし）。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from .attempts import UNKNOWN, Attempt
from .config import Settings


@dataclass
class Decision:
    # frigate: Frigate 自身が認識済み / attempts: 試行画像の多数決で既存人物 /
    # new: 試行画像から新しい ID / snapshot: スナップショットで既存人物 /
    # snapshot-new: スナップショットから新しい ID / retry: 次の周期でやり直す / none: 特定できず
    method: str
    person_id: Optional[str] = None
    score: Optional[float] = None
    # Frigate の train フォルダから person_id に振り分ける試行画像
    train_files: List[str] = field(default_factory=list)
    is_new: bool = False
    reason: str = ""


def decide(
    event_sub_label: Optional[str],
    event_sub_label_score: Optional[float],
    attempts: Sequence[Attempt],
    known: Iterable[str],
    cfg: Settings,
) -> Decision:
    known_set = set(known) - {UNKNOWN}
    by_score = sorted(attempts, key=lambda a: a.score, reverse=True)

    def usable(a: Attempt) -> bool:
        return a.face_px >= cfg.min_face_px

    # 1. Frigate が加重平均 >= recognition_threshold で名前を付けたイベント
    if event_sub_label and event_sub_label in known_set:
        reinforce = [
            a.file
            for a in by_score
            if a.name == event_sub_label and a.score >= cfg.reinforce_min_score and usable(a)
        ][: cfg.reinforce_per_event]
        return Decision("frigate", event_sub_label, event_sub_label_score, reinforce)

    # 2. 試行画像の多数決（スコア重み付き）。同点は単発の最高スコアで決め、それも同じなら判定しない
    named = [a for a in attempts if a.name in known_set and a.score >= cfg.merge_score]
    if named:
        weights = defaultdict(float)
        best = defaultdict(float)
        for a in named:
            weights[a.name] += a.score
            best[a.name] = max(best[a.name], a.score)
        ranking = sorted(weights, key=lambda n: (weights[n], best[n]), reverse=True)
        top = ranking[0]
        tied = len(ranking) > 1 and (weights[ranking[1]], best[ranking[1]]) == (weights[top], best[top])
        share = weights[top] / sum(weights.values())
        if share >= 0.5 and not tied:
            tops = sorted((a for a in named if a.name == top), key=lambda a: a.score, reverse=True)
            score = round(sum(a.score for a in tops) / len(tops), 2)
            reinforce = [a.file for a in tops if a.score >= cfg.reinforce_min_score and usable(a)][
                : cfg.reinforce_per_event
            ]
            return Decision("attempts", top, score, reinforce)
        if tied:
            return Decision("none", reason="tie between " + " / ".join(ranking[:2]))

    # 3. 誰にも似ていない → 十分な枚数と大きさがあれば新しい ID
    good = [a for a in attempts if usable(a)]
    if len(good) >= cfg.min_attempts_new:
        files = [a.file for a in sorted(good, key=lambda a: (a.face_px, a.score), reverse=True)]
        return Decision("new", None, None, files[: cfg.new_id_images], is_new=True)

    return Decision("none", reason=f"usable attempts {len(good)}/{len(attempts)}")
