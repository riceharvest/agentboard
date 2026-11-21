"""Runner records why a ready card is not launched: hold / hold_cleared events, `ab show`, `ab ls --stuck`. Throwaway AB_HOME."""
import json

from agentboard import runner, store


def _j(abx, *args):
    code, out, err = abx("--json", *args)
    assert code == 0, (args, out, err)
    return json.loads(out) if out.strip() else None


def _conn(abx):
    return store.connect(abx.home / store.DB_PATH.name)


def _tick(c):
    """The hold part of Runner.tick(): fill (no free slots) then the hold scan."""
    r = runner.Runner(c)
    r.fill(0)
    store.record_holds(c, r.waiting, r.busy_dirs())


def test_hold_locked_then_cleared(abx, tmp_path):
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    holder = _j(abx, "add", "holder", "--workdir", str(tmp_path))["id"]
    _j(abx, "claim", holder)
    c = _conn(abx)
    try:
        lb = json.loads(c.execute("SELECT labels FROM tasks WHERE id=?", (holder,)).fetchone()[0] or "{}")
        lb["worktree_dir"] = str(wt)
        c.execute("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(lb), holder))
        c.commit()
        t = _j(abx, "add", "stuck", "--workdir", str(wt / "sub"))["id"]
        _tick(c)
        _tick(c)
        out = abx("show", t)[1]
        assert f"hold=workdir_locked:{store.norm_dir(wt)} since " in out
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='hold' AND task_id=?", (t,)).fetchone()[0] == 1
        assert "workdir_locked" in abx("ls", "--stuck")[1]
        c.execute("UPDATE tasks SET workdir=? WHERE id=?", (str(tmp_path), t))  # clear the conflict
        c.commit()
        _tick(c)
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='hold_cleared' AND task_id=?", (t,)).fetchone()[0] == 1
        assert "hold=workdir" not in abx("show", t)[1]
    finally:
        c.close()


def test_hold_event_volume_and_stuck_groups(abx):
    ids = [_j(abx, "add", f"s{i}", "--manual")["id"] for i in range(3)]
    c = _conn(abx)
    try:
        for i in ids:  # runnable=0 with no named hold: the unexplained case
            c.execute("UPDATE tasks SET labels='{}' WHERE id=?", (i,))
        c.commit()
        for _ in range(10):
            _tick(c)
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='hold'").fetchone()[0] == 3
    finally:
        c.close()
    out = abx("ls", "--stuck")[1]
    assert "runnable_flag_zero:" in out and ": 3" in out
    assert "hold=runnable_flag_zero" in abx("show", ids[0])[1]
