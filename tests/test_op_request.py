"""`ab op-request`: a non-admin actor queues a runner reload / allowlisted config change; the runner tick applies one per tick."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import store, runner as R  # noqa: E402

ARG = 'extra_args.claude=["--max-turns","5"]'


def conn(abx):
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name), isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def runner(c, logs):
    r = R.Runner.__new__(R.Runner)
    r.conn, r.log = c, logs.append
    return r


def test_add_ls_does_not_touch_config(abx):
    rc, before, _ = abx("config", "get", "extra_args")
    rc, o, e = abx("op-request", "add", "--kind", "config", "--arg", ARG, "--why", "test", "--as", "worker1",
                   env={"AB_TASK": "t_abc"})
    assert rc == 0, e
    rc, o, e = abx("op-request", "ls", "--json")
    rows = json.loads(o)
    assert len(rows) == 1 and rows[0]["state"] == "pending" and rows[0]["actor"] == "worker1" and rows[0]["task_id"] == "t_abc"
    assert abx("config", "get", "extra_args")[1] == before


def test_tick_applies_once_config(abx):
    abx("op-request", "add", "--kind", "config", "--arg", ARG, "--why", "t")
    c, logs = conn(abx), []
    try:
        r = runner(c, logs)
        r.drain_op_requests()
        assert store.cfg(c, "extra_args")["claude"] == ["--max-turns", "5"]
        row = store.op_requests(c)[0]
        assert row["state"] == "applied" and json.loads(row["result"])["after"] == ["--max-turns", "5"]
        assert len(logs) == 1 and "before=" in logs[0] and "after=" in logs[0] and "kind=config" in logs[0]
        with store.tx(c):
            store.cfg_set(c, "extra_args", {}, "x")
        r.drain_op_requests()   # nothing pending: nothing re-applied
        assert store.cfg(c, "extra_args") == {} and len(logs) == 1
    finally:
        c.close()


def test_one_per_tick(abx):
    abx("op-request", "add", "--kind", "config", "--arg", ARG, "--why", "a")
    abx("op-request", "add", "--kind", "config", "--arg", 'extra_args.codex=["-x"]', "--why", "b")
    c = conn(abx)
    try:
        runner(c, []).drain_op_requests()
        assert [x["state"] for x in store.op_requests(c)] == ["applied", "pending"]
    finally:
        c.close()


def test_reload_once_and_leftover_applying(abx):
    abx("op-request", "add", "--kind", "reload", "--why", "new code")
    c, logs = conn(abx), []
    try:
        r = runner(c, logs)
        r.drain_op_requests()
        assert store.cfg(c, "stop") == "reload" and store.op_requests(c)[0]["state"] == "applying"
        with store.tx(c):   # the runner re-exec'd (reload() clears stop); the row is left 'applying'
            store.cfg_set(c, "stop", "", "runner")
            c.execute("UPDATE op_requests SET applied_at=applied_at-100")
        r.drain_op_requests()
        row = store.op_requests(c)[0]
        assert row["state"] == "applied" and "warning" in row["result"]
        assert store.cfg(c, "stop") == "" and any("WARNING" in m for m in logs)
        r.drain_op_requests()   # never re-executed
        assert store.cfg(c, "stop") == ""
    finally:
        c.close()


def test_outside_allowlist_refused_at_add(abx):
    rc, o, e = abx("op-request", "add", "--kind", "config", "--arg", "hooks.x=1", "--why", "t")
    assert rc != 0 and "extra_args" in e and "extra_args_tier" in e
    assert json.loads(abx("op-request", "ls", "--json")[1]) == []


def test_status_pending_line_and_stale(abx):
    abx("op-request", "add", "--kind", "reload", "--why", "t")
    rc, o, e = abx("runner", "status")
    assert "requests: 1 pending (oldest" in o
    assert json.loads(abx("op-request", "ls", "--stale", "--older", "30m", "--json")[1]) == []
    c = conn(abx)
    with store.tx(c):
        c.execute("UPDATE op_requests SET created_at=created_at-4000")
    c.close()
    assert len(json.loads(abx("op-request", "ls", "--stale", "--older", "30m", "--json")[1])) == 1
