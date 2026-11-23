"""`-l worktree=on,lane=b` must be two labels, not {"worktree": "on,lane=b"}; comma lists stay one value."""
import json
import os
import subprocess
import sys

from conftest import ab_with_identity  # noqa: E402
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ab(home, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    env["AB_HOME"] = str(home)
    return ab_with_identity(lambda *extra: subprocess.run([sys.executable, str(REPO / "ab"), *args, *extra],
                                                            env=env, capture_output=True, text=True), args)


def _labels(home, tid):
    r = _ab(home, "show", tid, "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    return d.get("labels") or d["task"]["labels"]


def test_add_splits_comma(tmp_path):
    r = _ab(tmp_path, "add", "x", "--body", "y", "-l", "worktree=on,lane=b", "--json")
    assert r.returncode == 0, r.stderr
    lab = _labels(tmp_path, json.loads(r.stdout)["id"])
    assert lab["worktree"] == "on" and lab["lane"] == "b"


def test_comma_value_kept_and_edit_splits(tmp_path):
    r = _ab(tmp_path, "add", "x", "--body", "y", "-l", "watch_quiet=done,cancelled", "--json")
    tid = json.loads(r.stdout)["id"]
    assert _labels(tmp_path, tid)["watch_quiet"] == "done,cancelled"
    e = _ab(tmp_path, "edit", tid, "-l", "a=1,b=2")
    assert e.returncode == 0, e.stderr
    lab = _labels(tmp_path, tid)
    assert lab["a"] == "1" and lab["b"] == "2"


def test_part_without_equals_refused(tmp_path):
    r = _ab(tmp_path, "add", "x", "--body", "y", "-l", "nokey")
    assert r.returncode != 0
