"""A run killed by a signal that did not come from the runner: with no output it is `interrupted` (no attempt used),
with output it stays a failed attempt. Reload leaves in-flight runs alive. Throwaway AB_HOME, no network, no model."""
import json
import os
import signal
import subprocess
import sys
import time
import types

from agentboard import runner, store
from conftest import ROOT, clean_env


def _add(abx, cmd):
    code, out, err = abx("--json", "add", "sig card", "--cmd", cmd)
    assert code == 0, err
    return json.loads(out)["id"]


def _runner(abx):
    c = store.connect(abx.home / store.DB_PATH.name)
    return c, runner.Runner(c, log=types.SimpleNamespace(write=lambda s: None, flush=lambda: None))


def _spawn_and_term(abx, tmp_path, cmd, wait=0.0):
    tid = _add(abx, cmd)
    c, r = _runner(abx)
    task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert r.spawn_cmd(task, str(tmp_path / "wd"))
    ch = next(iter(r.children.values()))
    time.sleep(wait)
    pgid = ch.pid
    os.kill(ch.pid, signal.SIGTERM)  # from outside the runner: no kill_reason
    deadline = time.time() + 10
    while r.children and time.time() < deadline:
        r.poll_children()
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)  # reap a leftover sleep
    except OSError:
        pass
    assert not r.children
    run = c.execute("SELECT * FROM runs WHERE task_id=?", (tid,)).fetchone()
    task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return run, task


def test_sigterm_no_output_is_interrupted_no_attempt(abx, tmp_path):
    run, task = _spawn_and_term(abx, tmp_path, "exec sleep 120", wait=0.3)
    assert run["outcome"] == "interrupted"
    assert run["detail"] == "killed by signal 15 before any output"
    assert task["status"] == "open" and task["owner"] is None
    assert (task["attempts"] or 0) == 0
    assert task["not_before"] is not None or not store.cfg(store.connect(abx.home / store.DB_PATH.name), "backoff")


def test_sigterm_after_output_is_failed_attempt(abx, tmp_path):
    run, task = _spawn_and_term(abx, tmp_path, "echo started; sleep 120", wait=0.5)
    assert run["outcome"] == "failed"
    assert task["attempts"] == 1


def test_reload_leaves_runs_alive(abx, tmp_path):
    tid = _add(abx, "sleep 6; echo finished")
    env = clean_env(abx.home)
    log = open(tmp_path / "runner.log", "wb")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=env, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    c = store.connect(abx.home / store.DB_PATH.name)
    try:
        store.cfg_set(c, "poll_s", 1, "test")
        run = None
        deadline = time.time() + 30
        while time.time() < deadline and not run:
            run = c.execute("SELECT * FROM runs WHERE task_id=?", (tid,)).fetchone()
            time.sleep(0.3)
        assert run is not None, (tmp_path / "runner.log").read_text()
        assert abx("runner", "reload")[0] == 0
        deadline = time.time() + 30
        while time.time() < deadline and "reloading with 1 run" not in (tmp_path / "runner.log").read_text():
            time.sleep(0.3)
        assert "reloading with 1 run(s) in flight" in (tmp_path / "runner.log").read_text()
        row = c.execute("SELECT ended_at, outcome FROM runs WHERE id=?", (run["id"],)).fetchone()
        assert row["ended_at"] is None and p.poll() is None  # same runner pid, run still going
        deadline = time.time() + 40
        while time.time() < deadline:
            row = c.execute("SELECT ended_at, outcome FROM runs WHERE id=?", (run["id"],)).fetchone()
            if row["ended_at"]:
                break
            time.sleep(0.5)
        assert row["outcome"] == "done", dict(row)
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
