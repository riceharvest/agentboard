"""worktree_base: a card's own worktree branches from the mainline when the shared checkout sits on another card's branch."""
import subprocess
from agentboard.runner import worktree_base


def g(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def repo(tmp_path, main="master"):
    d = tmp_path / "r"
    d.mkdir()
    g(d, "init", "-q", "-b", main)
    g(d, "config", "user.email", "t@t"); g(d, "config", "user.name", "t")
    (d / "f").write_text("1")
    g(d, "add", "-A"); g(d, "commit", "-qm", "one")
    return d


def test_plain_checkout_uses_head(tmp_path, monkeypatch):
    monkeypatch.delenv("AB_WORKTREE_BASE", raising=False)
    d = repo(tmp_path)
    assert worktree_base(str(d)) == "HEAD"
    g(d, "checkout", "-q", "-b", "feature")
    assert worktree_base(str(d)) == "HEAD"   # a deliberate non-card branch is respected


def test_card_branch_checkout_uses_mainline(tmp_path, monkeypatch):
    monkeypatch.delenv("AB_WORKTREE_BASE", raising=False)
    d = repo(tmp_path)
    g(d, "checkout", "-q", "-b", "ab/t_deadbeef")
    assert worktree_base(str(d)) == "master"
    (tmp_path / "x").mkdir()
    d2 = repo(tmp_path / "x", main="main")
    g(d2, "checkout", "-q", "-b", "ab/t_cafe0001")
    assert worktree_base(str(d2)) == "main"


def test_card_branch_without_mainline_falls_back_to_head(tmp_path, monkeypatch):
    monkeypatch.delenv("AB_WORKTREE_BASE", raising=False)
    d = repo(tmp_path, main="trunk")
    g(d, "checkout", "-q", "-b", "ab/t_deadbeef")
    assert worktree_base(str(d)) == "HEAD"


def test_override(tmp_path, monkeypatch):
    d = repo(tmp_path)
    monkeypatch.setenv("AB_WORKTREE_BASE", "master")
    assert worktree_base(str(d)) == "master"
    monkeypatch.setenv("AB_WORKTREE_BASE", "nope")
    assert worktree_base(str(d)) == "HEAD"
