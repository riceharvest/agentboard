"""`ab ls` / `ab show` print the run count so a first-try done card (attempts=0) is not read as never run."""
import json
import sqlite3


def _runs(abx, tid, n):
    db = next(abx.home.rglob("*.db"))
    c = sqlite3.connect(db)
    for _ in range(n):
        c.execute("INSERT INTO runs(task_id,harness,started_at,ended_at,outcome) VALUES(?,?,1,2,'x')", (tid, "claude"))
    c.commit(); c.close()


def test_run_count_shown(abx):
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    assert abx("claim", tid)[0] == 0
    _runs(abx, tid, 1)
    assert abx("done", tid, "ok")[0] == 0
    assert "runs=1 attempts=0/3" in abx("show", tid)[1]
    assert "runs=1 try 0/3" in abx("ls", "--status", "all")[1]
    ls = json.loads(abx("--json", "ls", "--status", "all")[1])
    assert ls[0]["runs"] == 1 and isinstance(json.loads(abx("--json", "show", tid)[1])["runs"], list)
