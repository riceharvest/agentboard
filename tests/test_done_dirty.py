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
