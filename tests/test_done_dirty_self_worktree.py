"""`ab done` run from a linked git worktree the worker made itself (a card with no worktree_dir label) checks that worktree's
dirty files, not the shared checkout's: its own uncommitted work blocks the close, the shared checkout's files do not."""
import json
import os
import subprocess
import sys

from conftest import TEST_IDENTITY, clean_env


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


def _ab(home, cwd, *args, workdir=None):
    env = clean_env(home, **({"AB_WORKDIR": str(workdir)} if workdir else {}))
    p = subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def test_done_checks_self_made_worktree_not_shared_checkout(tmp_path):
    home = tmp_path / "ab"
    shared = tmp_path / "shared"; shared.mkdir()
    _git(shared, "init", "-q"); _git(shared, "commit", "-q", "--allow-empty", "-m", "i")
    wt = tmp_path / "own-wt"
    _git(shared, "worktree", "add", "-q", "-b", "card-branch", str(wt))
    tid = json.loads(_ab(home, shared, "--json", "add", "B", "--as", "tester", "--why", "tests", workdir=shared)[1])["id"]
    assert _ab(home, shared, "claim", tid, *TEST_IDENTITY, workdir=shared)[0] == 0

    # the shared checkout is dirty (another card's file): not this card's to answer for
    (shared / "other.txt").write_text("someone else's file")
    # the worker's own worktree has an uncommitted output file: this card's
    (wt / "out.txt").write_text("mine")

    code, o, e = _ab(home, wt, "done", tid, "x", *TEST_IDENTITY, workdir=shared)
    assert code != 0 and "out.txt" in (o + e) and "other.txt" not in (o + e), (code, o, e)

    _git(wt, "add", "out.txt")
    _git(wt, "commit", "-q", "-m", "out")
    code, o, e = _ab(home, wt, "done", tid, "x", *TEST_IDENTITY, workdir=shared)
    assert code == 0, (code, o, e)


def test_done_from_shared_checkout_still_ignores_unrelated_dirty_files(tmp_path):
    home = tmp_path / "ab"
    shared = tmp_path / "shared"; shared.mkdir()
    _git(shared, "init", "-q"); _git(shared, "commit", "-q", "--allow-empty", "-m", "i")
    tid = json.loads(_ab(home, shared, "--json", "add", "B", "--as", "tester", "--why", "tests", workdir=shared)[1])["id"]
    os.makedirs(shared / "d", exist_ok=True)
    (shared / "d" / "old.txt").write_text("someone else's, before the claim")
    old = os.path.getmtime(shared / "d" / "old.txt") - 3600
    os.utime(shared / "d" / "old.txt", (old, old))
    assert _ab(home, shared, "claim", tid, *TEST_IDENTITY, workdir=shared)[0] == 0
    code, o, e = _ab(home, shared, "done", tid, "x", *TEST_IDENTITY, workdir=shared)
    assert code == 0, (code, o, e)
