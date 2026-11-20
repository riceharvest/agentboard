"""`ab merge TASK` lets git decide: a dirty main tree is fine unless local changes overlap the merged files."""
import json
import subprocess


def git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, check=True).stdout


def _repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "t@t")
    git(d, "config", "user.name", "t")
    (d / "a.txt").write_text("a\n")
    (d / "b.txt").write_text("b\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "init")
    return d


def _task(abx, d):
    rc, o, e = abx("--json", "add", "card", "--workdir", str(d))
    assert rc == 0, e
    return json.loads(o)["id"]


def _branch(d, tid):
    git(d, "checkout", "-q", "-b", f"ab/{tid}")
    (d / "a.txt").write_text("a changed\n")
    git(d, "commit", "-qam", "work")
    git(d, "checkout", "-q", "main")


def test_merge_with_dirty_non_overlapping_file_succeeds(abx, tmp_path):
    d = _repo(tmp_path)
    tid = _task(abx, d)
    _branch(d, tid)
    (d / "b.txt").write_text("dirty other work\n")  # another card's uncommitted change
    rc, o, e = abx("merge", tid)
    assert rc == 0, e
    assert (d / "a.txt").read_text() == "a changed\n"
    assert (d / "b.txt").read_text() == "dirty other work\n"


def test_merge_refuses_only_on_overlap_and_names_files(abx, tmp_path):
    d = _repo(tmp_path)
    tid = _task(abx, d)
    _branch(d, tid)
    (d / "a.txt").write_text("dirty overlapping\n")
    rc, o, e = abx("--json", "merge", tid)
    assert rc == 1
    assert "a.txt" in e and "overlap" in e
    assert json.loads(o)["overlap"] == ["a.txt"]
    assert (d / "a.txt").read_text() == "dirty overlapping\n"  # nothing lost
