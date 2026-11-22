"""`ab ls --ready --exclude-type/--exclude-label`: hidden cards are counted in an excluded bucket, not silently dropped."""
import json


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, "--tier", "low", *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def _fix(abx):
    b = _add(abx, "real work")
    w = [_add(abx, f"watch {i}", "-l", "type=watch") for i in (1, 2)]
    return b, w


def test_unfiltered_unchanged(abx):
    b, w = _fix(abx)
    o = abx("ls", "--ready")[1]
    assert all(i in o for i in [b, *w]) and "excluded" not in o


def test_exclude_type_text_and_json(abx):
    b, w = _fix(abx)
    code, o, e = abx("ls", "--ready", "--exclude-type", "watch")
    assert code == 0, e
    assert b in o and not any(i in o for i in w)
    assert o.rstrip().endswith("excluded-type: watch 2")
    p = json.loads(abx("--json", "ls", "--ready", "--exclude-type", "watch")[1])
    assert [d["id"] for d in p["items"]] == [b]
    assert p["excluded"] == {"excluded-type": {"watch": 2}}
    assert p["buckets"]["ready"] == 1


def test_only_watch_cards_gives_zero_rows(abx):
    for i in (1, 2):
        _add(abx, f"watch {i}", "-l", "type=watch")
    o = abx("ls", "--ready", "--exclude-type", "watch")[1]
    assert "(nothing)" in o and "excluded-type: watch 2" in o


def test_exclude_label(abx):
    b, w = _fix(abx)
    o = abx("ls", "--ready", "--exclude-label", "type=watch")[1]
    assert b in o and w[0] not in o and "excluded-label: type=watch 2" in o
