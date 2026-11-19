"""`ab snapshot` and the control verbs a UI uses. Every test runs against a throwaway AB_HOME."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

SCHEMA = os.path.join(ROOT, "agentboard", "snapshot.schema.json")


def snap(abx, *args):
    rc, out, err = abx("snapshot", "--json", *args)
    assert rc == 0, (out, err)
    return json.loads(out)


def j(abx, *args, rc=0):
    code, out, err = abx("--json", *args)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


@pytest.fixture
def big(abx):
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_board.py"), "2000"], capture_output=True, text=True,
                       env=clean_env(abx.home))
    assert p.returncode == 0, p.stderr
    return abx


def test_snapshot_matches_schema(big):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.load(open(SCHEMA))
    jsonschema.Draft7Validator.check_schema(schema)
    v = jsonschema.Draft7Validator(schema)
    d = snap(big, "--events-after", "0", "--tail", "1:200")
    assert not list(v.iter_errors(d))
    assert snap(big, "-B", "web")["board"] == "web"
    bad = dict(d)
    del bad["runner"]
    assert list(v.iter_errors(bad)), "the schema must reject a snapshot without a runner"
    bad = json.loads(json.dumps(d))
    bad["tasks"][0]["state"] = "bogus"
    assert list(v.iter_errors(bad))


def test_snapshot_content_and_speed(big):
    snap(big)  # warm-up
    d = snap(big)
    assert d["tasks_open"] == 1500 and d["tasks_closed_shown"] == 50 and len(d["tasks"]) == 1550
    assert {t["state"] for t in d["tasks"]} >= {"ready", "waiting", "done"}
    assert d["took_ms"] < 500, "queries + assembly should take tens of ms on 2,000 tasks"
    main = next(b for b in d["boards"] if b["name"] == "main")
    web = next(b for b in d["boards"] if b["name"] == "web")
    assert web["paused"] and web["max_slots"] == 1 and main["counts"]["open"] == 1200
    assert any(t["harness"] == "claude" for t in d["tasks"]) and any(not t["runnable"] for t in d["tasks"])
    assert any(t["deps"] for t in d["tasks"]) and any(t["deps_open"] for t in d["tasks"])
    assert d["tiers"]["order"] == ["low", "medium", "high", "max"]
    assert d["active_windows"] == ["always"] and d["runner"]["slots_effective"] == 3
    assert [w["name"] for w in d["watchers"]] == ["on-dead"]
    assert d["limits"][0]["scope"] == "bucket:claude-sub"
    # plan numbers come from the stored reading, the guard from the caps
    assert d["plan"]["claude_plan_pct"]["seven_day"] == 62.0 and 55 < d["plan"]["weekly_pace_pct"] < 70
    assert d["plan"]["guards"] and any(u["bucket"] == "claude-sub" and u["cap_hit"] for u in d["usage"]) and len(d["usage"]) == 5
    assert snap(big, "--closed", "0")["tasks_closed_shown"] == 0


def test_snapshot_is_read_only_and_offline(big):
    def content():  # main file + write-ahead log (SQLite may create an empty -wal on open; that is not a write)
        return {f.name: f.read_bytes() for f in big.home.glob("*.db*") if not f.name.endswith("-shm") and f.stat().st_size}
    before = content()
    snap(big, "--events-after", "0")
    assert content() == before, "snapshot must not write to the board"


def test_events_after_returns_only_newer(big):
    first = snap(big)
    assert first["events"] == [] and first["events_cursor"] > 0
    cur = first["events_cursor"]
    assert snap(big, "--events-after", str(cur))["events"] == []
    j(big, "add", "later task")
    j(big, "add", "another task")
    d = snap(big, "--events-after", str(cur))
    assert [e["kind"] for e in d["events"]] == ["created", "created"] and all(e["id"] > cur for e in d["events"])
    assert d["events_cursor"] == d["events"][-1]["id"]
    d = snap(big, "--events-after", "0", "--events-limit", "3")
    assert len(d["events"]) == 3 and d["events_truncated"] and d["events"][-1]["id"] == d["events_cursor"]
    assert [e["id"] for e in d["events"]] == sorted(e["id"] for e in d["events"])


def test_tail_returns_last_bytes_without_ansi(big):
    t = snap(big, "--tail", "1:60")["tail"]
    assert t["run"] == 1 and t["out"].endswith("line 499 õé\n") and "\x1b" not in t["out"] + t["err"]
    assert t["out_truncated"] and t["out_size"] > 60 and not t["err_truncated"] and t["err"] == "warning: something\n"
    assert len(t["out"].encode()) <= 60
    assert snap(big, "--tail", "99:10")["tail"]["error"] == "no run 99"
    assert big("snapshot", "--tail", "nonsense")[0] == 1
    rc, out, _ = big("tail", "--run", "1", "--bytes", "30")
    assert rc == 0 and "499 õé" in out and "--- stderr ---" in out
    assert "task_id" in j(big, "tail", "--run", "1", "--bytes", "30")
    j(big, "tail", "--run", "42", rc=1)


def test_fresh_home_and_errors(abx):
    d = snap(abx)  # no db yet: falls back to creating it
    assert d["tasks"] == [] and d["runner"]["running"] is False and [b["name"] for b in d["boards"]] == ["main"]
    rc, out, _ = abx("snapshot", "--json", "-B", "nope")
    assert rc == 1 and json.loads(out)["type"] == "LookupError"
    assert json.load(open(SCHEMA))["title"] == "agentboard snapshot"
    assert json.loads(abx("snapshot", "--schema")[1])["definitions"]["task"]


def test_control_verbs(abx):
    # runner settings (no runner needed: they are settings it reads every tick)
    assert j(abx, "runner", "pause")["ok"] and snap(abx)["runner"]["paused"]
    assert j(abx, "runner", "resume")["ok"] and not snap(abx)["runner"]["paused"]
    assert j(abx, "runner", "drain")["ok"] and snap(abx)["runner"]["draining"]
    assert j(abx, "runner", "reload")["action"] == "reload" and snap(abx)["runner"]["stop"] == "reload"
    j(abx, "runner", "resume")
    assert j(abx, "runner", "slots", "5") == {"slots": 5} and snap(abx)["runner"]["slots_setting"] == 5
    j(abx, "runner", "slots", "x", rc=1)
    j(abx, "runner", "slots", rc=1)
    # boards
    j(abx, "boards", "create", "web")
    assert next(b for b in j(abx, "boards", "set", "web", "--pause") if b["name"] == "web")["paused"] == 1
    assert next(b for b in j(abx, "boards", "set", "web", "--max-slots", "2") if b["name"] == "web")["max_slots"] == 2
    assert next(b for b in snap(abx)["boards"] if b["name"] == "web")["paused"] is True
    assert next(b for b in j(abx, "boards", "set", "web", "--resume") if b["name"] == "web")["paused"] == 0
    j(abx, "boards", "set", "nope", "--pause", rc=1)
    # tasks
    a = j(abx, "add", "first", "--tier", "low")["id"]
    b = j(abx, "add", "second")["id"]
    assert j(abx, "edit", a, "--priority", "7", "--tier", "high", "--harness", "claude", "--model", "sonnet", "--dep", b)["id"] == a
    t = next(x for x in snap(abx)["tasks"] if x["id"] == a)
    assert (t["priority"], t["tier"], t["harness"], t["model"], t["deps"], t["state"]) == (7, "high", "claude", "sonnet", [b], "waiting")
    j(abx, "edit", a, "--rm-dep", b, "--harness", "")
    t = next(x for x in snap(abx)["tasks"] if x["id"] == a)
    assert t["deps"] == [] and t["harness"] is None and t["state"] == "ready"
    j(abx, "edit", a, "--dep", "t_doesnotexist", rc=1)
    j(abx, "edit", a, "--harness", "nonesuch", rc=1)
    j(abx, "edit", a, "--tier", "nonesuch", rc=1)
    assert j(abx, "note", a, "a note")["noted"]
    assert j(abx, "defer", a, "--until", "+1h")["deferred"]
    assert next(x for x in snap(abx)["tasks"] if x["id"] == a)["state"] == "waiting"
    assert j(abx, "cancel", a, "not needed")["status"] == "cancelled"
    j(abx, "cancel", a, rc=1)  # already cancelled
    assert j(abx, "retry", a)["status"] == "open"
    # a manual decision card is answered with note + done
    d = j(abx, "add", "pick a name", "--manual", "-l", "owner=someone")["id"]
    assert [x["id"] for x in j(abx, "decision", "ls", "--owner", "someone")] == [d]
    assert j(abx, "decision", "ls", "--owner", "other") == []
    assert j(abx, "decision", "answer", d, "use the blue one")["answered"]
    shown = j(abx, "show", d)
    assert shown["status"] == "done" and shown["result"] == "use the blue one" and shown["notes"][-1]["body"] == "use the blue one"
    j(abx, "decision", "answer", d, "again", rc=1)
    j(abx, "decision", "answer", a, "not a decision", rc=1)  # runner-owned card
    # `done` for a manual card by hand
    m = j(abx, "add", "by hand", "--manual")["id"]
    assert j(abx, "done", m, "did it myself", "--force")["status"] == "done"
    # watchers and schedules
    j(abx, "watch", "add", "w1", "--on", "dead", "--brief", "look")
    assert j(abx, "watch", "disable", "w1")[0]["enabled"] == 0
    assert [w["enabled"] for w in snap(abx)["watchers"]] == [False]
    assert j(abx, "watch", "enable", "w1")[0]["enabled"] == 1
    j(abx, "watch", "disable", "nope", rc=1)
    j(abx, "sched", "add", "night", "--window", "00:00-23:59", "--slots", "4")
    assert snap(abx)["active_windows"] == ["night"] and snap(abx)["runner"]["slots_effective"] == 4
    assert j(abx, "sched", "disable", "night")[0]["enabled"] == 0 and snap(abx)["active_windows"] == []
    assert j(abx, "sched", "enable", "night")[0]["enabled"] == 1
    j(abx, "sched", "disable", "nope", rc=1)
    j(abx, "kill", "123", rc=1)  # no such run


FAKE_CLAUDE = """#!/bin/sh
echo started
exec sleep 300
"""


def test_kill_one_run_with_a_live_runner(abx, tmp_path):
    """Runner + a fake `claude` that sleeps: snapshot shows the child, `ab kill` stops only that run, the task is requeued."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home)}  # HOME: no real session logs get harvested
    j(abx, "config", "set", "min_free_gib", "0")
    j(abx, "config", "set", "poll_s", "1")
    j(abx, "config", "set", "harvest_every_s", "99999")
    j(abx, "tiers", "set", "low", "claude:haiku")
    j(abx, "tiers", "set", "medium", "claude:haiku")
    t1 = j(abx, "add", "sleeper one", "--workdir", str(tmp_path / "w1"))["id"]
    t2 = j(abx, "add", "sleeper two", "--workdir", str(tmp_path / "w2"))["id"]
    j(abx, "runner", "pause")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        j(abx, "runner", "slots", "2")
        j(abx, "runner", "resume")
        d = None
        for _ in range(40):
            d = snap(abx)
            if len(d["runner"]["children"]) == 2:
                break
            time.sleep(0.5)
        assert d["runner"]["running"] and len(d["runner"]["children"]) == 2, d["runner"]
        # reload the runner while both runs are live: the new process adopts them (they are no longer its own Popen objects)
        pid0 = d["runner"]["pid"]
        assert j(abx, "runner", "reload")["action"] == "reload"
        for _ in range(80):
            d = snap(abx, "--events-after", "0", "--events-limit", "1000")
            kinds = [e["kind"] for e in d["events"]]
            if "runner_reload" in kinds and kinds.count("runner_start") == 2 and not d["runner"]["stop"]:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"runner did not reload: {kinds}")
        assert d["runner"]["pid"] == pid0 and d["runner"]["running"] and len(d["runner"]["children"]) == 2
        ch = d["runner"]["children"][0]
        assert ch["harness"] == "claude" and ch["pid"] and ch["timeout_s"] and ch["elapsed_s"] >= 0 and ch["bucket"]
        assert sum(b["running"] for b in d["boards"]) == 2
        out = j(abx, "kill", str(ch["run"]))
        assert out["requested"] and out["runner_running"]
        j(abx, "runner", "pause")  # so the killed task is not started again at once
        for _ in range(40):
            d = snap(abx)
            if len(d["runner"]["children"]) == 1:
                break
            time.sleep(0.5)
        assert [c["run"] for c in d["runner"]["children"]] != [ch["run"]] and len(d["runner"]["children"]) == 1
        killed = next(x for x in d["tasks"] if x["id"] == ch["task_id"])
        assert killed["status"] == "open" and killed["attempts"] == 0, killed
        j(abx, "kill", str(ch["run"]), rc=1)  # already ended
        shown = j(abx, "show", ch["task_id"])
        assert [r["outcome"] for r in shown["runs"]] == ["killed"] and shown["attempts"] == 0
        tl = snap(abx, "--tail", f"{d['runner']['children'][0]['run']}:100")["tail"]
        assert tl["out"].startswith("started") and tl["running"]
        # cancel kills the other run and cancels its task
        other = d["runner"]["children"][0]
        j(abx, "cancel", other["task_id"])
        for _ in range(40):
            if not snap(abx)["runner"]["children"]:
                break
            time.sleep(0.5)
        assert snap(abx)["runner"]["children"] == []
    finally:
        j(abx, "runner", "stop", "--now")
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
