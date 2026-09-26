from gatekeeper.attempts import Attempt
from gatekeeper.config import Settings
from gatekeeper.decide import decide

CFG = Settings(min_attempts_new=2, min_face_px=40, new_id_images=3, reinforce_per_event=1, merge_score=0.8, reinforce_min_score=0.85)


def att(name, score, px=100, i=0):
    return Attempt(file=f"e-{i}-{name}-{score}.webp", event_id="e", timestamp=float(i), name=name, score=score, face_px=px)


def test_frigate_assigned_wins():
    d = decide("p0001", 0.93, [att("p0001", 0.9, i=1), att("p0001", 0.7, i=2)], ["p0001"], CFG)
    assert d.method == "frigate" and d.person_id == "p0001" and d.score == 0.93
    # 補強に使うのは高スコアで十分な大きさの 1 枚だけ
    assert d.train_files == ["e-1-p0001-0.9.webp"]


def test_frigate_sub_label_unknown_name_is_ignored():
    d = decide("someone", 0.95, [att("unknown", 0.3, i=1), att("unknown", 0.4, i=2)], ["p0001"], CFG)
    assert d.method == "new"


def test_attempts_majority():
    atts = [att("p0001", 0.85, i=1), att("p0001", 0.82, i=2), att("p0002", 0.81, i=3), att("unknown", 0.5, i=4)]
    d = decide(None, None, atts, ["p0001", "p0002"], CFG)
    assert d.method == "attempts" and d.person_id == "p0001"
    assert abs(d.score - 0.835) < 0.01
    assert d.train_files == ["e-1-p0001-0.85.webp"]


def test_attempts_split_vote_falls_through_to_new():
    atts = [att("p0001", 0.81, i=1), att("p0002", 0.82, i=2), att("p0003", 0.83, i=3)]
    d = decide(None, None, atts, ["p0001", "p0002", "p0003"], CFG)
    assert d.method == "new"


def test_new_id_uses_largest_faces_first():
    atts = [att("unknown", 0.2, px=50, i=1), att("unknown", 0.3, px=120, i=2), att("unknown", 0.1, px=90, i=3), att("unknown", 0.4, px=30, i=4)]
    d = decide(None, None, atts, ["p0001"], CFG)
    assert d.method == "new" and d.is_new
    assert d.train_files == ["e-2-unknown-0.3.webp", "e-3-unknown-0.1.webp", "e-1-unknown-0.2.webp"]


def test_too_few_usable_attempts():
    d = decide(None, None, [att("unknown", 0.2, px=20, i=1), att("unknown", 0.2, px=100, i=2)], ["p0001"], CFG)
    assert d.method == "none" and "1/2" in d.reason


def test_named_below_merge_score_is_not_a_match():
    d = decide(None, None, [att("p0001", 0.79, i=1), att("p0001", 0.75, i=2)], ["p0001"], CFG)
    assert d.method == "new"
