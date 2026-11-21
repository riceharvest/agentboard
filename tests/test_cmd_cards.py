"""Command cards (`ab add --cmd`): the runner runs the shell command itself, no harness / model / bucket, settled by exit code.
Throwaway AB_HOME, real runner process."""
import json
import subprocess
import sys
import time

import pytest

from agentboard import store
from conftest import ROOT, clean_env


@pytest.fixture
def rig(abx, tmp_path):
    procs = []

    def j(*args, rc=0):
        code, out, err = abx("--json", *args)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"), ("backoff", "1s")):
        j("config", "set", k, v)
    j("runner", "pause")
    logf = open(tmp_path / "runner.log", "ab")
    procs.append(subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home),
                                  stdout=logf, stderr=logf, start_new_session=True))
    j("runner", "slots", "2")
    j.tmp = tmp_path
    yield j
    try:
        j("runner", "stop", "--now")
    except AssertionError:
        pass
    for p in procs:
        p.terminate()


def wait(cond, secs=30):
    for _ in range(secs * 2):
        if cond():
            return True
        time.sleep(0.5)
    return False


def test_three_exit_paths_deps_and_no_usage(rig):
    j = rig
    before = j("usage", "--by", "task")
    ok = j("add", "ok card", "--cmd", "echo noise; echo ok", "--cwd", str(rig.tmp))["id"]
    dep = j("add", "after ok", "--cmd", "echo second", "--dep", ok)["id"]
    bad = j("add", "bad card", "--cmd", "echo a; echo oops >&2; exit 3", "--max-attempts", "1")["id"]
    ref = j("add", "refused card", "--cmd", "echo not now; exit 75")["id"]
    fixed = j("add", "fixed result", "--cmd", "echo ignored", "--result", "chosen")["id"]
    assert j("show", ok)["kind"] == "cmd" and j("show", ok)["cmd"] == "echo noise; echo ok"
    assert j("show", dep)["state"] == "waiting"
    j("runner", "resume")

    assert wait(lambda: j("show", ok)["status"] == "done"), open(rig.tmp / "runner.log").read()
    t = j("show", ok)
    assert t["result"] == "ok" and t["attempts"] == 0
    assert t["runs"][0]["harness"] == "cmd" and t["runs"][0]["bucket"] is None and t["runs"][0]["outcome"] == "done"
    assert any("noise" in n["body"] and "ok" in n["body"] for n in t["notes"])

    assert wait(lambda: j("show", dep)["status"] == "done")
    assert j("show", dep)["runs"][0]["started_at"] >= t["runs"][0]["ended_at"]  # only started once its dep was done
    assert j("show", fixed)["result"] if wait(lambda: j("show", fixed)["status"] == "done") else False
    assert j("show", fixed)["result"] == "chosen"

    assert wait(lambda: j("show", bad)["status"] == "dead"), open(rig.tmp / "runner.log").read()
    b = j("show", bad)
    assert "exited 3" in b["result"] and "oops" in b["result"] and b["attempts"] == 1

    # exit 75: released, no attempt used, retried later (the runner keeps retrying it after the backoff)
    assert wait(lambda: len(j("show", ref)["runs"]) >= 1 and j("show", ref)["runs"][0]["outcome"] == "released")
    r = j("show", ref)
    assert r["attempts"] == 0 and r["status"] in ("open", "claimed")
    assert r["runs"][0]["exit_code"] == 75
    assert any("without using an attempt" in n["body"] for n in r["notes"])

    # no usage rows, no bucket rows for these runs
    assert j("usage", "--by", "task") == before
    assert all(row.get("bucket") for row in j("usage", "--by", "bucket")) if j("usage", "--by", "bucket") else True
    snap = j("snapshot")
    assert {t["kind"] for t in snap["tasks"]} == {"cmd"}
    j("cancel", ref)


def test_runner_status_lists_running_cmd_and_kill_is_no_attempt(rig):
    j = rig
    t = j("add", "slow", "--cmd", "echo started; sleep 60")["id"]
    j("runner", "resume")
    assert wait(lambda: j("runner", "status")["children"]), open(rig.tmp / "runner.log").read()
    ch = j("runner", "status")["children"][0]
    assert ch["harness"] == "cmd" and ch["cmd"] == "echo started; sleep 60" and ch["bucket"] is None
    j("kill", str(ch["run"]))
    assert wait(lambda: j("show", t)["runs"][0]["outcome"] == "killed")
    assert j("show", t)["attempts"] == 0


def test_env_exported_and_timeout_fails(rig):
    j = rig
    e = j("add", "env", "--cmd", 'echo "$AB_TASK $AB_ACTOR $AB_BOARD"')["id"]
    to = j("add", "timeout", "--cmd", "sleep 60", "--timeout", "2s", "--max-attempts", "1")["id"]
    j("runner", "resume")
    assert wait(lambda: j("show", e)["status"] == "done")
    res = j("show", e)["result"]
    assert res.startswith(f"{e} cmd#") and res.endswith(" main"), res
    assert wait(lambda: j("show", to)["status"] == "dead", 40)
    assert j("show", to)["runs"][0]["outcome"] == "timeout"


def test_unknown_card_kind_is_refused_not_run_as_model(rig, abx):
    import sqlite3
    j = rig
    j("runner", "pause")
    tid = j("add", "future kind", "--cmd", "echo hi", "--cwd", str(rig.tmp))["id"]
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name))
    c.execute("UPDATE tasks SET kind='cmd-v9' WHERE id=?", (tid,))
    c.commit()
    c.close()
    j("runner", "resume")
    assert wait(lambda: j("show", tid)["status"] == "dead")
    t = j("show", tid)
    assert "cmd-v9" in t["result"]
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name))
    assert c.execute("SELECT count(*) FROM runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
    c.close()
