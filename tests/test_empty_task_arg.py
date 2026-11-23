"""`ab done "$AB_TASK" "summary"` with AB_TASK empty: the summary lands in the id slot. The error must say so
(not the bare 'no task <summary>'), and a real task id must still reach the store's own checks."""
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


def test_summary_in_id_slot_names_the_cause(tmp_path):
    r = _ab(tmp_path, "done", "my summary text here")
    assert r.returncode == 1
    assert "AB_TASK is empty" in r.stderr
    assert "Pass the id first" in r.stderr


def test_real_id_is_not_rewritten(tmp_path):
    add = _ab(tmp_path, "add", "t", "--channel", "main", "--json")
    assert add.returncode == 0, add.stderr
    import json
    tid = json.loads(add.stdout)["id"]
    r = _ab(tmp_path, "done", tid, "summary")
    assert "AB_TASK is empty" not in r.stderr
    assert r.returncode == 1  # open, not claimed: the store refuses, as before
