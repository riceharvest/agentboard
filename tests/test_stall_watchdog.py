"""Stall watchdog: runs stuck in provider 429 retries, or showing no sign of life, are caught by the runner.
Throwaway AB_HOME; a fake `hermes` that writes real-format agent.log lines (the Hermes logger format: `Retrying API call in
Ns (attempt k/10) ... error=...429`, `API call #N:` for a successful model call) into its per-run HERMES_HOME and sleeps;
a fake `claude` as the next candidate. Thresholds are seconds here (defaults: 10m retry, 45m idle)."""
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest

from conftest import ROOT, clean_env
from agentboard import harness as H

# Same shape as a real Hermes agent.log; {ts} is rewritten to "now" by the fake so the streak is as old as the run, not the fixture.
RETRY = ("{ts} WARNING [local-aaaa] agent.conversation_loop: Retrying API call in {d:.1f}s (attempt {n}/10) thread=t1 "
         "provider=nous base_url=http://127.0.0.1:1/v1 model=example/free policy=default error=HTTP 429: Rate limit exceeded")
CAPACITY = "{ts} INFO [local-aaaa] agent.turn_recovery: Nous 429 looks like upstream capacity (no exhausted bucket in headers) -- not tripping cross-session breaker."
OK = "{ts} INFO [local-aaaa] agent.conversation_loop: API call #{n}: model=example/free provider=nous in=100 out=5 total=105 latency=1.0s"

FAKE_HERMES = r'''#!/usr/bin/env python3
import os, sys, time
from datetime import datetime
argv = sys.argv[1:]
if argv[:2] == ["gateway", "stop"]:
    sys.exit(0)
hh = os.environ["HERMES_HOME"]
os.makedirs(os.path.join(hh, "logs"), exist_ok=True)
mode = os.environ.get("FAKE_MODE", "retry")
log = os.path.join(hh, "logs", "agent.log")
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
def w(line):
    with open(log, "a") as f:
        f.write(line.replace("{ts}", ts()) + "\n")
if mode == "retry":
    w(@OK@.format(ts="{ts}", n=1))
    n = 0
    while True:
        n += 1
        w(@RETRY@.format(ts="{ts}", n=n % 10 + 1, d=1.0))
        w(@CAPACITY@.format(ts="{ts}"))
        # a tool result that merely QUOTES the 429 lines must not count (no logger prefix) and goes to stdout too
        print("tool output: HTTP 429 Retrying API call in 3s (attempt 1/10) rate limit", flush=True)
        time.sleep(0.5)
elif mode == "recover":
    w(@RETRY@.format(ts="{ts}", n=1, d=1.0))
    time.sleep(2)
    w(@OK@.format(ts="{ts}", n=2))
    time.sleep(4)
    print("recovered fine")
    print("session_id: sess_ok")
elif mode == "idle":
    w("%s INFO gateway.run: housekeeping" % ts())   # log growth that is not a model call
    time.sleep(120)
elif mode == "quote":
    # the model's own text mentions 429 / rate limits; only the harness' lines count
    w(@OK@.format(ts="{ts}", n=1))
    w("  Retrying API call in 3s (attempt 1/10) error=HTTP 429 (an echoed tool result, no logger prefix)")
    time.sleep(120)
'''.replace("@OK@", repr(OK)).replace("@RETRY@", repr(RETRY)).replace("@CAPACITY@", repr(CAPACITY))

FAKE_CLAUDE = r'''#!/bin/sh
echo "claude ran" >> "$FAKE_LOG"
ab done "$AB_TASK" "finished on claude"
'''


def _lines(kinds):
    """(kind, seconds ago) -> agent.log text in the real format, to unit-test the probe."""
    out = []
    for kind, ago in kinds:
        ts = datetime.fromtimestamp(time.time() - ago).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        out.append({"ok": OK, "rl": RETRY, "cap": CAPACITY}[kind].format(ts=ts, n=1, d=2.0))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- unit: probes
def test_hermes_probe_counts_429_streak_since_last_model_call(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(_lines([("ok", 900), ("rl", 700), ("cap", 700), ("rl", 400), ("rl", 100)]))
    p = H.probe_hermes_log(log)
    assert p["retry_count"] == 4 and abs(time.time() - p["retry_since"] - 700) < 5
    assert abs(time.time() - p["last_progress_at"] - 900) < 5
    log.write_text(_lines([("ok", 900), ("rl", 700), ("rl", 400), ("ok", 100)]))  # a successful call ends the streak
    p = H.probe_hermes_log(log)
    assert p["retry_count"] == 0 and p["retry_since"] is None and abs(time.time() - p["last_progress_at"] - 100) < 5
    assert H.probe_hermes_log(tmp_path / "nope.log")["last_progress_at"] is None  # no log yet: a probe with no progress, not None


def test_hermes_probe_ignores_quoted_lines_and_non_429_retries(tmp_path):
    log = tmp_path / "agent.log"
    quoted = ("  Retrying API call in 3s (attempt 1/10) error=HTTP 429 (echoed by a tool)\n"
              "tool says: 2026-11-19 19:35:12,000 WARNING agent.conversation_loop-ish: nothing\n"
              "2026-11-19 19:35:12,000 INFO tools.shell: Retrying API call in 3s HTTP 429\n")  # wrong logger: not the agent's API loop
    conn_err = ("2026-11-19 19:35:12,000 WARNING [local-aaaa] agent.conversation_loop: Retrying API call in 2.3s (attempt 1/10) "
                "provider=custom error=Connection error.\n")
    log.write_text(quoted + conn_err)
    p = H.probe_hermes_log(log)
    assert p["retry_count"] == 0, p


def test_claude_probe_reads_transcript_api_errors(tmp_path, monkeypatch):
    cfgdir, work = tmp_path / "cc", tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    d = H._claude_projects_dir(str(work))
    d.mkdir(parents=True)
    t0 = time.time()

    def rec(kind, ago, **kw):
        ts = datetime.fromtimestamp(t0 - ago, UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return json.dumps({"type": "system" if kind == "err" else kind, "timestamp": ts,
                           **({"subtype": "api_error", "error": {"status": kw.get("status", 429), "message": "rate limited" if kw.get("status", 429) == 429 else "overloaded"}} if kind == "err" else {})})
    (d / "s.jsonl").write_text("\n".join([rec("assistant", 800), rec("err", 600), rec("err", 300, status=503), rec("err", 100)]) + "\n")
    run = type("R", (), {"started": t0 - 1000, "workdir": str(work)})
    p = H.probe_claude_transcript(run)
    assert p["retry_count"] == 2 and abs(t0 - p["retry_since"] - 600) < 5 and abs(t0 - p["last_progress_at"] - 800) < 5
    assert H.probe_claude_transcript(type("R", (), {"started": t0 + 100, "workdir": str(work)})) is None  # stale transcript
    # a worker blocked on a subagent writes under the session dir, not the main file: that is life, not idle
    os.utime(d / "s.jsonl", (t0 - 900, t0 - 900))
    assert abs(H.probe_claude_transcript(run)["last_progress_at"] - (t0 - 800)) < 5  # only the old assistant record (tail is an api_error)
    sub = d / "s" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text("{}\n")
    assert abs(H.probe_claude_transcript(run)["last_progress_at"] - time.time()) < 5
    cache = {}
    R2 = type("R", (), {"started": t0 - 1000, "workdir": str(work), "cache": cache})
    H.probe_claude_transcript(R2)
    assert cache["claude_path"].endswith("s.jsonl")
    assert H.ADAPTERS["claude"].idle_signal == "probe" and H.ADAPTERS["codex"].idle_signal == "out"
    assert H.ADAPTERS["agy"].idle_signal is None  # prints once at exit: never called idle


# ---------------------------------------------------------------- integration: the runner kills and reroutes
@pytest.fixture
def rig(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for n, body in (("hermes", FAKE_HERMES), ("claude", FAKE_CLAUDE)):
        (fake / n).write_text(body)
        (fake / n).chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    (home / ".hermes").mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
           "FAKE_LOG": str(tmp_path / "fake.log"), "FAKE_MODE": "retry"}
    procs = []

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "true"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"),
                 ("stall_retry_after", "6s"), ("stall_idle_after", "6s"), ("stall_cooldown", "30m"), ("backoff", "1s")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "hermes/nous:example/free", "claude:haiku")
    j("runner", "pause")

    def start(slots="1"):
        logf = open(tmp_path / "runner.log", "ab")
        procs.append(subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                                      stdout=logf, stderr=logf, start_new_session=True))
        j("runner", "slots", slots)
        j("runner", "resume")
    j.start, j.env, j.tmp = start, env, tmp_path
    yield j
    try:
        j("runner", "stop", "--now")
    except AssertionError:
        pass
    for p in procs:
        p.terminate()


def wait(cond, secs=40):
    for _ in range(secs * 2):
        if cond():
            return True
        time.sleep(0.5)
    return False


def rig_home(j):
    return j.tmp / "ab"


def runs_of(j, tid):
    return j("show", tid)["runs"]


def test_retry_stall_is_a_limit_not_an_attempt_and_reroutes(rig, abx):
    j = rig
    t = j("add", "rate limited card", "--body", "work", "--workdir", str(j.tmp / "w"))["id"]
    j.start()
    # the run shows up as retrying 429 in status / snapshot before it is killed
    def shown():
        code, txt, _ = abx("runner", "status", env=j.env)
        return "retrying 429" in txt
    assert wait(shown, 20), open(j.tmp / "runner.log").read()
    snap = j("snapshot")
    assert any((c.get("stall") or "").startswith("retrying 429") for c in snap["runner"]["children"]), snap["runner"]["children"]
    # then the watchdog kills it as a LIMIT on the bucket and the card moves on to the next candidate
    assert wait(lambda: runs_of(j, t) and runs_of(j, t)[0]["outcome"] == "limit"), (runs_of(j, t), open(j.tmp / "runner.log").read())
    assert wait(lambda: j("show", t)["status"] == "done"), (j("show", t), open(j.tmp / "runner.log").read())
    sh = j("show", t)
    assert sh["attempts"] == 0, sh                                  # no attempt burnt
    assert [r["harness"] for r in sh["runs"]] == ["hermes", "claude"]
    assert sh["result"] == "finished on claude"
    lim = {x["scope"]: x for x in j("limits")}
    assert "bucket:hermes-nous" in lim and lim["bucket:hermes-nous"]["kind"] == "limit", lim
    assert lim["bucket:hermes-nous"]["until"] - time.time() > 25 * 60   # stall_cooldown 30m
    assert "429" in (lim["bucket:hermes-nous"]["reason"] or "")
    assert "stalled in provider rate-limit retries" in sh["runs"][0]["detail"], sh["runs"][0]
    import sqlite3  # the bucket's AIMD budget was halved (halved_at is only set by pacer_on_limit)
    db = next((rig_home(j)).glob("*.db"))
    row = sqlite3.connect(str(db)).execute("SELECT value FROM control WHERE value LIKE '%halved_at%'").fetchall()
    assert any("hermes-nous" in r[0] and "halved_at" in r[0] for r in row), row


def test_idle_stall_retries_once_then_holds_for_overseer(rig):
    j = rig
    j.env["FAKE_MODE"] = "idle"
    j("tiers", "set", "medium", "hermes:example/free")
    t = j("add", "silent card", "--body", "work", "--workdir", str(j.tmp / "w"))["id"]
    j.start()
    assert wait(lambda: runs_of(j, t) and runs_of(j, t)[0]["outcome"] == "idle"), (runs_of(j, t), open(j.tmp / "runner.log").read())
    # first idle kill: retried, no attempt counted
    assert wait(lambda: len(runs_of(j, t)) >= 2 and runs_of(j, t)[1]["outcome"] == "idle", 40), runs_of(j, t)
    sh = j("show", t)
    assert sh["attempts"] == 0 and sh["labels"].get("stalled") == "overseer" and not sh["runnable"], sh
    time.sleep(8)
    assert len(runs_of(j, t)) == 2   # held: no third run
    assert any(p["kind"] == "stalled" for p in j("anomalies")), j("anomalies")
    j("edit", t, "--auto", "-l", "stalled=")
    assert wait(lambda: len(runs_of(j, t)) == 3, 20)


def test_stall_off_label_is_never_killed_and_label_duration_overrides(rig, abx):
    j = rig
    j.env["FAKE_MODE"] = "retry"
    t = j("add", "waits on a long job", "--body", "work", "--workdir", str(j.tmp / "w"), "-l", "stall=off")["id"]
    j.start()
    time.sleep(18)  # 3x stall_retry_after of 6s
    sh = j("show", t)
    assert sh["status"] == "claimed" and len(sh["runs"]) == 1 and sh["runs"][0]["outcome"] is None, sh
    code, txt, _ = abx("runner", "status", env=j.env)
    assert "retrying 429" in txt  # still visible, just never killed
    j("runner", "stop", "--now")


def test_recovering_run_is_left_alone(rig):
    j = rig
    j.env["FAKE_MODE"] = "recover"
    t = j("add", "blip", "--body", "work", "--workdir", str(j.tmp / "w"))["id"]
    j("config", "set", "stall_retry_after", "30s")
    j.start()
    assert wait(lambda: j("show", t)["status"] == "done", 30), (j("show", t), open(j.tmp / "runner.log").read())
    sh = j("show", t)
    assert [r["outcome"] for r in sh["runs"]] == ["done_auto"], sh["runs"]
    assert not j("limits")


def test_quoted_429_lines_do_not_trip_the_watchdog(rig):
    j = rig
    j.env["FAKE_MODE"] = "quote"
    t = j("add", "about rate limits", "--body", "work", "--workdir", str(j.tmp / "w"), "-l", "stall=1h")["id"]
    j("config", "set", "stall_idle_after", "1h")
    j.start()
    time.sleep(14)  # > 2x stall_retry_after
    sh = j("show", t)
    assert sh["status"] == "claimed" and sh["runs"][0]["outcome"] is None and not j("limits"), sh


def test_run_home_is_profile_shaped_and_agent_log_is_kept(rig, abx):
    j = rig
    assert H.hermes_run_home(7, j.tmp / "root") == j.tmp / "root" / "profiles" / "ab-run-7"
    t = j("add", "finishes fine", "--body", "work", "--workdir", str(j.tmp / "w"))["id"]
    j.env["FAKE_MODE"] = "recover"
    j.start()
    kept = j.tmp / "ab" / "runs"
    assert wait(lambda: any(kept.glob("*.hermes-agent.log")), 40), "agent.log was not kept"
    log = next(kept.glob("*.hermes-agent.log"))
    assert "API call #2" in log.read_text()
    run_id = log.name.split(".")[0]
    assert not (j.tmp / "fakehome" / ".hermes" / "profiles" / f"ab-run-{run_id}").exists()   # home removed after finalize
    code, out, _ = abx("show", t, env=j.env)
    assert f"kept {log} ({log.stat().st_size} bytes)" in out


def test_keep_agent_log_is_size_capped(tmp_path):
    home = tmp_path / "h"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "agent.log").write_bytes(b"x" * 100 + b"TAIL")
    assert H.keep_hermes_agent_log(home, tmp_path / "o.log", cap=10) == 10
    assert (tmp_path / "o.log").read_bytes().endswith(b"TAIL")
    assert H.keep_hermes_agent_log(tmp_path / "none", tmp_path / "p.log") is None
