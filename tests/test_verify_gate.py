"""--verify done gate: the runner runs the card's command after `ab done`; exit 0 accepts, else the card reopens with the output in
the next brief, at most verify_rounds reopenings, then it is failed. Throwaway AB_HOME with a fake `claude` that calls `ab done`."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env

FAKE = r'''#!/bin/sh
printf '%s\n=====\n' "$2" >> "$FAKE_LOG.briefs"
n=$(cat "$FAKE_LOG.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$FAKE_LOG.n"
[ "$n" -ge "$FAKE_OK_ON" ] && touch ok
tid=$(echo "$2" | sed -n "s/.*agentboard task \(t_[0-9a-f]*\).*/\1/p" | head -1)
ab done "$tid" "attempt $n claims done" >> "$FAKE_LOG.done" 2>&1
exit 0
'''


def run_card(abx, tmp_path, ok_on, verify="test -f ok || { echo missing ok file; ls; exit 3; }"):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_LOG": str(tmp_path / "fake.log"), "FAKE_OK_ON": str(ok_on)}

    def j(*args):
        code, out, err = abx("--json", *args, env=env)
        assert code == 0, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"), ("backoff", "1s")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku")
    j("runner", "pause")
    w = tmp_path / "w"
    w.mkdir()
    t = j("add", "gated card", "--body", "make ok", "--workdir", str(w), "--tier", "medium", "--verify", verify)["id"]
    logf = open(tmp_path / "runner.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=logf, stderr=logf, start_new_session=True)
    try:
        j("runner", "slots", "1")
        j("runner", "resume")
        for _ in range(120):
            c = j("show", t)
            if c["status"] in ("done", "dead") and all(r["outcome"] for r in c["runs"]):
                break
            time.sleep(0.5)
        card = j("show", t)
    finally:
        try:
            j("runner", "stop", "--now")
        except AssertionError:
            pass
        p.terminate()
    code, text, _ = abx("show", t, env=env)
    print(text)
    return card, [r["outcome"] for r in sorted(card["runs"], key=lambda r: r["id"])], (tmp_path / "fake.log.briefs").read_text().split("=====\n")


def test_reopens_then_accepts(abx, tmp_path):
    card, outcomes, briefs = run_card(abx, tmp_path, ok_on=2)
    assert card["status"] == "done", card["status"]
    assert outcomes == ["verify_failed", "agent_done"], outcomes
    assert "DONE GATE" in briefs[0] and "REJECTED" not in briefs[0]
    assert "REJECTED" in briefs[1] and "missing ok file" in briefs[1]


def test_third_failure_fails_card(abx, tmp_path):
    card, outcomes, briefs = run_card(abx, tmp_path, ok_on=99)
    assert card["status"] == "dead", card["status"]
    assert outcomes == ["verify_failed"] * 3, outcomes
    assert "missing ok file" in (card["result"] or "")
    assert "round 2 of 3" in briefs[2]
