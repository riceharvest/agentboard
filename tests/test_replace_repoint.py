"""--replace cancels the card holding the key AND repoints the cards that waited on it, so none waits on a cancelled card forever."""
import json
import sqlite3


def _db(abx):
    return sqlite3.connect(next(abx.home.rglob("*.db")))


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, "--tier", "low", *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def test_replace_repoints_dependents(abx):
    a = _add(abx, "fix", "--key", "fix:x")
    b = _add(abx, "gate", "--dep", a)
    a2 = _add(abx, "fix v2", "--key", "fix:x", "--replace")
    assert a2 != a
    db = _db(abx)
    assert db.execute("select status from tasks where id=?", (a,)).fetchone()[0] == "cancelled"
    assert [r[0] for r in db.execute("select dep_id from deps where task_id=?", (b,))] == [a2]


def test_replace_does_not_close_a_cycle(abx):
    x = _add(abx, "x")
    a = _add(abx, "fix", "--key", "k", "--dep", x)
    b = _add(abx, "gate", "--dep", a)
    # the replacement itself waits on the gate: repointing the gate onto it would be a cycle, so the gate keeps no dep on it
    a2 = _add(abx, "fix2", "--key", "k", "--replace", "--dep", b)
    db = _db(abx)
    assert db.execute("select count(*) from deps where task_id=? and dep_id=?", (b, a2)).fetchone()[0] == 0
    assert db.execute("select count(*) from deps where task_id=? and dep_id=?", (a2, b)).fetchone()[0] == 1
