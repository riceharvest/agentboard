"""Build a small, generic board in a throwaway AB_HOME and write what `ab snapshot --json` returns for it.

    python3 tui/tests/make_fixture.py OUT.json            # fixture used by the Rust render tests and the README GIF
    python3 tui/tests/make_fixture.py --keep-home DIR      # also leave the board in DIR (for screenshots / manual runs)

Never touches the real board: AB_HOME is always a temp dir (or the one given with --keep-home).
The JSON is scrubbed of local paths and host names so it can live in the repo."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def build(home, run_pids=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    env.update(AB_HOME=str(home), PYTHONPATH=str(ROOT), AB_ACTOR="tui-test", AB_NO_COLOR="1")
    os.environ.update(AB_HOME=str(home))

    def ab(*args, actor=None, ok=True):
        e = dict(env)
        if actor:
            e["AB_ACTOR"] = actor
        p = subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=ROOT, capture_output=True, text=True, env=e)
        if ok and p.returncode not in (0,):
            raise SystemExit(f"ab {' '.join(args)} failed: {p.stderr}{p.stdout}")
        return p.stdout.strip()

    def add(title, body="", *labels, **kw):
        a = ["add", title, "--body", body]
        for l in labels:
            a += ["--label", l]
        for k, v in kw.items():
            if v is True:
                a.append("--" + k.replace("_", "-"))
            elif isinstance(v, list):
                for x in v:
                    a += ["--" + k.replace("_", "-"), x]
            elif v is not None:
                a += ["--" + k.replace("_", "-"), str(v)]
        return ab(*a).split()[0]

    ab("boards", "create", "web")
    ab("boards", "set", "web", "--max-slots", "1", "--pause")
    t = {}
    t["dive"] = add("Survey the retry strategies of other task boards", "Read three public boards and compare.", "type=dive", tier="high", priority=3)
    t["exp"] = add("Does a shorter lease cut stuck runs?", "Run the soak twice with leases of 60 s and 600 s.", "type=experiment", tier="medium", priority=2)
    t["build"] = add("Add a health endpoint to the runner", "Expose `ab runner status --json` over a unix socket.", "type=build", tier="high", priority=4, dep=[t["dive"]])
    t["rel"] = add("Cut release 0.4", "Tag, build wheels, update the changelog.", "type=release", tier="low", priority=1, dep=[t["build"], t["exp"]])
    t["watch"] = add("Watch the nightly soak", "Wake when the soak finishes.", "type=watch", tier="low")
    t["rev"] = add("Review the lease patch", "Check the diff for races.", "type=review", tier="medium", priority=2, manual=True)
    t["dec1"] = add("Decide: default slot count", "Options:\n- A: 2 slots (safe)\n- B: 4 slots (faster)\n\nDefault: A if nobody answers by Friday.", "type=decision", "owner=lead", tier="low", priority=5, manual=True)
    t["dec2"] = add("Decide: drop python 3.9 support?", "Options:\n- yes\n- no\n\nDefault: no.", "type=decision", "owner=lead", tier="low", priority=4, manual=True)
    t["run1"] = add("Port the usage harvester to the new log format", "Parse the new JSONL layout.", "type=build", tier="high", priority=3, timeout="45m", workdir="/work/harvester")
    t["run2"] = add("Summarise last week's failed runs", "Group by cause.", "type=dive", tier="medium", priority=2, timeout="20m")
    t["done1"] = add("Write the snapshot schema", "JSON schema for UIs.", "type=build", tier="high")
    t["done2"] = add("Compare two lease lengths", "Soak A vs B.", "type=experiment", tier="medium")
    t["dead1"] = add("Flaky integration test on arm64", "Fails one in five.", "type=build", tier="medium", max_attempts=1)
    t["web1"] = add("Landing page copy", "Draft the hero text.", "type=build", tier="low", board="web")
    # one task that waits for a time
    add("Nightly report", "", "type=watch", tier="low", after="+6h")
    for key in ("done1", "done2"):
        ab("claim", t[key], actor="worker-1")
        ab("done", t[key], "finished: all checks passed, 12 tests", actor="worker-1")
    ab("claim", t["dead1"], actor="worker-2")
    ab("fail", t["dead1"], "segfault in the arm64 build, no repro", actor="worker-2")
    ab("note", t["dive"], "Found two boards worth comparing; notes in the thread.", actor="worker-1")
    ab("note", t["dec1"], "Context: three agents are idle while two slots are busy.", actor="worker-1")
    for key in ("run1", "run2"):
        ab("claim", t[key], actor="worker-%s" % key[-1])
    ab("watch", "add", "on-dead", "--on", "dead", "--brief", "Look at the dead card and say why it died.", "--all-boards")
    ab("sched", "add", "always", "--window", "00:00-23:59", "--slots", "3")
    ab("config", "set", "caps", json.dumps({"claude-sub": {"window": "5h", "max_output": 400000, "max_5h_pct": 80, "pace_7d_lead_h": 12}}))
    ab("limits", "set", "bucket:opencode-free", "--until", "+25m", "--reason", "rate limited")

    from agentboard import store
    c = store.connect()
    now = time.time()
    with store.tx(c):
        reset5 = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 1.5 * 3600))
        reset7 = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 3.2 * 86400))
        c.execute("INSERT INTO control(key,value) VALUES('_plan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps({"ts": now - 40, "data": {"five_hour": 38.0, "seven_day": 64.0, "five_hour_resets": reset5, "seven_day_resets": reset7}}),))
        buckets = [("claude-sub", "claude", "sonnet-5", 1.0), ("local", "pi", "local-27b", 0.6), ("opencode-free", "opencode", "free-model", 0.3)]
        n = 0
        for h in range(168):
            for b, harness, model, w in buckets:
                # a daily rhythm plus a bump: gives the sparklines some shape
                load = (1 + ((h % 24) in range(8, 20)) * 3) * w * (1 + (h % 7) / 7)
                if h < 20 and b == "opencode-free":
                    load *= 4
                for k in range(int(load)):
                    n += 1
                    c.execute("INSERT OR IGNORE INTO usage(source,key,ts,harness,bucket,model,input,output) VALUES('fx',?,?,?,?,?,?,?)",
                              (str(n), now - (167 - h) * 3600 - k * 97 - 30, harness, b, model, 2000 + k * 13, 800 + (k * 37) % 900))
        # runner row and two live runs with output files
        runs = Path(home) / "runs"
        runs.mkdir(exist_ok=True)
        pid = (run_pids or [os.getpid()])[0]
        c.execute("INSERT OR REPLACE INTO runner(id,pid,host,started_at,heartbeat,state,active,slots,note) VALUES(1,?,?,?,?,?,?,?,?)",
                  (pid, "devbox", now - 5400, now, "running", 2, 3, "tick"))
        outs = {
            "run1": ["Reading harvester/claude.py", "Found the new JSONL layout (v2): usage moved under message.usage", "editing harvester/claude.py",
                     "running tests: 11 passed, 1 failed", "fixing test_cache_read_tokens", "tests: 12 passed", "committing"],
            "run2": ["listing failed runs of the last 7 days", "37 runs failed: 21 timeouts, 9 rate limits, 7 crashes", "grouping by cause", "writing summary.md"],
        }
        for i, (key, harness, model, bucket, started) in enumerate([("run1", "claude", "sonnet-5", "claude-sub", now - 1260), ("run2", "pi", "local-27b", "local", now - 340)], 1):
            out = runs / f"{i}.out"
            out.write_text("".join(f"\x1b[32m>\x1b[0m {l}\n" for l in outs[key]))
            (runs / f"{i}.err").write_text("")
            cur = c.execute("INSERT INTO runs(id,task_id,harness,model,effort,tier,bucket,pid,started_at,outcome,out_path,err_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (i, t[key], harness, model, "high", "high", bucket, pid, started, None, str(out), str(runs / f"{i}.err")))
            c.execute("UPDATE tasks SET run_id=? WHERE id=?", (i, t[key]))
        # an earlier, finished run on the done task, for the detail screen's run history
        c.execute("INSERT INTO runs(id,task_id,harness,model,bucket,pid,started_at,ended_at,outcome,exit_code,input,output,cost_usd) VALUES(10,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (t["done1"], "claude", "sonnet-5", "claude-sub", 1, now - 7200, now - 6300, "done", 0, 41000, 8800, 0.0))
    return t


def snapshot(home, run=1):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    env.update(AB_HOME=str(home), PYTHONPATH=str(ROOT), AB_ACTOR="tui-test")
    out = subprocess.run([sys.executable, "-m", "agentboard", "snapshot", "--json", "--closed", "20", "--events-after", "0",
                          "--events-limit", "200", "--tail", f"{run}:2000"], cwd=ROOT, capture_output=True, text=True, env=env, check=True).stdout
    return json.loads(out)


def scrub(s, home):
    """No local paths, and task ids reshaped (t_ + 4 hex + zz + 2 hex) so they stay distinct but never look like real board ids."""
    import re
    txt = json.dumps(s).replace(str(home), "/home/user/ab-home")
    txt = re.sub(r"t_[0-9a-f]{8}", lambda m: "t_" + m.group(0)[2:6] + "zz" + m.group(0)[8:10], txt)
    return json.loads(txt)


def main():
    args = sys.argv[1:]
    keep = None
    if "--keep-home" in args:
        i = args.index("--keep-home")
        keep = Path(args[i + 1])
        del args[i:i + 2]
    out = Path(args[0]) if args else None
    home = keep or Path(tempfile.mkdtemp(prefix="ab-fixture-"))
    home.mkdir(parents=True, exist_ok=True)
    build(home)
    snap = scrub(snapshot(home), home)
    snap["runner"]["host"] = "devbox"
    if out:
        out.write_text(json.dumps(snap, indent=1) + "\n")
        print(f"wrote {out}: {len(snap['tasks'])} tasks, {len(snap['events'])} events")
    if keep:
        print(f"board left in {keep} (AB_HOME={keep}); the runner row points at pid {os.getpid()} which exits now, so it shows as stopped")


if __name__ == "__main__":
    main()
