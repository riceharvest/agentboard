"""Runner workdir lock: cross-board busy check (also across a reload), lock-free non-code cards, opt-in git worktree.
Throwaway AB_HOME, fake `claude` that sleeps."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

FAKE_CLAUDE = "#!/bin/sh\necho \"$@\" > \"$FAKE_ARGS.$$\"\nexec sleep 300\n"


@pytest.fixture
def rig(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_ARGS": str(tmp_path / "args")}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku")
    j("runner", "pause")
    procs = []

    def start(slots="4"):
        p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        procs.append(p)
        j("runner", "slots", slots)
        j("runner", "resume")
    j.start = start
    j.env = env
    yield j
    try:
        j("runner", "stop", "--now")
    except AssertionError:
        pass
    for p in procs:
        p.terminate()


def status(j, tid):
    return j("show", tid)["status"]


def wait(cond, secs=30):
    for _ in range(secs * 2):
        if cond():
            return True
        time.sleep(0.5)
    return False


def test_cross_board_busy_and_reload(rig, tmp_path):
    """Same workdir on two boards: the second waits, also after a runner reload re-adopted the first run."""
    j = rig
    j("boards", "create", "other")
    wd = tmp_path / "shared"
    a = j("add", "first", "--workdir", str(wd))["id"]
    j.start()
    assert wait(lambda: status(j, a) == "claimed")
    b = j("--board", "other", "add", "second", "--workdir", str(wd) + "/")["id"]  # same dir, other board, trailing slash
    time.sleep(4)
    assert status(j, b) == "open"
    assert j("runner", "reload")["action"] == "reload"
    time.sleep(6)  # new process adopted run a (adopted children carry no workdir)
    assert status(j, a) == "claimed" and status(j, b) == "open"
    j("cancel", a)
    assert wait(lambda: status(j, b) == "claimed")


def test_manual_claim_holds_the_dir(rig, tmp_path):
    """A card claimed by a live session (not a runner child) also holds its workdir."""
    j = rig
    wd = tmp_path / "shared"
    # runnable=0 card so the runner leaves it alone
    h = j("add", "held", "--workdir", str(wd), "--manual")
    j("claim", h["id"])
    w = j("add", "waits", "--workdir", str(wd))["id"]
    j.start()
    time.sleep(4)
    assert status(j, w) == "open"
    j("done", h["id"], "x")
    assert wait(lambda: status(j, w) == "claimed")


def test_watch_card_skips_the_lock(rig, tmp_path):
    j = rig
    wd = tmp_path / "shared"
    build = j("add", "long build", "--workdir", str(wd))["id"]
    watch = j("add", "overseer sweep", "--workdir", str(wd), "-l", "type=watch")["id"]
    plain = j("add", "another build", "--workdir", str(wd))["id"]
    j.start()
    assert wait(lambda: status(j, watch) == "claimed" and status(j, build) == "claimed")
    time.sleep(3)
    assert status(j, plain) == "open"  # a lock-free card doesn't hold it, the build does


def test_worktree_when_busy(rig, tmp_path):
    j = rig
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q")
    g("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    first = j("add", "first", "--workdir", str(repo))["id"]
    second = j("add", "second", "--workdir", str(repo), "-l", "worktree=on")["id"]
    plain = j("add", "third", "--workdir", str(repo))["id"]
    j.start()
    assert wait(lambda: status(j, second) == "claimed" and status(j, first) == "claimed")
    time.sleep(3)
    assert status(j, plain) == "open"  # no opt-in: waits as before
    lb = j("show", second)["labels"]
    assert lb["worktree_branch"] == f"ab/{second}" and os.path.isdir(lb["worktree_dir"])
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert lb["worktree_dir"] in out
    briefs = [open(os.path.join(str(tmp_path), f)).read() for f in os.listdir(tmp_path) if f.startswith("args.")]
    assert any("WORKTREE:" in b and f"ab/{second}" in b for b in briefs), briefs
