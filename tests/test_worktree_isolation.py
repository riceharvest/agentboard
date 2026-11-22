"""worktree=on always isolates (busy or not), never falls back to the shared dir. Throwaway AB_HOME, fake `claude`."""
import json
import subprocess
import time
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


def test_worktree_on_not_held_by_busy_shared_dir(rig, abx, tmp_path):
    """A worktree=on card never waits on another card's lock of the shared checkout: no workdir_locked hold,
    `ab ls --ready` lists it, and it starts in its own worktree as soon as a slot is free."""
    j = rig
    d = repo(tmp_path)
    holder = j("add", "long job", "--workdir", str(d))["id"]
    wt = j("add", "worktree build", "--workdir", str(d), "-l", "worktree=on")["id"]
    j.start(slots="1")
    assert wait(lambda: status(j, holder) == "claimed")
    assert wait(lambda: wt in [x["id"] for x in j("ls", "--ready")])
    time.sleep(4)  # a few runner ticks: the hold scan runs too
    assert status(j, wt) == "open"
    show = abx("show", wt, env=j.env)[1]
    assert "hold=workdir_locked" not in show
    assert wt not in abx_stuck_ids(j)
    j("runner", "slots", "2")
    assert wait(lambda: status(j, wt) == "claimed")
    lb = j("show", wt)["labels"]
    assert lb.get("worktree_dir") and lb["worktree_branch"] == f"ab/{wt}"


def abx_stuck_ids(j):
    return [x["id"] for x in j("ls", "--stuck")]


def test_plain_card_still_held_by_busy_shared_dir(rig, abx, tmp_path):
    """Negative control: the same card without worktree=on (auto worktree off) still gets the workdir_locked hold."""
    j = rig
    j("config", "set", "auto_worktree", "false")
    d = repo(tmp_path)
    holder = j("add", "long job", "--workdir", str(d))["id"]
    plain = j("add", "plain build", "--workdir", str(d))["id"]
    j.start(slots="1")
    assert wait(lambda: status(j, holder) == "claimed")
    assert wait(lambda: "hold=workdir_locked:" in abx("show", plain, env=j.env)[1])
    assert status(j, plain) == "open"
    assert plain in abx_stuck_ids(j)



def test_worktree_on_cannot_isolate_says_so_in_hold(rig, abx, tmp_path):
    """When worktree=on cannot isolate (non-git workdir) the card may fall back to the lock, but the hold reason says so."""
    j = rig
    d = tmp_path / "plain"
    d.mkdir()
    holder = j("add", "long job", "--workdir", str(d))["id"]
    wt = j("add", "no git", "--workdir", str(d), "-l", "worktree=on")["id"]
    j.start(slots="1")
    assert wait(lambda: status(j, holder) == "claimed")
    assert wait(lambda: "cannot isolate" in abx("show", wt, env=j.env)[1])
    assert wt in abx_stuck_ids(j)
