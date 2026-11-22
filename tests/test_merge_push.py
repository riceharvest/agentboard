"""`ab merge --push` / cfg merge_push: a merged branch reaches origin, not just the local checkout.
A bare repo stands in for the remote, so nothing talks to a real GitHub."""
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, clean_env


def _git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout.strip()


@pytest.fixture
def repo(abx, tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(bare))
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-b", "master")
    (wt / "a.txt").write_text("one\n")
    _git(wt, "add", "a.txt")
    _git(wt, "commit", "-m", "one")
    _git(wt, "remote", "add", "origin", str(bare))
    _git(wt, "push", "-q", "-u", "origin", "master")
    _git(wt, "checkout", "-q", "-b", "ab/card")
    (wt / "a.txt").write_text("two\n")
    _git(wt, "commit", "-am", "two")
    _git(wt, "checkout", "-q", "master")
    return wt, bare


def _card(abx, wt):
    code, out, err = abx("--json", "add", "merge me", "--cmd", "true", "--workdir", str(wt),
                         "--as", "tester", "--why", "t")
    assert code == 0, err
    tid = json.loads(out)["id"]
    return tid


def _set_rule(abx, on):
    code, _, err = abx("--json", "config", "set", "merge_push", "true" if on else "false",
                       "--as", "tester", "--why", "t")
    assert code == 0, err


def test_merge_push_sends_the_branch_to_origin(abx, repo):
    wt, bare = repo
    tid = _card(abx, wt)
    out = abx("--json", "merge", tid, "--branch", "ab/card", "--into", str(wt), "--push",
              "--as", "tester", "--why", "t")[1]
    body = json.loads(out)
    assert body["merged"] and body["pushed"] is True, body
    assert _git(bare, "log", "--oneline", "-1", "master").endswith("two")


def test_config_merge_push_applies_without_the_flag(abx, repo):
    wt, bare = repo
    tid = _card(abx, wt)
    _set_rule(abx, True)
    body = json.loads(abx("--json", "merge", tid, "--branch", "ab/card", "--into", str(wt),
                          "--as", "tester", "--why", "t")[1])
    assert body["pushed"] is True, body
    assert _git(bare, "log", "--oneline", "-1", "master").endswith("two")


def test_no_push_by_default(abx, repo):
    wt, bare = repo
    tid = _card(abx, wt)
    body = json.loads(abx("--json", "merge", tid, "--branch", "ab/card", "--into", str(wt),
                          "--as", "tester", "--why", "t")[1])
    assert body["merged"] and body["pushed"] is None, body
    assert "two" not in _git(bare, "log", "--oneline", "master")
