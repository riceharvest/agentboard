"""`ab ls --limit` cut: a note on stderr says rows were hidden; stdout stays parseable."""
import json


def _fill(abx, n=60):
    for i in range(n):
        assert abx("add", f"c{i}")[0] == 0


def test_ls_truncation_note(abx):
    _fill(abx)
    code, o, e = abx("ls", "--status", "all")
    assert code == 0 and len(o.splitlines()) == 50
    assert "showing 50 of 60" in e and "--limit" in e
    code, o, e = abx("ls", "--status", "all", "--limit", "500")
    assert code == 0 and len(o.splitlines()) == 60 and e.strip() == ""


def test_ls_truncation_json(abx):
    _fill(abx)
    code, o, e = abx("ls", "--status", "all", "--json")
    rows = json.loads(o)
    assert code == 0 and isinstance(rows, list) and len(rows) == 50
    assert "showing 50 of 60" in e


def test_ls_no_note_when_short(abx):
    _fill(abx, 3)
    code, o, e = abx("ls")
    assert code == 0 and len(o.splitlines()) == 3 and e.strip() == ""
