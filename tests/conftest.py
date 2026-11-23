import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentboard import store  # noqa: E402

# Every card/config change is audited (a named actor plus a reason). The suite under tests/ is not an agent: give it a
# synthetic identity instead of repeating --as/--why in ~every test. Only a refusal that NAMES that requirement is
# retried, so a test asserting any other refusal still sees it.
TEST_IDENTITY = ("--as", "tester", "--why", "tests")
_IDENTITY_REFUSAL = ("anonymous change refused", "change refused: say why")


@pytest.fixture(autouse=True)
def _audit_why():
    """The same policy for a test that calls store.add_task/cfg_set in-process: name the reason there too."""
    tok = store.WHY.set("tests")
    yield
    store.WHY.reset(tok)


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_") and k not in ("HERMES_HOME", "BASH_ENV")}  # never act as the worker running the tests
    env.update(AB_HOME=str(home), PYTHONPATH=ROOT, **extra)
    # a reason for an audited change: naming one does not touch identity inference (which reads actor/task/board only)
    env.setdefault("AB_WHY", "tests")
    return env


def ab_with_identity(call, args=()):
    """`call(*extra) -> CompletedProcess`, retried once with the test identity when the refusal names who/why.

    Lets a test module keep its own `_ab` helper (different env, script path) and still not spell the identity out."""
    r = call()
    if r.returncode and "--as" not in args and "--why" not in args \
            and any(m in (r.stderr + r.stdout) for m in _IDENTITY_REFUSAL):
        r = call(*TEST_IDENTITY)
    return r


@pytest.fixture
def abx(tmp_path):
    """Run `ab ...` against a throwaway AB_HOME: abx('ls', '--json') -> (exit code, stdout, stderr).

    A call refused for missing who/why is retried once with the test identity, so a test only spells identity out
    when the identity itself is what it checks."""
    home = tmp_path / "ab"

    def run(*args, env=None, stdin=None):
        def go(*a):
            p = subprocess.run([sys.executable, "-m", "agentboard", *a], cwd=ROOT, capture_output=True, text=True,
                               env=clean_env(home, **(env or {})), input=stdin)
            return p.returncode, p.stdout, p.stderr
        code, out, err = go(*args)
        if code != 0 and any(m in (err + out) for m in _IDENTITY_REFUSAL) and "--as" not in args and "--why" not in args:
            code, out, err = go(*args, *TEST_IDENTITY)
        return code, out, err
    run.home = home
    return run
