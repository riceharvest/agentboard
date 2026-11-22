"""RAM gate / board slot cap recorded as a hold on the ready cards the runner will not launch. Throwaway AB_HOME."""
import json

from agentboard import runner, store


def _j(abx, *args):
    code, out, err = abx("--json", *args)
    assert code == 0, (args, out, err)
    return json.loads(out) if out.strip() else None


def _conn(abx):
    return store.connect(abx.home / store.DB_PATH.name)


def _tick(c, slots):
    r = runner.Runner(c)
    r.fill(slots)
    store.record_holds(c, r.waiting, r.busy_dirs(), r.gates)
    return r


def test_ram_gate_hold_emitted_and_cleared(abx, monkeypatch):
    t = _j(abx, "add", "cheap", "--tier", "low")["id"]
    c = _conn(abx)
    try:
        store.cfg_set(c, "min_free_gib", 999, "test")
        monkeypatch.setattr(runner, "free_gib", lambda: 3.25)
        r = _tick(c, 1)
        assert r.gates[t].startswith("ram_gate:3.") and r.gates[t].endswith("GiB<999")
        h = store.current_hold(c, t)
        assert h["holder"] == "runner" and h["reason"].startswith("ram_gate:")
        # a moving free-RAM figure refreshes the same hold, it does not stack events
        monkeypatch.setattr(runner, "free_gib", lambda: 3.5)
        _tick(c, 1)
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='hold' AND task_id=?", (t,)).fetchone()[0] == 1
        o = abx("ls", "--ready")[1]
        assert "cross RAM gate: 3." in o and "GiB free < 999" in o
        stuck = abx("ls", "--stuck")[1]
        assert t in stuck and "ram_gate: 1" in stuck
        item = _j(abx, "ls", "--ready")["items"][0]
        assert item["hold"]["holder"] == "runner" and item["hold"]["since"]
        # gate lifted: nothing launches (0 slots) and the hold is cleared
        _tick(c, 0)
        assert store.current_hold(c, t) is None
        assert c.execute("SELECT COUNT(*) FROM events WHERE kind='hold_cleared' AND task_id=?", (t,)).fetchone()[0] == 1
        assert "ram_gate" not in abx("ls", "--ready")[1]
    finally:
        c.close()


def test_hold_reason_takes_gates(abx):
    t = _j(abx, "add", "cheap", "--tier", "low")["id"]
    c = _conn(abx)
    try:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (t,)).fetchone()
        assert store.hold_reason(c, row) is None
        assert store.hold_reason(c, row, gates={t: "board_slots:main 2/2"}) == "board_slots:main 2/2"
        store.record_holds(c, gates={t: "board_slots:main 2/2"})
        h = store.current_hold(c, t)
        assert (h["reason"], h["holder"]) == ("board_slots:main 2/2", "runner")
        assert "board slots full: main 2/2" in abx("ls", "--ready")[1]
    finally:
        c.close()
