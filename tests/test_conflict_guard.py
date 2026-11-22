"""`ab` refuses to run from a checkout with unresolved merge-conflict markers (and the runner refuses to claim from a
checkout that does not import at all) instead of dying in SyntaxError.

Every test builds a throwaway copy of the package in tmp_path and runs `python -m agentboard` against it: the live
checkout must never be touched.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFLICT = "<<<<<<< HEAD\nx = 1\n=======\ny = 2\n>>>>>>> side\n"


@pytest.fixture
def checkout(tmp_path):
    shutil.copytree(os.path.join(ROOT, "agentboard"), tmp_path / "agentboard", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def run(c, *args, code=None):
    """(rc, stdout, stderr) for `python -m agentboard ARGS` (or `python -c CODE`) run from the copied checkout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    env.update(PYTHONPATH=str(c), AB_HOME=str(c / "ab"))
    cmd = [sys.executable, "-c", code] if code else [sys.executable, "-m", "agentboard", *args]
    p = subprocess.run(cmd, cwd=str(c), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _append(path, text):
    with open(path, "a") as f:
        f.write(text)


# -- unit: the scan itself --------------------------------------------------------------------------------------------

def test_scan_finds_package_and_root_modules_only(tmp_path):
    from agentboard import _sanity
    pkg = tmp_path / "agentboard"
    pkg.mkdir()
    (pkg / "cli.py").write_text("x = 1\n" + CONFLICT)
    (tmp_path / "a.py").write_text(CONFLICT + "y = 2\n")
    (tmp_path / "README.md").write_text(CONFLICT)          # non-.py: not a module, must not count
    (pkg / "GUIDE.md").write_text(CONFLICT)
    got = [(str(f.relative_to(tmp_path)), n) for f, n, _ in _sanity.conflicted_modules(pkg)]
    assert got == [("a.py", 1), ("a.py", 3), ("a.py", 5),
                   ("agentboard/cli.py", 2), ("agentboard/cli.py", 4), ("agentboard/cli.py", 6)]


# -- the refusal ------------------------------------------------------------------------------------------------------

def test_markers_in_a_package_module_exit_2_naming_path_and_line(checkout):
    cli = checkout / "agentboard" / "cli.py"
    line = len(cli.read_text().splitlines()) + 1      # the first marker lands on the next line
    _append(cli, CONFLICT)
    rc, out, err = run(checkout, "ls")
    assert rc == 2, (rc, out, err)
    assert f"ab: checkout at {checkout} has unresolved merge-conflict markers:" in err
    assert f"agentboard/cli.py:{line}  <<<<<<< HEAD" in err
    assert "resolve the merge" in err
    assert "Traceback" not in err


def test_markers_at_the_checkout_root_exit_2(checkout):
    (checkout / "a.py").write_text(CONFLICT)          # a root module breaks `python -m agentboard` from the checkout too
    rc, out, err = run(checkout, "ls")
    assert rc == 2
    assert "a.py:1  <<<<<<< HEAD" in err


def test_clean_checkout_unchanged(checkout):
    rc, out, err = run(checkout, "ls", "--json")
    assert rc == 0 and json.loads(out or "[]") == []
    rc, out, err = run(checkout, "--help")
    assert rc == 0 and "agentboard: a task board" in out


def test_markers_only_in_a_non_python_file_change_nothing(checkout):
    _append(checkout / "README.md", CONFLICT)
    rc, out, err = run(checkout, "ls", "--json")
    assert rc == 0 and json.loads(out or "[]") == []
    rc, out, err = run(checkout, "--help")
    assert rc == 0


def test_version_and_guide_still_work_on_a_conflicted_tree(checkout):
    _append(checkout / "agentboard" / "cli.py", CONFLICT)   # cli.py itself is the broken module
    rc, out, err = run(checkout, "--version")
    assert rc == 0 and re.fullmatch(r"ab \S+\n", out), (rc, out, err)
    rc, out, err = run(checkout, "guide")
    assert rc == 0 and out.startswith("# agentboard"), (rc, out[:80], err)


# -- runner: status + no claims ---------------------------------------------------------------------------------------

def test_runner_status_reports_unloadable_checkout_and_claims_nothing(checkout):
    (checkout / "agentboard" / "harness.py").write_text("def broken(:\n")   # lazy import: `ab` itself still runs
    rc, out, err = run(checkout, "ls")
    assert rc == 0
    tid = run(checkout, "add", "claim guard")[1].strip()
    assert tid.startswith("t_")
    rc, out, err = run(checkout, "runner", "status")
    assert rc == 0
    assert "checkout_unloadable: SyntaxError: invalid syntax (agentboard/harness.py, line 1)" in out
    # one fill() with four free slots claims nothing (and says why)
    rc, out, err = run(checkout, code="from agentboard import store, runner\n"
                                      "c = store.connect()\n"
                                      "print(runner.Runner(c).fill(4))\n")
    assert rc == 0 and "checkout_unloadable:" in out, (rc, out, err)
    assert tid not in run(checkout, "ls", "--status", "claimed")[1]
    assert tid in run(checkout, "ls", "--status", "open")[1]
