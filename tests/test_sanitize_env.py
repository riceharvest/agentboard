"""Inside a worker shell (AB_RUN_HOME set), an explicit AB_HOME for another board keeps the caller's AB_TASK/AB_ACTOR."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import cli, store  # noqa: E402


def _run(monkeypatch, tmp_path, home_set, **env):
    for k in ("AB_RUN", "AB_ACTOR", "AB_BOARD", "AB_TASK", "AB_RUN_HOME", "AB_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(store, "HOME", tmp_path / "other")
    monkeypatch.setenv("AB_RUN_HOME", str(tmp_path / "runhome"))
    monkeypatch.setenv("AB_RUN", "7")
    monkeypatch.setenv("AB_BOARD", "workerboard")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if home_set:
        monkeypatch.setenv("AB_HOME", str(tmp_path / "other"))
    cli._sanitize_worker_env()


def test_sanitize_keeps_task_and_actor_with_explicit_ab_home(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, True, AB_TASK="y", AB_ACTOR="me#1")
    assert os.environ["AB_TASK"] == "y" and os.environ["AB_ACTOR"] == "me#1"
    assert "AB_RUN" not in os.environ and "AB_RUN_HOME" not in os.environ


def test_sanitize_still_drops_all_without_explicit_ab_home(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, False, AB_TASK="y", AB_ACTOR="me#1")
    assert not any(k in os.environ for k in ("AB_TASK", "AB_ACTOR", "AB_RUN", "AB_BOARD", "AB_RUN_HOME"))


def test_sanitize_noop_on_own_home(monkeypatch, tmp_path):
    for k in ("AB_RUN", "AB_ACTOR", "AB_BOARD", "AB_TASK", "AB_RUN_HOME", "AB_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(store, "HOME", tmp_path / "h")
    monkeypatch.setenv("AB_RUN_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("AB_TASK", "t")
    cli._sanitize_worker_env()
    assert os.environ["AB_TASK"] == "t" and "AB_RUN_HOME" in os.environ


def test_sanitize_end_to_end(abx, tmp_path):
    from conftest import clean_env
    import subprocess
    rc, o, e = abx("add", "card")
    env = clean_env(abx.home, AB_RUN_HOME=str(tmp_path / "somewhere_else"), AB_TASK="nope_task", AB_ACTOR="me#1")
    p = subprocess.run([sys.executable, "-m", "agentboard", "whoami"], capture_output=True, text=True, env=env,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert p.returncode == 0, p.stderr
    assert "me#1" in p.stdout


def test_sanitize_keeps_board_with_explicit_ab_home(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, True, AB_TASK="y", AB_BOARD="fixture")
    assert os.environ["AB_BOARD"] == "fixture" and os.environ["AB_TASK"] == "y"
    assert "AB_RUN" not in os.environ and "AB_RUN_HOME" not in os.environ


def test_sanitize_warns_when_dropping(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, False, AB_TASK="y")
    err = capsys.readouterr().err
    assert "AB_TASK" in err and "AB_BOARD" in err and "ignoring" in err
