"""A dependency that would close a cycle is refused with the path; a cycle forced into sqlite gets hold dep_cycle."""
import json
import sqlite3


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, "--tier", "low", *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def _db(abx):
    return sqlite3.connect(next(abx.home.rglob("*.db")))


def _chain(abx):
    a = _add(abx, "A")
    b = _add(abx, "B", "--dep", a)
    c = _add(abx, "C", "--dep", b)
    return a, b, c


def _ndeps(abx):
    return _db(abx).execute("select count(*) from deps").fetchone()[0]


def test_edit_refuses_cycle_with_path(abx):
    a, b, c = _chain(abx)
    code, o, e = abx("edit", a, "--dep", c)
    assert code != 0
    assert f"{a} -> {c} -> {b} -> {a}" in e
    assert _ndeps(abx) == 2


def test_self_dep_refused(abx):
    a = _add(abx, "A")
    code, o, e = abx("edit", a, "--dep", a)
    assert code != 0 and f"{a} -> {a}" in e


def test_add_with_dep_cannot_cycle(abx):
    a, b, c = _chain(abx)
    d = _add(abx, "D", "--dep", c)
    assert _ndeps(abx) == 3 and d


def test_allow_dep_cycle_flag(abx):
    a, b, c = _chain(abx)
    code, o, e = abx("edit", a, "--dep", c, "--allow-dep-cycle", "intentional test")
    assert code == 0, e
    assert _ndeps(abx) == 3


def _forced(abx):
    a, b, c = _chain(abx)
    db = _db(abx)
    db.execute("insert into deps(task_id,dep_id) values(?,?)", (a, c))
    db.commit()
    return a, b, c


def test_forced_cycle_hold_in_ls_and_snapshot(abx):
    a, b, c = _forced(abx)
    d = _add(abx, "D", "--dep", c)  # downstream of the cycle, not in it
    o = abx("ls", "--in-flight")[1]
    assert "dep_cycle 3" in o and "dep-waiting 1" in o
    items = json.loads(abx("--json", "ls", "--all")[1])
    items = items["items"] if isinstance(items, dict) else items
    holds = {x["id"]: (x.get("hold") or {}).get("reason") for x in items}
    assert holds[a] == holds[b] == holds[c] == "dep_cycle"
    assert holds[d] is None
    snap = json.loads(abx("snapshot", "--json")[1])
    by = {t["id"]: t for t in snap["tasks"]}
    assert all(by[x]["hold"]["reason"] == "dep_cycle" for x in (a, b, c))
    assert by[a]["hold"]["cycle"].startswith(a) and by[d]["hold"] is None
