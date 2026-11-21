"""A card held runnable=False prints the command that clears it; --runnable on|off is first class."""
import json


def _j(abx, *args, rc=0):
    code, out, err = abx("--json", *args)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


def test_runnable_off_hint_and_clear(abx):
    i = _j(abx, "add", "x", "--body", "b")["id"]
    abx("edit", i, "--runnable", "off")
    cmd = f"ab edit {i} --auto"
    _, out, _ = abx("ls", "--stuck")
    assert "clear with: " + cmd in out
    _, out, _ = abx("show", i)
    assert "clear with: " + cmd in out
    assert abx("edit", i, "--runnable", "on")[0] == 0
    _, out, _ = abx("show", i)
    assert "runnable=True" in out and "hold=" not in out and "clear with" not in out


def test_manual_hint_and_auto_clears(abx):
    m = _j(abx, "add", "m", "--manual")["id"]
    _, out, _ = abx("show", m)
    assert f"clear with: ab edit {m} --auto" in out
    abx("edit", m, "--auto")
    assert _j(abx, "show", m)["runnable"] == 1


def test_add_help_names_hold_and_release(abx):
    _, out, _ = abx("add", "--help")
    assert "--manual" in out and "ab edit ID --auto" in out


def test_add_manual_in_run_needs_releaser(abx, monkeypatch):
    pass
    code, out, err = abx("add", "orphan", "--manual", "--body", "wait", env={"AB_TASK": "t_x"})
    assert code != 0 and "releasing actor" in (out + err)
    assert abx("add", "ok", "--manual", "-l", "owner=bob", env={"AB_TASK": "t_x"})[0] == 0
