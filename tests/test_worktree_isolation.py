"""worktree=on always isolates (busy or not), never falls back to the shared dir. Throwaway AB_HOME, fake `claude`."""
import json
import subprocess
from pathlib import Path

from conftest import ROOT
from test_workdir_lock import rig, wait, status  # noqa: F401  (rig fixture)


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def repo(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    git(d, "init", "-q", "-b", "master")
    git(d, "config", "user.email", "t@t"); git(d, "config", "user.name", "t")
    (d / "f").write_text("1")
    git(d, "add", "-A"); git(d, "commit", "-qm", "one")
    return d


def events(j, tid):
    return json.dumps(j("log", tid))


def test_free_dir_worktree_on_isolates(rig, tmp_path):
    j = rig
    d = repo(tmp_path)
    t = j("add", "x", "--workdir", str(d), "-l", "worktree=on")["id"]
    j.start()
    assert wait(lambda: status(j, t) == "claimed")
    lb = j("show", t)["labels"]
    assert lb["worktree_branch"] == f"ab/{t}"
    assert Path(lb["worktree_dir"]).is_dir() and lb["worktree_dir"] != str(d)
    assert git(lb["worktree_dir"], "branch", "--show-current") == f"ab/{t}"


def test_free_dir_worktree_off_shared(rig, tmp_path):
    j = rig
    d = repo(tmp_path)
    t = j("add", "x", "--workdir", str(d), "-l", "worktree=off")["id"]
    j.start()
    assert wait(lambda: status(j, t) == "claimed")
    assert "worktree_dir" not in j("show", t)["labels"]


def test_non_git_waits(rig, tmp_path):
    j = rig
    d = tmp_path / "plain"
    d.mkdir()
    t = j("add", "x", "--workdir", str(d), "-l", "worktree=on")["id"]
    j.start()
    assert wait(lambda: "not a git dir" in events(j, t))
    assert status(j, t) == "open"


def test_share_workdir_skips_with_event(rig, tmp_path):
    j = rig
    j("config", "set", "share_workdir", "true")
    d = repo(tmp_path)
    t = j("add", "x", "--workdir", str(d), "-l", "worktree=on")["id"]
    j.start()
    assert wait(lambda: status(j, t) == "claimed")
    assert "worktree_dir" not in j("show", t)["labels"]
    assert wait(lambda: "share_workdir" in events(j, t))
