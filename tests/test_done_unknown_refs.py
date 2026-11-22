"""`ab done` warns about cited card ids that exist on no board; --strict-refs refuses."""
import json


def _claim(abx, title="x"):
    tid = json.loads(abx("--json", "add", title)[1])["id"]
    assert abx("claim", tid)[0] == 0
    return tid


def _status(abx, tid):
    return json.loads(abx("--json", "show", tid)[1])["status"]


def test_all_known(abx):
    other = json.loads(abx("--json", "add", "other")[1])["id"]
    tid = _claim(abx)
    code, o, e = abx("done", tid, f"verdict: see {other}", "--allow-dirty")
    assert code == 0 and "exist on no board" not in e
    assert _status(abx, tid) == "done"


def test_known_on_other_board(abx):
    assert abx("boards", "create", "second")[0] == 0
    other = json.loads(abx("--json", "--board", "second", "add", "other")[1])["id"]
    tid = _claim(abx)
    code, o, e = abx("done", tid, f"verdict: see {other}", "--allow-dirty")
    assert code == 0 and "exist on no board" not in e


def test_unknown_warns(abx):
    tid = _claim(abx)
    code, o, e = abx("done", tid, "verdict: cites t_abcdef12", "--allow-dirty")
    assert code == 0 and "t_abcdef12" in e and "exist on no board" in e
    assert _status(abx, tid) == "done"


def test_unknown_strict(abx):
    tid = _claim(abx)
    code, o, e = abx("done", tid, "verdict: cites t_abcdef12", "--allow-dirty", "--strict-refs")
    assert code == 2 and "t_abcdef12" in e
    assert _status(abx, tid) == "claimed"


def test_ignored_and_placeholder(abx):
    tid = _claim(abx)
    text = "verdict: ok t_00000000\n```\nt_abcdef12\n```\nexport AB_TASK=t_abcdef13\n"
    code, o, e = abx("done", tid, text, "--allow-dirty", "--strict-refs")
    assert code == 0 and "exist on no board" not in e and "t_00000000" in e and "not checked" in e
    assert _status(abx, tid) == "done"
