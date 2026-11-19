"""Pace-based launching: per-bucket launch budget per interval (provider pace / AIMD / cap),
smooth starts (launch_per_tick), limit halving, and max_runs hard ceiling. Throwaway AB_HOME."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from conftest import ROOT, clean_env
from agentboard import snapshot, store, usage


def _iso_offset(sec):
    return datetime.fromtimestamp(time.time() + sec, tz=timezone.utc).isoformat()


def test_claude_plan_pace_raises_and_lowers_budget(tmp_path, monkeypatch):
    """Faked Claude plan reading (monkeypatch usage.claude_plan_pct) below/above the pace line
    raises/lowers the claude-sub launch budget, and recent cost per run in `usage` scales it."""
    db_path = tmp_path / "test.sqlite"
    conn = store.connect(db_path)
    try:
        # Halfway through both 5h and 7d windows -> pace line is 50%.
        r5 = _iso_offset(2.5 * 3600)
        r7 = _iso_offset(3.5 * 86400)

        # 1) Well below the 50% pace line -> budget raises
        monkeypatch.setattr(usage, "claude_plan_pct", lambda: {
            "five_hour": 15.0, "seven_day": 20.0, "five_hour_resets": r5, "seven_day_resets": r7,
        })
        usage.refresh_plan(conn)
        below = usage.launch_budget(conn, "claude-sub")
        assert below["why"] == "pace"

        # 2) Right on the 50% pace line -> baseline steady budget
        monkeypatch.setattr(usage, "claude_plan_pct", lambda: {
            "five_hour": 50.0, "seven_day": 50.0, "five_hour_resets": r5, "seven_day_resets": r7,
        })
        usage.refresh_plan(conn)
        on_pace = usage.launch_budget(conn, "claude-sub")
        assert on_pace["why"] == "pace"

        # 3) Above the 50% pace line -> budget lowers
        monkeypatch.setattr(usage, "claude_plan_pct", lambda: {
            "five_hour": 75.0, "seven_day": 82.0, "five_hour_resets": r5, "seven_day_resets": r7,
        })
        usage.refresh_plan(conn)
        above = usage.launch_budget(conn, "claude-sub")
        assert above["why"] == "pace"
        assert below["budget"] > on_pace["budget"] > above["budget"] >= 1, (below, on_pace, above)

        # Snapshot (read-only) and buckets_report (`ab usage`) expose the same launch_budget and budget_why
        rep = {r["bucket"]: r for r in usage.buckets_report(conn)}
        assert rep["claude-sub"]["launch_budget"] == above["budget"]
        assert rep["claude-sub"]["budget_why"] == "pace"
        snap = snapshot.build(conn)
        s_row = next(u for u in snap["usage"] if u["bucket"] == "claude-sub")
        assert s_row["launch_budget"] == above["budget"] and s_row["budget_why"] == "pace"

        # 4) Cost per run from the `usage` table: cheaper runs raise the budget, expensive runs lower it
        monkeypatch.setattr(usage, "claude_plan_pct", lambda: {
            "five_hour": 25.0, "seven_day": 25.0, "five_hour_resets": r5, "seven_day_resets": r7,
        })
        now = time.time()
        with store.tx(conn):
            for i in range(5):
                conn.execute(
                    "INSERT OR REPLACE INTO usage(source,key,ts,harness,bucket,model,task_id,input,output) "
                    "VALUES('run',?,?, 'claude','claude-sub','sonnet',?, 1000, 5000)",
                    (f"cheap_{i}", now - 60 * i, f"task_c_{i}"),
                )
        cheap_budget = usage.launch_budget(conn, "claude-sub")["budget"]
        with store.tx(conn):
            conn.execute("DELETE FROM usage WHERE bucket='claude-sub'")
            for i in range(5):
                conn.execute(
                    "INSERT OR REPLACE INTO usage(source,key,ts,harness,bucket,model,task_id,input,output) "
                    "VALUES('run',?,?, 'claude','claude-sub','sonnet',?, 50000, 100000)",
                    (f"exp_{i}", now - 60 * i, f"task_e_{i}"),
                )
        expensive_budget = usage.launch_budget(conn, "claude-sub")["budget"]
        assert cheap_budget > expensive_budget, (cheap_budget, expensive_budget)

        # 5) With a hard pace_7d_lead_h guard, being above the weekly pace line drops budget to 0
        with store.tx(conn):
            store.cfg_set(conn, "caps", {"claude-sub": {"window": "5h", "pace_7d_lead_h": 1.0}}, "test")
        monkeypatch.setattr(usage, "claude_plan_pct", lambda: {
            "five_hour": 30.0, "seven_day": 80.0, "five_hour_resets": r5, "seven_day_resets": r7,
        })
        guarded = usage.launch_budget(conn, "claude-sub")
        assert guarded["budget"] == 0 and guarded["why"] == "pace"
    finally:
        conn.close()


def test_aimd_ramp_limit_halving_and_max_runs_ceiling(tmp_path):
    """AIMD starts at 1 launch per interval, adds 1 per clean interval with activity,
    halves on a limit error, and respects max_runs as a hard ceiling."""
    db_path = tmp_path / "aimd.sqlite"
    conn = store.connect(db_path)
    try:
        with store.tx(conn):
            store.cfg_set(conn, "pace_interval", "10s", "test")
        t0 = 1_800_000_000.0
        b = "openai-codex"

        # Starts at 1 launch per interval (AIMD)
        info = usage.launch_budget(conn, b, now=t0)
        assert info["budget"] == 1 and info["why"] == "aimd" and info["remaining"] == 1

        # Simulate 3 clean intervals with launches: budget ramps 1 -> 2 -> 3 -> 4
        cur = t0
        for expected in (1, 2, 3):
            assert usage.launch_budget(conn, b, now=cur)["budget"] == expected
            with store.tx(conn):
                for k in range(expected):
                    usage.pacer_on_spawn(conn, b, now=cur + k * 0.1)
                    conn.execute("INSERT INTO runs(task_id,harness,bucket,started_at,ended_at,outcome) VALUES('x','codex',?,?,?,'done')",
                                 (b, cur + k * 0.1, cur + k * 0.1 + 0.5))
            # Now at budget for this interval
            assert usage.pacer_status(conn, b, now=cur + 1.0) is not None
            cur += 10.5
            usage.pacer_tick(conn, now=cur)
        assert usage.launch_budget(conn, b, now=cur)["budget"] == 4

        # An idle interval (no runs) does not increase the budget
        cur += 11.0
        usage.pacer_tick(conn, now=cur)
        assert usage.launch_budget(conn, b, now=cur)["budget"] == 4

        # Limit error halves the bucket's budget: 4 -> 2, and another halves 2 -> 1
        with store.tx(conn):
            new_b = usage.pacer_on_limit(conn, b, now=cur)
        assert new_b == 2 and usage.launch_budget(conn, b, now=cur)["budget"] == 2
        with store.tx(conn):
            new_b2 = usage.pacer_on_limit(conn, b, now=cur + 1.0)
        assert new_b2 == 1 and usage.launch_budget(conn, b, now=cur + 1.0)["budget"] == 1

        # max_runs cap acts as a hard ceiling and reports why='cap'
        with store.tx(conn):
            pstate = usage._load_pacer(conn)
            pstate[b]["budget"] = 6
            usage._save_pacer(conn, pstate)
            store.cfg_set(conn, "caps", {b: {"window": "1h", "max_runs": 3}}, "test")
        capped = usage.launch_budget(conn, b, now=cur + 2.0)
        assert capped["budget"] == 3 and capped["why"] == "cap"
        assert usage.cap_status(conn, b) is not None  # 3 runs already started in the last 1h
    finally:
        conn.close()


FAKE_CODEX = """#!/usr/bin/env python3
import os, subprocess, sys, time
mode_file = os.environ.get("FAKE_CODEX_MODE", "")
mode = open(mode_file).read().strip() if mode_file and os.path.exists(mode_file) else "ok"
open(os.environ["FAKE_START_LOG"], "a").write(f"codex\\t{os.environ['AB_RUN']}\\t{os.environ['AB_TASK']}\\t{time.time():.3f}\\t{mode}\\n")
if mode == "limit":
    sys.stderr.write("Error: 429 rate limit reached, try again in 30m\\n")
    sys.exit(1)
subprocess.run(["ab", "done", os.environ["AB_TASK"], "codex ok"], check=True)
print('{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":50}}')
print('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}')
"""

FAKE_AGY = """#!/usr/bin/env python3
import os, subprocess, time
open(os.environ["FAKE_START_LOG"], "a").write(f"agy\\t{os.environ['AB_RUN']}\\t{os.environ['AB_TASK']}\\t{time.time():.3f}\\tok\\n")
subprocess.run(["ab", "done", os.environ["AB_TASK"], "agy ok"], check=True)
print('{"status":"SUCCESS","response":"done","usage":{"input_tokens":80,"output_tokens":40}}')
"""


def test_30_ready_cards_spread_and_limit_error_halves_budget(abx, tmp_path):
    """30 ready cards on one bucket -> starts spread per the AIMD budget, never more than launch_per_tick (2)
    per tick; then a fake limit error halves that bucket's budget while another bucket keeps launching."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, script in (("codex", FAKE_CODEX), ("agy", FAKE_AGY)):
        p = fake / name
        p.write_text(script)
        p.chmod(0o755)
    start_log = tmp_path / "starts.log"
    mode_file = tmp_path / "codex.mode"
    mode_file.write_text("ok")
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env = {
        "PATH": f"{fake}:{os.environ['PATH']}",
        "HOME": str(fake_home),
        "FAKE_START_LOG": str(start_log),
        "FAKE_CODEX_MODE": str(mode_file),
    }

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    for k, v in (
        ("min_free_gib", "0"),
        ("poll_s", "0.2"),
        ("pace_interval", "1.2s"),
        ("launch_per_tick", "2"),
        ("share_workdir", "true"),
        ("harvest_every_s", "99999"),
    ):
        j("config", "set", k, v)
    j("runner", "pause")

    wd = str(tmp_path / "work")
    tids = [j("add", f"card {i}", "--harness", "codex", "--workdir", wd)["id"] for i in range(30)]

    proc = subprocess.Popen(
        [sys.executable, "-m", "agentboard", "runner", "run"],
        cwd=ROOT,
        env=clean_env(abx.home, **env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        j("runner", "slots", "20")
        j("runner", "resume")
        deadline = time.time() + 45
        while time.time() < deadline:
            snap = j("snapshot")
            done_n = sum(1 for t in snap["tasks"] if t["status"] == "done")
            if done_n == 30:
                break
            time.sleep(0.3)
        assert sum(1 for t in j("snapshot")["tasks"] if t["status"] == "done") == 30

        # Check start timestamps: 30 cards must be spread across intervals (AIMD: 1, 2, 3, 4, 5, 6, 7, 2 -> >= 6s total span)
        lines = [ln.split("\t") for ln in start_log.read_text().splitlines() if ln.strip()]
        assert len(lines) == 30
        ts = [float(x[3]) for x in lines]
        total_span = ts[-1] - ts[0]
        assert total_span >= 5.0, f"expected starts spread across intervals, got span {total_span:.2f}s: {ts}"
        # First interval launched 1 run, so ts[1] - ts[0] >= ~1.0s (one pace_interval)
        assert ts[1] - ts[0] >= 0.8, f"first interval should only launch 1 card: ts[0]={ts[0]}, ts[1]={ts[1]}"
        # No more than launch_per_tick (2) launches within any single tick window (< 0.12s apart)
        for i in range(len(ts) - 2):
            assert ts[i + 2] - ts[i] >= 0.12, f"more than launch_per_tick (2) started in one tick: {ts[i:i+3]}"

        # Verify `openai-codex` AIMD budget ramped up to >= 4
        u_before = {r["bucket"]: r for r in j("usage", "--no-harvest")}["openai-codex"]
        budget_before = u_before["launch_budget"]
        assert budget_before >= 4 and u_before["budget_why"] == "aimd", u_before

        # Now switch codex to emit a 429 limit error, and add cards for both codex and agy
        mode_file.write_text("limit")
        t_codex = j("add", "hits limit", "--harness", "codex", "--workdir", wd)["id"]
        t_agy1 = j("add", "other bucket 1", "--harness", "agy", "--model", "Gemini 3.8 Flash (Medium)", "--workdir", wd)["id"]
        t_agy2 = j("add", "other bucket 2", "--harness", "agy", "--model", "Gemini 3.8 Flash (Medium)", "--workdir", wd)["id"]

        deadline = time.time() + 20
        while time.time() < deadline:
            if (j("show", t_agy1)["status"] == "done"
                    and j("show", t_agy2)["status"] == "done"
                    and any(r["outcome"] == "limit" for r in j("show", t_codex)["runs"])):
                break
            time.sleep(0.3)

        assert j("show", t_agy1)["status"] == "done"
        assert j("show", t_agy2)["status"] == "done"
        assert any(r["outcome"] == "limit" for r in j("show", t_codex)["runs"])

        # The limit error on openai-codex halved its launch budget
        u_after = {r["bucket"]: r for r in j("usage", "--no-harvest")}["openai-codex"]
        assert u_after["launch_budget"] == budget_before // 2, (budget_before, u_after)
        snap_after = {r["bucket"]: r for r in j("snapshot")["usage"]}["openai-codex"]
        assert snap_after["launch_budget"] == budget_before // 2 and snap_after["budget_why"] == "aimd"
    finally:
        j("runner", "stop", "--now")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
