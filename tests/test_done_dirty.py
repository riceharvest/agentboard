"""`ab done` refuses while the task worktree has untracked or modified files; --allow-dirty overrides."""
import json
import subprocess


def _git(wt, *a):
    subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)


def test_done_refuses_dirty_worktree(abx, tmp_path):
    wt = tmp_path / "wt"; wt.mkdir()
    _git(wt, "init", "-q"); _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    tid = json.loads(abx("--json", "add", "x", "--label", f"worktree_dir={wt}")[1])["id"]
    assert abx("claim", tid)[0] == 0
    (wt / "out").mkdir(); (wt / "out" / "x.jsonl").write_text("{}")
    code, o, e = abx("done", tid, "x")
    assert code != 0 and "out/x.jsonl" in (o + e), (code, o, e)
    assert json.loads(abx("--json", "show", tid)[1])["status"] != "done"
    code, o, e = abx("done", tid, "x", "--allow-dirty")
    assert code == 0, (o, e)
    assert json.loads(abx("--json", "show", tid)[1])["status"] == "done"


def _shared(abx, tmp_path):
    wd = tmp_path / "shared"; wd.mkdir()
    _git(wd, "init", "-q"); _git(wd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    return wd


def test_done_ignores_other_cards_dirty_files_in_shared_checkout(abx, tmp_path):
    import os, time
    wd = _shared(abx, tmp_path)
    env = {"AB_WORKDIR": str(wd)}
    a = json.loads(abx("--json", "add", "A")[1])["id"]
    b = json.loads(abx("--json", "add", "B")[1])["id"]
    assert abx("claim", a)[0] == 0
    (wd / "other.txt").write_text("a's file")
    old = time.time() - 3600; os.utime(wd / "other.txt", (old, old))  # written before B started
    assert abx("claim", b)[0] == 0
    code, o, e = abx("done", b, "x", env=env)
    assert code == 0, (code, o, e)
    assert "other.txt" in (o + e) and "ignored" in (o + e)  # one-line note


def test_done_refuses_own_dirty_file_in_shared_checkout(abx, tmp_path):
    wd = _shared(abx, tmp_path)
    env = {"AB_WORKDIR": str(wd)}
    b = json.loads(abx("--json", "add", "B")[1])["id"]
    assert abx("claim", b)[0] == 0
    (wd / "mine.txt").write_text("b's file")
    code, o, e = abx("done", b, "x", env=env)
    assert code != 0 and "mine.txt" in (o + e), (code, o, e)
    assert abx("done", b, "x", "--allow-dirty", env=env)[0] == 0


def test_done_succeeds_when_unrelated_file_dirty_before_claim(abx, tmp_path):
    wd = _shared(abx, tmp_path)
    env = {"AB_WORKDIR": str(wd)}
    (wd / "unrelated.txt").write_text("someone else's work")  # dirty before this task exists
    b = json.loads(abx("--json", "add", "B")[1])["id"]
    assert abx("claim", b)[0] == 0
    code, o, e = abx("done", b, "x", env=env)  # task wrote nothing; no --allow-dirty
    assert code == 0, (code, o, e)
