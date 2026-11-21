"""limit_resume (opt-in): workers checkpoint; a retry after a limit death sees the commits + progress file + last note in its
brief; after limit_skip_after limit deaths of a card on one route, that route is skipped for the card (next tier entry).
Throwaway AB_HOME; a fake `claude` that commits, writes a progress file and dies on a usage limit; a fake `codex` that finishes."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

FAKE_CLAUDE = r'''#!/bin/sh
# -p BRIEF: keep every brief this route was started with
printf '%s\n=====\n' "$2" >> "$FAKE_LOG.briefs"
n=$(cat "$FAKE_LOG.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$FAKE_LOG.n"
echo "step $n done, next: step $((n+1))" > .ab-progress.md
echo "work $n" > "work$n.txt"
git add "work$n.txt" && git -c user.name=t -c user.email=t@t commit -qm "milestone $n"
tid=$(echo "$2" | sed -n "s/.*agentboard task \(t_[0-9a-f]*\).*/\1/p" | head -1)
ab note "$tid" "died after milestone $n" >> "$FAKE_LOG.note" 2>&1; echo "tid=$tid rc=$?" >> "$FAKE_LOG.note"
echo "Claude AI usage limit reached|$(( $(date +%s) - 5 ))" >&2
exit 1
'''
FAKE_CODEX = r'''#!/bin/sh
echo "codex ran" >> "$FAKE_LOG"
ab done "$AB_TASK" "finished on codex"
'''


def test_limit_deaths_resume_then_skip_route(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for n, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        (fake / n).write_text(body)
        (fake / n).chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_LOG": str(tmp_path / "fake.log")}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"),
                 ("cooldown.limit", "1s"), ("limit_resume", "on"), ("limit_skip_after", "2"), ("backoff", "1s")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku", "codex")
    j("runner", "pause")
    w = tmp_path / "w"
    w.mkdir()
    for c in (["init", "-q", "-b", "master"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(w), *c], check=True)
    t = j("add", "limit card", "--body", "do the work", "--workdir", str(w), "--tier", "medium")["id"]
    logf = open(tmp_path / "runner.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=logf, stderr=logf, start_new_session=True)
    try:
        j("runner", "slots", "1")
        j("runner", "resume")
        for _ in range(120):
            c = j("show", t)
            if c["status"] == "done" and all(r["outcome"] for r in c["runs"]):
                break
            time.sleep(0.5)
        card = j("show", t)
        assert card["status"] == "done", card["status"]
    finally:
        try:
            j("runner", "stop", "--now")
        except AssertionError:
            pass
        p.terminate()
    runs = [(r["harness"], r["outcome"]) for r in sorted(card["runs"], key=lambda r: r["id"])]
    print("RUNS", runs)
    assert runs == [("claude", "limit"), ("claude", "limit"), ("codex", "agent_done")], runs
    briefs = (tmp_path / "fake.log.briefs").read_text().split("=====\n")
    first, second = briefs[0], briefs[1]
    assert "CHECKPOINTS" in first and "RESUMING AFTER A PROVIDER LIMIT" not in first
    assert "RESUMING AFTER A PROVIDER LIMIT" in second
    assert "milestone 1" in second and "work1.txt" in second and "step 1 done, next: step 2" in second and "died after milestone 1" in second
    print("RETRY BRIEF SECTION:\n" + second[second.index("RESUMING AFTER"):second.index("BOARD (the")])
    log = (tmp_path / "runner.log").read_text()
    print("RUNNER LOG:\n" + log)
    assert "route skipped" in log or (tmp_path / "fake.log").exists()


def test_progress_path_is_per_task(tmp_path, monkeypatch):
    """Two cards in one workdir get distinct AB_PROGRESS paths, outside the repo; the brief names the path."""
    from agentboard import runner, store
    monkeypatch.setattr(store, "HOME", tmp_path)
    a, b = runner.progress_path("t_aaaa1111"), runner.progress_path("t_bbbb2222")
    assert a != b and a.parent == b.parent == tmp_path / "progress"
    a.parent.mkdir()
    a.write_text("A\n")
    b.write_text("B\n")
    assert a.read_text() == "A\n" and b.read_text() == "B\n"

    class C:
        def execute(self, *a):
            return self
        def fetchone(self):
            return None
    monkeypatch.setattr(runner, "cfg", lambda c, k: "on")
    assert "$AB_PROGRESS" in runner.checkpoint_rules(C(), "t_aaaa1111") and str(a) in runner.checkpoint_rules(C(), "t_aaaa1111")
