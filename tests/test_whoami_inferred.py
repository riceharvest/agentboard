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
