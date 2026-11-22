"""ab ls / ab unmerged default to the board of the run whose home this shell is in (no AB_* exported)."""
import json

from conftest import clean_env  # noqa: F401
from test_run_identity import live_run, _ab  # noqa: F401


def _run(abx, r, args, extra=None):
    env = {"HERMES_HOME": str(r["home"]), **(extra or {})}
    code, out, err = _ab(abx, str(r["home"]), args, env)
    assert code == 0, err
    return out


def test_ls_uses_run_board(abx, live_run):
    abx("boards", "create", "lab")
    r = live_run(run_id=7, board="lab")
    abx("--json", "add", "other lab row", "--board", "lab")
    abx("--json", "add", "main row", "--board", "main")
    rows = json.loads(_run(abx, r, ["ls", "--json"]))
    assert rows and {x["board"] for x in rows} == {"lab"}
    rows = json.loads(_run(abx, r, ["ls", "--json", "-B", "main"]))
    assert {x["board"] for x in rows} == {"main"}
    rows = json.loads(_run(abx, r, ["ls", "--json"], {"AB_BOARD": "main"}))
    assert {x["board"] for x in rows} == {"main"}


def test_unmerged_uses_run_board(abx, live_run, tmp_path):
    abx("boards", "create", "lab")
    r = live_run(run_id=7, board="lab")
    tid = json.loads(abx("--json", "add", "wt", "--board", "lab", "--label", "worktree_branch=ab/none",
                         "--workdir", str(tmp_path))[1])["id"]
    assert abx("claim", tid)[0] == 0
    assert abx("done", tid, "x", "--force")[0] == 0
    out = _run(abx, r, ["unmerged"])
    assert "0 of 0" not in out.splitlines()[-1]
