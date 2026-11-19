"""Tests for the generic git hygiene hooks in hooks/ (commit-msg trailer stripping,
pre-push scanning only the pushed range). Runs real git in throwaway repos."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

HOOKS = Path(ROOT) / "hooks"
ZERO = "0" * 40


def git(repo, *args, check=True, input=None, env=None):
    e = dict(os.environ)
    # Isolate from the developer's global/system git config (hooksPath, signing, templates).
    e.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
             GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
             GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    if env:
        e.update(env)
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       input=input, env=e)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stdout}{p.stderr}")
    return p


def commit(repo, relpath, content, msg, no_verify=False):
    target = Path(repo) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", relpath)
    args = ["commit", "-m", msg]
    if no_verify:
        args.append("--no-verify")
    return git(repo, *args, check=False)


@pytest.fixture
def repo(tmp_path):
    """A repo with both hygiene hooks installed, plus a bare 'origin' remote."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "test@example.com")
    git(r, "config", "commit.gpgsign", "false")
    common = Path(git(r, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = (r / common).resolve()
    for name in ("commit-msg", "pre-push"):
        dst = common / "hooks" / name
        shutil.copy2(HOOKS / name, dst)
        dst.chmod(0o755)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(remote, "config", "receive.denyDeleteCurrent", "ignore")
    git(r, "remote", "add", "origin", str(remote))
    return r


def push(repo, refspec, env=None):
    return git(repo, "push", "origin", refspec, check=False, env=env)


def seed_remote_with_bad_history(repo):
    """Master history that trips both guards, already on the remote (seeded --no-verify)."""
    p = commit(repo, "notes.txt", "secret-marker in old history\n",
               "old commit\n\nCo-Authored-By: Claude <noreply@anthropic.com>", no_verify=True)
    assert p.returncode == 0, p.stdout + p.stderr
    git(repo, "config", "hygiene.forbiddenPattern", "secret-marker")
    git(repo, "push", "--no-verify", "origin", "master")
    git(repo, "fetch", "-q", "origin")


# ---------------- commit-msg ----------------

def test_commit_msg_strips_ai_trailer(repo):
    p = commit(repo, "a.txt", "a\n",
               "Add a\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nCo-Authored-By: Jane Human <jane@example.com>")
    assert p.returncode == 0, p.stdout + p.stderr
    msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "Co-Authored-By: Claude" not in msg
    assert "Co-Authored-By: Jane Human <jane@example.com>" in msg
    assert msg.startswith("Add a")


def test_commit_msg_strips_generated_lines(repo):
    p = commit(repo, "b.txt", "b\n",
               "Add b\n\n🤖 Generated with Claude Code\nCo-Authored-By: OpenAI Codex <codex@openai.com>")
    assert p.returncode == 0, p.stdout + p.stderr
    msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "Generated with" not in msg and "Co-Authored-By" not in msg


def test_commit_msg_keeps_human_coauthor(repo):
    p = commit(repo, "c.txt", "c\n", "Add c\n\nCo-Authored-By: Jane Human <jane@example.com>")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Jane Human" in git(repo, "log", "-1", "--format=%B").stdout


def test_commit_msg_pattern_configurable(repo):
    git(repo, "config", "hygiene.aiPattern", "my-internal-bot")
    p = commit(repo, "d.txt", "d\n",
               "Add d\n\nCo-Authored-By: My-Internal-Bot <bot@corp.example>\nCo-Authored-By: Claude <noreply@anthropic.com>")
    assert p.returncode == 0, p.stdout + p.stderr
    msg = git(repo, "log", "-1", "--format=%B").stdout
    assert "My-Internal-Bot" not in msg
    # configured pattern replaces the default: Claude is no longer matched
    assert "Claude" in msg


# ---------------- pre-push ----------------

def test_pre_push_new_branch_clean_commit_succeeds(repo):
    seed_remote_with_bad_history(repo)
    git(repo, "checkout", "-q", "-b", "feature/clean")
    p = commit(repo, "feature.txt", "clean work\n", "Clean feature commit")
    assert p.returncode == 0, p.stdout + p.stderr
    p = push(repo, "feature/clean")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "feature/clean" in git(repo, "ls-remote", "--heads", "origin").stdout


def test_pre_push_blocks_ai_trailer_in_outgoing_commit(repo):
    commit(repo, "base.txt", "base\n", "base")
    push(repo, "master")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "feature/dirty")
    p = commit(repo, "x.txt", "x\n",
               "Add x\n\nCo-Authored-By: Claude <noreply@anthropic.com>", no_verify=True)
    assert p.returncode == 0, p.stdout + p.stderr
    p = push(repo, "feature/dirty")
    assert p.returncode != 0 and "AI co-author trailer" in p.stdout + p.stderr


def test_pre_push_update_scans_only_new_range(repo):
    seed_remote_with_bad_history(repo)
    # master's old commits are already on the remote; a clean new commit must pass
    p = commit(repo, "next.txt", "more clean work\n", "Next clean commit")
    assert p.returncode == 0, p.stdout + p.stderr
    p = push(repo, "master")
    assert p.returncode == 0, p.stdout + p.stderr


def test_pre_push_blocks_forbidden_content_in_range(repo):
    commit(repo, "base.txt", "base\n", "base")
    push(repo, "master")
    git(repo, "fetch", "-q", "origin")
    git(repo, "config", "hygiene.forbiddenPattern", "secret-marker")
    p = commit(repo, "leak.txt", "contains secret-marker here\n", "Leak")
    assert p.returncode == 0, p.stdout + p.stderr
    p = push(repo, "master")
    assert p.returncode != 0 and "forbidden content" in p.stdout + p.stderr


def test_pre_push_delete_ref_passes(repo):
    commit(repo, "base.txt", "base\n", "base")
    push(repo, "master")
    git(repo, "fetch", "-q", "origin")
    p = push(repo, ":master")
    assert p.returncode == 0, p.stdout + p.stderr