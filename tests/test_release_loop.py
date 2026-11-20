"""Release-loop guard: a fake `claude` that only runs `ab release`. After 3 quick released runs the card gets a not_before
backoff and a note, and no 4th launch happens."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env

FAKE = r'''#!/bin/sh
echo run >> "$FAKE_LOG.n"
tid=$(echo "$2" | sed -n "s/.*agentboard task \(t_[0-9a-f]*\).*/\1/p" | head -1)
ab release "$tid" "nothing to do yet" >> "$FAKE_LOG.out" 2>&1
exit 0
'''


def test_release_loop_backoff(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_LOG": str(tmp_path / "fake.log")}

    def j(*args):
        code, out, err = abx("--json", *args, env=env)
        assert code == 0, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku")
    j("runner", "pause")
    w = tmp_path / "w"
    w.mkdir()
    t = j("add", "waits for data", "--workdir", str(w), "--tier", "medium")["id"]
    logf = open(tmp_path / "runner.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=logf, stderr=logf, start_new_session=True)
    try:
        j("runner", "slots", "1")
        j("runner", "resume")
        for _ in range(90):
            c = j("show", t)
            if len([r for r in c["runs"] if r["outcome"]]) >= 3:
                break
            time.sleep(0.5)
        time.sleep(8)  # a 4th launch would have happened within this window
        card = j("show", t)
    finally:
        try:
            j("runner", "stop", "--now")
        except AssertionError:
            pass
        p.terminate()
    runs = sorted(card["runs"], key=lambda r: r["id"])
    print([(r["id"], r["outcome"]) for r in runs])
    assert [r["outcome"] for r in runs] == ["agent_open", "agent_open", "release_loop"], runs
    assert 540 < card["not_before"] - time.time() <= 600
    code, text, _ = abx("show", t, env=env)
    print(text)
    assert "relaunch loop: 3 short released runs; backoff until" in text
    assert (tmp_path / "fake.log.n").read_text().count("run") == 3
    code, out, _ = abx("outcomes", "--json", env=env)
    assert json.loads(out)["totals"]["failures"].get("release_loop") == 1
