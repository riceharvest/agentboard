import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_") and k != "HERMES_HOME"}  # never act as the worker running the tests
    env.update(AB_HOME=str(home), PYTHONPATH=ROOT, **extra)
    return env


@pytest.fixture
def abx(tmp_path):
    """Run `ab ...` against a throwaway AB_HOME: abx('ls', '--json') -> (exit code, stdout, stderr)."""
    home = tmp_path / "ab"

    def run(*args, env=None, stdin=None):
        p = subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=ROOT, capture_output=True, text=True,
                           env=clean_env(home, **(env or {})), input=stdin)
        return p.returncode, p.stdout, p.stderr
    run.home = home
    return run
