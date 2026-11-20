"""Runner workdir lock: cross-board busy check (also across a reload), lock-free non-code cards, auto git worktree,
`ab ls --ready` busy reasons, and worker env hygiene across AB_HOME. Throwaway AB_HOME, fake `claude` that sleeps."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

FAKE_CLAUDE = "#!/bin/sh\necho \"$@\" > \"$FAKE_ARGS.$$\"\nenv | grep '^AB_' > \"$FAKE_ENV.$$\" || true\nexec sleep 300\n"


@pytest.fixture
def rig(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home),
           "FAKE_ARGS": str(tmp_path / "args"), "FAKE_ENV": str(tmp_path / "env")}

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
    j.home = abx.home
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


def test_role_run_survives_reload(rig, tmp_path):
    """A role-labelled run (owner role#N) is re-adopted under the same owner after a reload, not killed as expired."""
    j = rig
    a = j("add", "overseer sweep", "-l", "role=overseer")["id"]
    j.start()
    assert wait(lambda: status(j, a) == "claimed")
    owner = j("show", a)["owner"]
    assert owner.startswith("overseer#")
    assert j("runner", "reload")["action"] == "reload"
    time.sleep(6)
    t = j("show", a)
    assert t["status"] == "claimed" and t["owner"] == owner
    pids = [int(p.name.rsplit(".", 1)[1]) for p in tmp_path.glob("args.*")]
    assert len(pids) == 1
    os.kill(pids[0], 0)  # the fake agent still runs; before the adopt fix the reload killed it as "expired"


def test_role_run_survives_reload_with_runs_owner_column(rig, tmp_path):
    """Regression : a runs.owner column (present on the live board) shadowed the joined tasks.owner in
    adopt(), so every role run was re-adopted as harness#N, killed 'expired' with 0 tokens, and burned an attempt."""
    import sqlite3
    j = rig
    db = sqlite3.connect(str(j.home / ("board" + ".db")))
    db.execute("ALTER TABLE runs ADD COLUMN owner TEXT")
    db.commit()
    db.close()
    a = j("add", "overseer sweep", "-l", "role=overseer")["id"]
    j.start()
    assert wait(lambda: status(j, a) == "claimed")
    before = j("show", a)
    assert before["owner"].startswith("overseer#")
    assert j("runner", "reload")["action"] == "reload"
    time.sleep(8)
    after = j("show", a)
    assert after["status"] == "claimed" and after["owner"] == before["owner"]
    assert after["attempts"] == before["attempts"]
    db = sqlite3.connect(str(j.home / ("board" + ".db")))
    assert db.execute("SELECT count(*) FROM runs WHERE ended_at IS NOT NULL").fetchone()[0] == 0  # nothing was killed


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


def _init_git(repo):
    repo.mkdir(parents=True, exist_ok=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q")
    g("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def test_auto_worktree_when_busy_and_opt_out(rig, abx, tmp_path):
    """Two code cards on the same git workdir with 2 slots: both start, second in its own worktree;
    worktree=off waits even with free slots and `ab ls --ready` reports 'workdir busy (auto worktree off)'."""
    j = rig
    repo = _init_git(tmp_path / "repo")
    first = j("add", "first", "--workdir", str(repo))["id"]
    second = j("add", "second", "--workdir", str(repo))["id"]
    optout = j("add", "optout", "--workdir", str(repo), "-l", "worktree=off")["id"]
    j.start(slots="2")
    assert wait(lambda: status(j, first) == "claimed" and status(j, second) == "claimed")
    # Second card (or whichever was second) got its own worktree on branch ab/<id>
    lbs = {tid: j("show", tid)["labels"] for tid in (first, second)}
    wt_ids = [tid for tid, lb in lbs.items() if lb.get("worktree_dir")]
    assert len(wt_ids) == 1, lbs
    wt_id = wt_ids[0]
    lb = lbs[wt_id]
    assert lb["worktree_branch"] == f"ab/{wt_id}" and os.path.isdir(lb["worktree_dir"])
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert lb["worktree_dir"] in out
    briefs = [open(os.path.join(str(tmp_path), f)).read() for f in os.listdir(tmp_path) if f.startswith("args.")]
    assert any("WORKTREE:" in b and f"ab/{wt_id}" in b for b in briefs), briefs
    # `ab ls --ready` text and JSON show 'workdir busy (auto worktree off)' for optout, NOT for a card that will auto-worktree
    fourth = j("add", "fourth", "--workdir", str(repo))["id"]
    code, txt, err = abx("ls", "--ready", env=j.env)
    assert code == 0, err
    ready_json = {x["id"]: x for x in j("ls", "--ready")}
    assert ready_json[optout].get("wait_reason") == "workdir busy (auto worktree off)"
    assert "wait_reason" not in ready_json[fourth]
    optout_line = next(l for l in txt.splitlines() if optout in l)
    fourth_line = next(l for l in txt.splitlines() if fourth in l)
    assert "workdir busy (auto worktree off)" in optout_line
    assert "workdir busy" not in fourth_line
    # Open a free slot: optout still waits on the busy dir while fourth starts in a worktree
    j("runner", "slots", "4")
    assert wait(lambda: status(j, fourth) == "claimed")
    time.sleep(3)
    assert status(j, optout) == "open"
    # Runner exported AB_RUN_HOME == AB_HOME to spawned workers
    envs = [open(os.path.join(str(tmp_path), f)).read() for f in os.listdir(tmp_path) if f.startswith("env.")]
    assert envs and all(f"AB_RUN_HOME={abx.home}" in e for e in envs), envs


def test_auto_worktree_config_off_and_non_git(rig, abx, tmp_path):
    """`ab config set auto_worktree false` makes plain cards wait while worktree=on still starts;
    non-git workdir waits with 'workdir busy' (not 'auto worktree off')."""
    j = rig
    repo = _init_git(tmp_path / "repo")
    nongit = tmp_path / "plain_dir"
    nongit.mkdir()
    j("config", "set", "auto_worktree", "false")
    first = j("add", "first", "--workdir", str(repo))["id"]
    waits = j("add", "waits", "--workdir", str(repo))["id"]
    optin = j("add", "optin", "--workdir", str(repo), "-l", "worktree=on")["id"]
    ng1 = j("add", "ng1", "--workdir", str(nongit))["id"]
    ng2 = j("add", "ng2", "--workdir", str(nongit))["id"]
    j.start(slots="4")
    assert wait(lambda: status(j, first) == "claimed" and status(j, optin) == "claimed" and status(j, ng1) == "claimed")
    time.sleep(3)
    assert status(j, waits) == "open"
    assert status(j, ng2) == "open"
    ready = {x["id"]: x for x in j("ls", "--ready")}
    assert ready[waits].get("wait_reason") == "workdir busy (auto worktree off)"
    assert ready[ng2].get("wait_reason") == "workdir busy"


def test_worker_env_hygiene_across_ab_home(tmp_path):
    """Running `ab` with a different AB_HOME than AB_RUN_HOME strips AB_RUN/AB_ACTOR/AB_BOARD/AB_TASK;
    pointing at the same AB_HOME (even via trailing slash or symlink) keeps the worker identity locked."""
    worker_home = tmp_path / "real_home"
    other_home = tmp_path / "throwaway_home"
    sym_home = tmp_path / "sym_home"
    worker_home.mkdir()
    other_home.mkdir()
    sym_home.symlink_to(worker_home)

    worker_env = {
        "AB_HOME": str(worker_home),
        "AB_RUN_HOME": str(worker_home),
        "AB_RUN": "42",
        "AB_ACTOR": "claude#42",
        "AB_BOARD": "customboard",
        "AB_TASK": "t_11223344",
    }

    def run_ab(home_val, *args):
        env = clean_env(home_val, **{k: v for k, v in worker_env.items() if k != "AB_HOME"})
        return subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=ROOT, capture_output=True, text=True, env=env)

    # Different AB_HOME: worker identity and board are stripped -> defaults to agent / main, admin works
    r = run_ab(other_home, "--json", "whoami")
    assert r.returncode == 0, r.stderr
    who = json.loads(r.stdout)
    assert who["actor"] == "agent" and who["board"] == "main" and who["run"] is None and who["task"] is None, who
    r = run_ab(other_home, "--json", "config", "set", "pacer", "false")
    assert r.returncode == 0, r.stderr
    r = run_ab(other_home, "--json", "add", "works on throwaway")
    assert r.returncode == 0, r.stderr

    # Same AB_HOME (trailing slash and symlink): worker identity stays locked, admin still denied
    for same in (str(worker_home) + "/", str(sym_home)):
        r = run_ab(same, "--json", "--as", "admin", "whoami")
        assert r.returncode == 0, r.stderr
        who = json.loads(r.stdout)
        assert who["actor"] == "claude#42" and who["board"] == "customboard" and who["run"] == "42", who
        r = run_ab(same, "--json", "config", "set", "pacer", "false")
        assert r.returncode != 0, "worker must not gain admin on its own AB_HOME"
