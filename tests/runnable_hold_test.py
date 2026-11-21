"""runnable=0 always says why (labels.hold); `ab ls --stuck` lists non-deliberate holds. Throwaway AB_HOME."""
import json



def _j(abx, *args, rc=0):
    code, out, err = abx("--json", *args)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None



def test_hold_paths(abx):
    # manual
    m = _j(abx, "add", "manual card", "--manual")["id"]
    assert _j(abx, "show", m)["labels"]["hold"] == "manual"
    code, out, _ = abx("show", m)
    assert "runnable=False (hold=manual)" in out
    # edit --manual on a normal card
    e = _j(abx, "add", "edit card")["id"]
    abx("edit", e, "--manual")
    assert _j(abx, "show", e)["labels"]["hold"] == "manual"
    abx("edit", e, "--auto")
    d = _j(abx, "show", e)
    assert d["runnable"] == 1 and "hold" not in d["labels"]
    # retry_backoff via a failed attempt with backoff
    _j(abx, "config", "set", "backoff", "1h")
    r = _j(abx, "add", "backoff card")["id"]
    _j(abx, "claim", r)
    _j(abx, "fail", r, "tests failed")
    d = _j(abx, "show", r)
    assert d["runnable"] == 0 and d["labels"]["hold"] == "retry_backoff"
    assert "(hold=retry_backoff)" in abx("show", r)[1]
    # needs_admin via grant refusal
    g = _j(abx, "add", "grant card")["id"]
    _j(abx, "claim", g)
    _j(abx, "fail", g, "x has no admin grant for this")
    assert _j(abx, "show", g)["labels"]["hold"] == "needs_admin"
    # ab ls shows the reason on ready cards; --stuck lists backoff only when ready (not_before passed)
    abx("edit", r, "--after", "")  # clear not_before: ready now, still runnable=0 + hold=retry_backoff
    ids = [t["id"] for t in _j(abx, "ls", "--stuck")]
    # heal_holds runs on ls: the expired backoff is recomputed, so nothing is stuck
    assert m not in ids and g not in ids
    assert _j(abx, "show", r)["runnable"] == 1


def test_stuck_lists_retry_backoff_not_manual(abx):
    m = _j(abx, "add", "manual card", "--manual")["id"]
    r = _j(abx, "add", "stuck card")["id"]
    # simulate a hold that outlived its cause and that nothing heals: an unexplained/odd hold value
    abx("edit", r, "--manual")
    abx("edit", r, "-l", "hold=retry_backoff")  # hold set, not_before empty
    # heal_holds would clear it; use an unknown reason which heal does not touch
    abx("edit", r, "-l", "hold=legacy")
    ids = [t["id"] for t in _j(abx, "ls", "--stuck")]
    assert r in ids and m not in ids
    out = abx("ls", "--stuck")[1]
    assert "runnable=0 (hold=legacy)" in out
    assert "ready but not runnable" in abx("decision", "ls")[1]
    abx("edit", r, "--auto")
    assert r not in [t["id"] for t in _j(abx, "ls", "--stuck")]
