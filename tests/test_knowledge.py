"""Tests for knowledge factory: outcomes fix, mining, priors injection, lock-park staggering, and worktree pruning."""
import fcntl
import json
import subprocess

import pytest

from agentboard import knowledge, outcomes, runner, store, unmerged

T = 1_000_000.0


@pytest.fixture(autouse=True)
def _why():
    tok = store.WHY.set("test")
    yield
    store.WHY.reset(tok)


def _card(c, title, status="done", result="ok", labels=None, board="main"):
    tid = store.add_task(c, "tester", title, labels=labels or {}, board=board)
    c.execute("UPDATE tasks SET status=?, result=? WHERE id=?", (status, result, tid))
    return tid


def _run(c, tid, harness="claude", model="m1", tier="medium", outcome="agent_done",
         dur_s=120, out=500, cr=50000, cost=0.25, detail=None, start=T):
    c.execute(
        "INSERT INTO runs(task_id,harness,model,tier,outcome,started_at,ended_at,input,output,cache_read,cost_usd,detail)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, harness, model, tier, outcome, start, None if outcome is None else start + dur_s, 100, out, cr, cost, detail),
    )


def test_outcomes_classifies_abandoned_verify_and_idle(tmp_path):
    c = store.connect(tmp_path / "b.db")
    t1 = _card(c, "a", "open", "", {"type": "build"})
    _run(c, t1, outcome="agent_open")
    _run(c, t1, outcome="agent_claimed")
    _run(c, t1, outcome="verify_failed")
    _run(c, t1, outcome="idle")
    _run(c, t1, outcome=None)  # truly still running

    rep = outcomes.ledger(c, by="label:type", t=T + 1000)
    f = rep["groups"][0]["failures"]
    assert f.get("abandoned") == 2
    assert f.get("verify_failed") == 1
    assert f.get("stall_killed") == 1
    assert f.get("running") == 1
    assert "other" not in f


def test_mine_priors_and_brief_injection(tmp_path):
    c = store.connect(tmp_path / "k.db")
    for i in range(6):
        tid = _card(c, f"exp {i}", "done", "done ok", {"type": "experiment", "tpl": "experiment@aaa"}, board="main")
        _run(c, tid, outcome="parked", detail="waiting for gpu lock held by pid 123")
        _run(c, tid, outcome="agent_open", detail="No module named 'missingpkg'")
        _run(c, tid, outcome="agent_done")
    # Two WEAKER reviews on main/experiment
    t_ok = _card(c, "reviewed", "done", "done", {"type": "experiment", "tpl": "experiment@aaa"}, board="main")
    _run(c, t_ok, outcome="agent_done")
    _card(c, "rev1", "done", "WEAKER: overstated scope on split B", {"type": "review", "reviews": t_ok}, board="main")
    t_ok2 = _card(c, "reviewed2", "done", "done", {"type": "experiment", "tpl": "experiment@aaa"}, board="main")
    _run(c, t_ok2, outcome="agent_done")
    _card(c, "rev2", "done", "WEAKER: missing baseline control", {"type": "review", "reviews": t_ok2}, board="main")

    rep = knowledge.mine(c, scan_logs=False, t=T + 5000)
    assert rep["summary"]["parked_lock"] == 6
    assert rep["summary"]["abandoned"] == 6
    assert rep["summary"]["review_verdicts"]["WEAKER"] == 2
    assert "main/experiment" in rep["priors"]
    assert rep["templates"][0]["tpl"] == "experiment@aaa"
    assert "top (board/type) hotspots:" in knowledge.render_mine(rep)

    # Save priors; injection is opt-in (default off)
    saved = knowledge.save_priors(c, rep)
    assert saved >= 1
    new_tid = store.add_task(c, "tester", "next exp", labels={"type": "experiment"}, board="main")
    task = store.get_task(c, new_tid)
    b_off = runner.build_brief(c, task, str(tmp_path), 1, actor="w1")
    assert "KNOWN PITFALLS & PRIORS" not in b_off

    store.cfg_set(c, "inject_priors", True, actor="tester", why="test")
    b_on = runner.build_brief(c, task, str(tmp_path), 1, actor="w1")
    assert "KNOWN PITFALLS & PRIORS" in b_on
    assert "main/experiment" in b_on
    # Byte-identical static prefix preserved for prompt caching
    assert b_on[:400] == b_off[:400]


def test_park_lock_and_staggered_wake(tmp_path):
    c = store.connect(tmp_path / "p.db")
    lock_file = tmp_path / "gpu.lock"
    lock_file.write_text("held\n")

    t1 = store.add_task(c, "tester", "job 1", priority=10)
    t2 = store.add_task(c, "tester", "job 2", priority=5)
    store.claim(c, "w1", t1)
    store.claim(c, "w2", t2)
    store.park(c, t1, "w1", "waiting for external lock file to release", lock=str(lock_file))
    store.park(c, t2, "w2", "waiting for external lock file to release", lock=str(lock_file))

    # Hold exclusive flock -> neither card wakes
    fd = open(lock_file, "r+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.wake_parked(c) == []
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()

    # Lock released -> staggered wake: only highest-priority card wakes on tick 1
    w1 = runner.wake_parked(c)
    assert len(w1) == 1 and w1[0][0] == t1
    # Tick 2 wakes the second card
    w2 = runner.wake_parked(c)
    assert len(w2) == 1 and w2[0][0] == t2


def test_prune_worktrees_safety(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    git("init", "-b", "master")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("v1\n")
    git("add", "f.txt")
    git("commit", "-m", "init")

    c = store.connect(tmp_path / "w.db")
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()

    # 1. Done + clean + merged -> pruned
    t_done = _card(c, "done clean", "done", "ok", {"worktree_branch": "ab/done1"})
    git("worktree", "add", "-b", "ab/done1", str(wt_root / t_done), "master")
    c.execute("UPDATE tasks SET workdir=? WHERE id=?", (str(repo), t_done))

    # 2. Open -> skipped_active
    t_open = _card(c, "open card", "open", "", {"worktree_branch": "ab/open1"})
    git("worktree", "add", "-b", "ab/open1", str(wt_root / t_open), "master")
    c.execute("UPDATE tasks SET workdir=? WHERE id=?", (str(repo), t_open))

    # 3. Done + dirty -> skipped_dirty
    t_dirty = _card(c, "done dirty", "done", "ok", {"worktree_branch": "ab/dirty1"})
    git("worktree", "add", "-b", "ab/dirty1", str(wt_root / t_dirty), "master")
    (wt_root / t_dirty / "untracked.txt").write_text("keep\n")
    c.execute("UPDATE tasks SET workdir=? WHERE id=?", (str(repo), t_dirty))

    # 4. Done + unmerged -> skipped_unmerged
    t_unm = _card(c, "done unmerged", "done", "ok", {"worktree_branch": "ab/unm1"})
    git("worktree", "add", "-b", "ab/unm1", str(wt_root / t_unm), "master")
    (wt_root / t_unm / "new.txt").write_text("commit\n")
    subprocess.run(["git", "-C", str(wt_root / t_unm), "add", "new.txt"], check=True)
    subprocess.run(["git", "-C", str(wt_root / t_unm), "commit", "-m", "ahead"], check=True)
    c.execute("UPDATE tasks SET workdir=? WHERE id=?", (str(repo), t_unm))

    dry = unmerged.prune_worktrees(c, wt_root, dry_run=True)
    assert len(dry["removed"]) == 1 and dry["removed"][0]["id"] == t_done
    assert dry["skipped_active"] == 1
    assert len(dry["skipped_dirty"]) == 1
    assert len(dry["skipped_unmerged"]) == 1
    assert (wt_root / t_done).exists()

    applied = unmerged.prune_worktrees(c, wt_root, dry_run=False)
    assert len(applied["removed"]) == 1
    assert not (wt_root / t_done).exists()
    assert (wt_root / t_open).exists()
    assert (wt_root / t_dirty).exists()
    assert (wt_root / t_unm).exists()


def test_cli_mine_priors_worktrees(abx):
    code, out, err = abx("mine", "--write", "--json")
    assert code == 0, err
    assert json.loads(out)["summary"]["runs"] == 0
    code, out, err = abx("priors", "--json")
    assert code == 0, err
    assert "priors" in json.loads(out)
    code, out, err = abx("worktrees", "prune", "--json")
    assert code == 0, err
    assert json.loads(out)["dry_run"] is True
