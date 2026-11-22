"""`ab whoami` reports the run inferred from the run home, and why inference refused."""
import json

from conftest import clean_env  # noqa: F401
from test_run_identity import live_run, _ab  # noqa: F401


def _who(abx, r):
    code, out, err = _ab(abx, str(r["home"]), ["whoami", "--json"], {"HERMES_HOME": str(r["home"])})
    assert code == 0, err
    return json.loads(out)


def test_inferred_run(abx, live_run):
    r = live_run(run_id=7)
    d = _who(abx, r)
    assert d["inferred_run"] == 7 and d["inferred_reason"] is None


def test_missing_marker(abx, live_run):
    r = live_run(run_id=7)
    (r["home"] / "ab-run.json").unlink()
    d = _who(abx, r)
    assert d["inferred_run"] is None
    assert d["inferred_reason"].startswith("run home ") and "has no ab-run.json" in d["inferred_reason"]


def test_foreign_marker(abx, live_run):
    r = live_run(run_id=7)
    mk = r["home"] / "ab-run.json"
    m = json.loads(mk.read_text())
    m["ab_home"] = "/elsewhere/other"
    mk.write_text(json.dumps(m))
    d = _who(abx, r)
    assert d["inferred_run"] is None
    assert d["inferred_reason"].startswith("run home ") and "marker does not match" in d["inferred_reason"]


def test_not_a_run_home(abx, tmp_path):
    code, out, err = _ab(abx, str(tmp_path), ["whoami"])
    assert code == 0, err
    assert "inferred_run=None inferred_reason=None" in out


def _who_env(abx, cwd, args, env):
    code, out, err = _ab(abx, str(cwd), ["whoami", "--json", *args], env)
    assert code == 0, err
    return json.loads(out)


def test_effective_identity_from_run_home(abx, live_run):
    r = live_run(run_id=7)
    d = _who(abx, r)
    assert d["actor"] == "hermes#7" and d["actor_source"] == "run home"
    assert d["board"] == "main" and d["board_source"] == "run home"
    assert d["run"] == 7 and d["task"] == r["task"]
    assert d["admin"] is not None and d["inferred_run"] == 7


def test_effective_identity_flag_wins(abx, live_run):
    r = live_run(run_id=7)
    d = _who_env(abx, r["home"], ["--as", "someone#9"], {"HERMES_HOME": str(r["home"])})
    assert d["actor"] == "someone#9" and d["actor_source"] == "flag"


def test_effective_identity_env(abx, tmp_path):
    d = _who_env(abx, tmp_path, [], {"AB_ACTOR": "x#1"})
    assert d["actor"] == "x#1" and d["actor_source"] == "env"
    assert d["board_source"] == "default"


def test_effective_identity_default(abx, tmp_path):
    d = _who_env(abx, tmp_path, [], {})
    assert d["actor"] == "agent" and d["actor_source"] == "default"
    assert d["board_source"] == "default" and d["inferred_reason"] is None
    assert d["task"] is None and d["run"] is None
