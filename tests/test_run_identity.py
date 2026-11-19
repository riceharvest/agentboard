"""`ab` infers task/actor/board from a live run's home (<root>/profiles/ab-run-N) when AB_* are not exported.
Positive: `ab done` / `ab done "summary"` from the run home succeed. Negative: anything ambiguous, stale, foreign or
not a run home refuses with an error and writes nothing."""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

sys.path.insert(0, ROOT)
from agentboard import runner as R, store  # noqa: E402


def _ab(abx, cwd, args, extra_env=None):
    env = clean_env(abx.home, **(extra_env or {}))
    p = subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=cwd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _db(abx):
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name))
    c.row_factory = sqlite3.Row
    return c


def _snapshot(abx):
    c = _db(abx)
    try:
        return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("tasks", "notes", "events", "boards")}
    finally:
        c.close()


@pytest.fixture
def live_run(abx, tmp_path):
    """A claimed task held by live hermes run 7 whose home is <root>/profiles/ab-run-7 with the runner's marker."""
    def make(run_id=7, task_title="inferred task", board="main", pid=None):
        code, out, err = abx("--json", "add", task_title, "--board", board)
        assert code == 0, err
        tid = json.loads(out)["id"]
        proc = None
        if pid is None:
            proc = subprocess.Popen(["sleep", "120"])
            pid = proc.pid
        pstart = R.pid_start(pid)
        home = tmp_path / "hermes" / "profiles" / f"ab-run-{run_id}"
        home.mkdir(parents=True, exist_ok=True)
        (home / "ab-run.json").write_text(json.dumps({"run": run_id, "task": tid, "actor": f"hermes#{run_id}",
                                                      "board": board, "ab_home": str(abx.home)}))
        c = _db(abx)
        with c:
            c.execute("INSERT INTO runs(id, task_id, harness, pid, pid_start, started_at) VALUES (?,?,?,?,?,?)",
                      (run_id, tid, "hermes", pid, pstart, time.time()))
            c.execute("UPDATE tasks SET status='claimed', owner=?, run_id=?, lease_until=NULL WHERE id=?",
                      (f"hermes#{run_id}", run_id, tid))
        c.close()
        procs.append(proc)
        return {"task": tid, "home": home, "root": tmp_path / "hermes", "pid": pid}
    procs = []
    yield make
    for p in procs:
        if p:
            p.kill()
            p.wait()


def _status(abx, tid):
    c = _db(abx)
    row = c.execute("SELECT status, owner, board FROM tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    return tuple(row)


def test_done_without_task_arg_resolves_from_run_home(abx, live_run):
    r = live_run()
    code, out, err = _ab(abx, str(r["home"]), ["done", ""], {"HERMES_HOME": str(r["home"])})
    assert code == 0, (out, err)
    assert _status(abx, r["task"])[0] == "done"


def test_done_with_summary_in_id_slot_when_env_empty(abx, live_run):
    """`ab done $AB_TASK "summary"` with AB_TASK empty reaches ab as `done "summary"`."""
    r = live_run(run_id=8)
    code, out, err = _ab(abx, str(r["home"]), ["done", "summary text"], {"HERMES_HOME": str(r["home"])})
    assert code == 0, (out, err)
    assert _status(abx, r["task"])[0] == "done"
    c = _db(abx)
    res = c.execute("SELECT result FROM tasks WHERE id=?", (r["task"],)).fetchone()[0]
    c.close()
    assert res == "summary text"


def test_note_from_cwd_run_home(abx, live_run):
    r = live_run(run_id=9)
    code, out, err = _ab(abx, str(r["home"]), ["note", "progress text"])
    assert code == 0, (out, err)
    c = _db(abx)
    body = c.execute("SELECT body FROM messages WHERE thread=? ORDER BY id DESC", (r["task"],)).fetchone()[0]
    c.close()
    assert body == "progress text"


def test_non_run_cwd_refuses_without_writing(abx, tmp_path):
    code, out, err = abx("--json", "add", "untouched", "--board", "main")
    tid = json.loads(out)["id"]
    before = _snapshot(abx)
    other = tmp_path / "plain"
    other.mkdir()
    code, out, err = _ab(abx, str(other), ["done", "", "x"])
    assert code == 1 and "no task id" in err, (code, err)
    assert _snapshot(abx) == before
    assert _status(abx, tid)[0] == "open"


def test_ambiguous_home_refuses(abx, live_run, tmp_path):
    a = live_run(run_id=10)
    b = live_run(run_id=11)
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(b["home"]), ["done", "", "x"], {"HERMES_HOME": str(a["home"])})
    assert code == 1 and "ambiguous" in err, (code, err)
    assert _snapshot(abx) == before


def test_stale_ended_run_refuses(abx, live_run):
    r = live_run(run_id=12)
    c = _db(abx)
    with c:
        c.execute("UPDATE runs SET ended_at=? WHERE id=12", (time.time(),))
    c.close()
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x"])
    assert code == 1 and "ended" in err, (code, err)
    assert _snapshot(abx) == before


def test_dead_process_refuses(abx, live_run):
    dead = subprocess.Popen(["sleep", "120"])
    r = live_run(run_id=13, pid=dead.pid)  # records pid_start while alive
    dead.kill()
    dead.wait()
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x"])
    assert code == 1 and "gone" in err, (code, err)
    assert _snapshot(abx) == before


def test_missing_marker_refuses(abx, live_run):
    r = live_run(run_id=14)
    (r["home"] / "ab-run.json").unlink()
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x"])
    assert code == 1 and "no ab-run.json" in err, (code, err)
    assert _snapshot(abx) == before


def test_live_run_on_board_a_never_resolves_to_board_b(abx, live_run):
    abx("--json", "boards", "create", "b")
    r = live_run(run_id=15, board="main")
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x"], {"AB_BOARD": "b"})
    assert code == 1 and "not b" in err, (code, err)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x", "--board", "b"])
    assert code == 1, (code, err)
    assert _snapshot(abx) == before
    assert _status(abx, r["task"]) == ("claimed", "hermes#15", "main")


def test_explicit_env_task_is_not_overridden(abx, live_run):
    r = live_run(run_id=16)
    other = live_run(run_id=17)
    before = _snapshot(abx)
    code, out, err = _ab(abx, str(r["home"]), ["done", "", "x"], {"AB_TASK": other["task"]})
    assert code == 1 and "not the task of run 16" in err, (code, err)
    assert _snapshot(abx) == before
