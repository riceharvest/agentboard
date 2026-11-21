"""runs.wake: the card + scheduler settings a run woke on, written at spawn, read-only after. Throwaway AB_HOME."""
import json

from agentboard import harness as H, runner, store


def _j(abx, *args):
    code, out, err = abx("--json", *args)
    assert code == 0, (args, out, err)
    return json.loads(out) if out.strip() else None


def _conn(abx):
    return store.connect(abx.home / store.DB_PATH.name)


def _spawn(c, tid, monkeypatch, tmp_path):
    monkeypatch.setattr(H.ADAPTERS["claude"], "build", lambda brief, cand, ep, extra: (["true"], {}))
    r = runner.Runner(c)
    row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert r.spawn(row, H.Candidate("claude", "sonnet", "low"), "claude", str(tmp_path / "w"))
    return r


def _end(c, tid):
    c.execute("UPDATE runs SET ended_at=1, outcome='released' WHERE task_id=?", (tid,))
    c.execute("UPDATE tasks SET status='open', owner=NULL, run_id=NULL, attempts=attempts+1 WHERE id=?", (tid,))
    c.commit()


def _wake(c, tid):
    rows = c.execute("SELECT wake FROM runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    return [json.loads(r[0]) if r[0] else None for r in rows]


def test_wake_snapshot_frozen_and_per_run(abx, tmp_path, monkeypatch):
    dep = _j(abx, "add", "dep")["id"]
    _j(abx, "claim", dep); _j(abx, "done", dep, "ok")
    tid = _j(abx, "add", "card", "-l", "a=1", "--priority", "5", "--dep", dep)["id"]
    c = _conn(abx)
    try:
        store.cfg_set(c, "slots", 3, "t")
        store.cfg_set(c, "launch_per_tick", 4, "t")
        _spawn(c, tid, monkeypatch, tmp_path)
        w = _wake(c, tid)[0]
        assert set(w) == {"at", "board", "status", "priority", "tier", "attempts", "labels", "deps", "cfg", "bucket", "candidate"}
        assert w["status"] == "open" and w["priority"] == 5 and w["attempts"] == 0 and w["deps"] == [dep]
        assert w["labels"]["a"] == "1"
        assert w["cfg"] == {"slots": 3, "launch_per_tick": 4, "gating": store.cfg(c, "gating"), "paused": False}
        assert w["bucket"] == "claude" and w["candidate"] == "claude:sonnet@low"
        c.execute("UPDATE tasks SET labels='{\"a\":\"2\"}', priority=9 WHERE id=?", (tid,))
        c.commit()
        store.cfg_set(c, "slots", 8, "t")
        store.cfg_set(c, "launch_per_tick", 1, "t")
        assert _wake(c, tid)[0] == w and _wake(c, tid)[0] == w
        _end(c, tid)
        _spawn(c, tid, monkeypatch, tmp_path)
        w1, w2 = _wake(c, tid)
        assert w1 == w
        assert w2["attempts"] == 1 and w2["priority"] == 9 and w2["labels"]["a"] == "2" and w2["cfg"]["slots"] == 8
        assert w2["at"] >= w1["at"]
        shown = _j(abx, "show", tid)["runs"]
        assert isinstance(shown[0]["wake"], dict) and shown[1]["wake"]["attempts"] == 1
        code, out, _ = abx("wake", tid)
        assert code == 0 and json.loads(out) == w2
    finally:
        c.close()


def test_wake_null_and_migration(abx, tmp_path, monkeypatch):
    tid = _j(abx, "add", "old")["id"]
    c = _conn(abx)
    try:
        code, out, err = abx("wake", tid)
        assert code == 2 and "Traceback" not in err
        c.execute("INSERT INTO runs(task_id,harness,started_at) VALUES(?,?,?)", (tid, "claude", 1.0))
        c.commit()
        assert _j(abx, "show", tid)["runs"][0]["wake"] is None
        code, out, err = abx("wake", tid)
        assert code == 2 and "predates" in err and len(err.strip().splitlines()) == 1 and "Traceback" not in err
        c.execute("DELETE FROM runs")
        c.commit()
        c.execute("ALTER TABLE runs DROP COLUMN wake")  # a DB made by the previous schema
        c.commit()
    finally:
        c.close()
    assert _j(abx, "show", tid)["runs"] == []  # reopening migrates the column back
    c = _conn(abx)
    try:
        assert "wake" in {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        _spawn(c, tid, monkeypatch, tmp_path)
        assert _wake(c, tid)[0]["labels"] is not None
    finally:
        c.close()
