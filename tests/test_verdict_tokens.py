"""`ab done` enforces the board's optional verdict_tokens config."""
import json
import pytest
from agentboard import store


@pytest.mark.parametrize("text,tok", [
    ("GO: numbers", "GO"), ("  go — x", "GO"), ("**PASS**.", "PASS"), ("> # `NO-GO` | why", "NO-GO"),
    ("FAIL: x", "FAIL"), ("RESULT: NOT SUPPORTED", "RESULT"), ("", ""),
])
def test_first_token(text, tok):
    assert store.verdict_first_token(text) == tok


def test_ok_and_unset():
    assert store.verdict_ok("anything", [])
    assert store.verdict_ok("Go: x", ["GO", "NO-GO"])
    assert not store.verdict_ok("verdict is GO", ["GO"])




def _claim(abx, title="x"):
    tid = json.loads(abx("--json", "add", title)[1])["id"]
    assert abx("claim", tid)[0] == 0
    return tid


def test_done_enforced(abx):
    code, o, e = abx("config", "set", "verdict_tokens", "GO,NO-GO")
    assert code == 0, (o, e)
    assert abx("config", "get", "verdict_tokens")[1].strip() == '"GO,NO-GO"'
    tid = _claim(abx)
    code, o, e = abx("done", tid, "verdict is GO: numbers", "--allow-dirty")
    assert code != 0 and "GO, NO-GO" in (o + e) and "verdict is GO" in (o + e) and "--no-verdict-check" in (o + e)
    assert json.loads(abx("--json", "show", tid)[1])["status"] == "claimed"
    assert abx("done", tid, "GO: numbers", "--allow-dirty")[0] == 0
    t2 = _claim(abx, "y")
    assert abx("done", t2, "**NO-GO** — reason", "--allow-dirty")[0] == 0
    t3 = _claim(abx, "z")
    assert abx("done", t3, "whatever", "--allow-dirty", "--no-verdict-check")[0] == 0


def test_unset_regression(abx):
    tid = _claim(abx)
    assert abx("done", tid, "anything", "--allow-dirty")[0] == 0
