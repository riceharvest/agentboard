import json
import os
import subprocess
import sys
import pytest


@pytest.fixture
def ab(tmp_path):
    def run(*args, task=None, rc=0):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
        env["AB_HOME"] = str(tmp_path)
        if task:
            env["AB_TASK"] = task
        r = subprocess.run([sys.executable, "-m", "agentboard", *args], capture_output=True, text=True, env=env)
        assert r.returncode == rc, r.stdout + r.stderr
        return (r.stdout + r.stderr).strip()
    return run


def test_creator_task_wires_its_card_and_can_readd(ab):
    base = ab("add", "base", "--body", "x", "--as", "w1#1")
    child = ab("add", "child", "--body", "y", "--as", "w1#1", task=base)
    # a different actor (retry of the same card) still wires the card its task created
    ab("edit", child, "--as", "w1#2", "--dep", base, task=base)
    assert base in json.loads(ab("show", child, "--json"))["deps"]
    ab("edit", child, "--as", "w1#2", "--rm-dep", base, task=base)
    ab("edit", child, "--as", "w1#2", "--dep", base, task=base)
    assert base in json.loads(ab("show", child, "--json"))["deps"]


def test_foreign_card_still_needs_manage(ab):
    base = ab("add", "base", "--body", "x", "--as", "w1#1")
    foreign = ab("add", "foreign", "--body", "z", "--as", "w2#1")
    out = ab("edit", foreign, "--as", "w1#1", "--dep", base, task=base, rc=1)
    assert "manage" in out
    ab("edit", foreign, "--as", "w1#1", "--rm-dep", base, task=base, rc=1)
