from gatekeeper.attempts import group_by_event, parse_attempt


def test_parse_typical():
    a = parse_attempt("1790348534.612203-88vx2v-1790349000.5-unknown-0.55.webp")
    assert a is not None
    assert a.event_id == "1790348534.612203-88vx2v"
    assert a.timestamp == 1790349000.5
    assert a.name == "unknown"
    assert a.score == 0.55


def test_parse_named_with_underscore():
    a = parse_attempt("1790348534.612203-88vx2v-1790349000.5-p0001-0.91.webp")
    assert a and a.name == "p0001" and a.score == 0.91
    b = parse_attempt("1.0-abc-2.0-john_doe-0.8.webp")
    assert b and b.name == "john_doe" and b.event_id == "1.0-abc"


def test_parse_rejects_garbage():
    assert parse_attempt("notes.txt") is None
    assert parse_attempt("x.webp") is None
    assert parse_attempt("a-b-c-notafloat.webp") is None


def test_group():
    g = group_by_event([
        "1.0-a-2.0-unknown-0.1.webp",
        "1.0-a-3.0-unknown-0.2.webp",
        "1.0-b-4.0-p0001-0.9.webp",
        "junk.png",
    ])
    assert set(g) == {"1.0-a", "1.0-b"}
    assert len(g["1.0-a"]) == 2
