import io
import json
import os
import subprocess
import sys
import time

from agentboard import runner, store


def git(*a, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True)


def setup(abx, tmp_path):
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    repo = tmp_path / "r"
    repo.mkdir()
    assert git("init", "-q", cwd=repo).returncode == 0
    assert git("commit", "-q", "--allow-empty", "-m", "one", cwd=repo).returncode == 0
    return tid, repo


def make(abx, tmp_path, monkeypatch):
    from tests.test_park import _conn
    tid, repo = setup(abx, tmp_path)
    monkeypatch.setattr(store, "HOME", abx.home)
    c = _conn(abx)
    log = io.StringIO()
    r = runner.Runner(c, log=log)
    task = {"id": tid}
    first = r.make_worktree(task, str(repo))
    assert first, r.wt_err
    wt = first[0]
    gd = git("rev-parse", "--absolute-git-dir", cwd=wt).stdout.strip()
    return r, c, task, repo, wt, gd, log


def commit_ok(wt):
    open(os.path.join(wt, "f"), "a").write("x\n")
    assert git("add", "f", cwd=wt).returncode == 0
    return git("commit", "-q", "-m", "two", cwd=wt).returncode == 0


def age(path, secs):
    t = time.time() - secs
    os.utime(path, (t, t))


def test_dead_lock_cleared_on_reuse(abx, tmp_path, monkeypatch):
    r, c, task, repo, wt, gd, log = make(abx, tmp_path, monkeypatch)
    lock = os.path.join(gd, "index.lock")
    open(lock, "w").close()
    age(lock, 600)
    assert r.make_worktree(task, str(repo))
    assert not os.path.exists(lock)
    assert commit_ok(wt)
    assert "index.lock" in log.getvalue()
    ev = c.execute("SELECT payload FROM events WHERE task_id=? AND kind='worktree_lock'", (task["id"],)).fetchall()
    assert ev and json.loads(ev[0][0])["file"] == "index.lock"


def test_fresh_lock_left(abx, tmp_path, monkeypatch):
    r, c, task, repo, wt, gd, log = make(abx, tmp_path, monkeypatch)
    lock = os.path.join(gd, "index.lock")
    open(lock, "w").close()
    r.make_worktree(task, str(repo))
    assert os.path.exists(lock)
    assert "younger" in log.getvalue()


def test_held_lock_left(abx, tmp_path, monkeypatch):
    r, c, task, repo, wt, gd, log = make(abx, tmp_path, monkeypatch)
    lock = os.path.join(gd, "index.lock")
    p = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'w'); print('r',flush=True); time.sleep(30)", lock],
                         stdout=subprocess.PIPE, text=True)
    try:
        p.stdout.readline()
        age(lock, 600)
        r.make_worktree(task, str(repo))
        assert os.path.exists(lock)
        assert "held open" in log.getvalue()
    finally:
        p.kill()
        p.wait()


def test_nonempty_lock_left(abx, tmp_path, monkeypatch):
    r, c, task, repo, wt, gd, log = make(abx, tmp_path, monkeypatch)
    lock = os.path.join(gd, "index.lock")
    open(lock, "w").write("12345\n")
    age(lock, 600)
    r.make_worktree(task, str(repo))
    assert os.path.exists(lock)
    assert "not empty" in log.getvalue()
