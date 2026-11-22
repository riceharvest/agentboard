"""`ab unmerged` lists done worktree cards whose branch is not on the default branch; `ab done` warns once."""
import json
import subprocess

G = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    git(r, "init", "-q", "-b", "master"); git(r, *G, "commit", "-q", "--allow-empty", "-m", "base")
    return r


def branch(r, name, n):
    git(r, "checkout", "-q", "-b", name)
    for i in range(n):
        git(r, *G, "commit", "-q", "--allow-empty", "-m", f"work {i}")
    git(r, "checkout", "-q", "master")


def card(abx, repo, br, done=True):
    tid = json.loads(abx("--json", "add", "x", "--label", f"worktree_branch={br}", "--workdir", str(repo))[1])["id"]
    if done:
        assert abx("claim", tid)[0] == 0
        assert abx("done", tid, "x", "--force")[0] == 0
    return tid


def test_merged_absent_ahead_listed_missing(abx, tmp_path):
    r = make_repo(tmp_path)
    branch(r, "ab/m", 1); git(r, "merge", "-q", "ab/m")
    branch(r, "ab/a", 2)
    branch(r, "ab/gone", 1); git(r, "branch", "-q", "-D", "ab/gone")
    m, a, g = card(abx, r, "ab/m"), card(abx, r, "ab/a"), card(abx, r, "ab/gone")
    code, o, e = abx("unmerged")
    assert code == 0, e
    assert m not in o
    assert f"{a}  ab/a  +2  work 1" in o
    assert f"{g}  ab/gone  branch missing" in o
    assert o.strip().splitlines()[-1] == "unmerged: 2 of 3 done worktree card(s), oldest 0d"


def test_json_shape_and_repo_flag(abx, tmp_path):
    r = make_repo(tmp_path); branch(r, "ab/a", 2)
    a = card(abx, tmp_path / "elsewhere-not-a-repo", "ab/a")
    code, o, _ = abx("--json", "unmerged", "--repo", str(r))
    d = json.loads(o)
    assert code == 0 and set(d) == {"unmerged", "count", "total", "oldest_days"}
    assert d["count"] == 1 and d["total"] == 1 and d["unmerged"][0]["id"] == a and d["unmerged"][0]["ahead"] == 2


def test_done_warns_once_when_ahead(abx, tmp_path):
    r = make_repo(tmp_path); branch(r, "ab/a", 3)
    tid = card(abx, r, "ab/a", done=False)
    assert abx("claim", tid)[0] == 0
    code, o, e = abx("done", tid, "x", "--force")
    assert code == 0
    assert (o + e).count("note: ab/a has 3 commit(s) not on master") == 1
    assert f"git -C {r} merge ab/a" in e
    assert json.loads(abx("--json", "show", tid)[1])["status"] == "done"


def test_done_silent_when_merged_or_no_branch(abx, tmp_path):
    r = make_repo(tmp_path); branch(r, "ab/m", 1); git(r, "merge", "-q", "ab/m")
    for br in ("ab/m", None):
        args = ["--label", f"worktree_branch={br}"] if br else []
        tid = json.loads(abx("--json", "add", "x", "--workdir", str(r), *args)[1])["id"]
        assert abx("claim", tid)[0] == 0
        code, o, e = abx("done", tid, "x", "--force")
        assert code == 0 and "note:" not in o + e


def test_ls_status_done_shows_unmerged_section(abx, tmp_path):
    r = make_repo(tmp_path); branch(r, "ab/a", 1); card(abx, r, "ab/a")
    code, o, e = abx("ls", "--status", "done")
    assert code == 0 and "unmerged: 1 of 1 done worktree card(s)" in e
