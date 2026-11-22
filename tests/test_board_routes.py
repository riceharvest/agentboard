"""Per-board route allowlist: `ab boards --routes` restricts which harness/provider/model
route a card on that board may launch on, tier fallback included. Throwaway AB_HOME."""
import json
import types

from agentboard import harness as H, runner, store


def _cand(harness, model, effort=None, provider=None):
    return H.Candidate(harness=harness, model=model, effort=effort, provider=provider)


def _board(abx, routes, tier=None):
    args = ["--json", "boards", "create", "jde", "--routes", routes, "--as", "tester", "--why", "test"]
    if tier:
        args += ["--tier", tier]
    code, out, err = abx(*args)
    assert code == 0, err
    return json.loads(out) if out.strip() else None


def _routes_of(abx):
    code, out, err = abx("--json", "boards", "ls")
    assert code == 0, err
    rows = json.loads(out)
    return next(r["routes"] for r in rows if r["name"] == "jde")


def test_create_set_and_clear_routes(abx):
    code, _, err = abx("--json", "boards", "create", "jde", "--routes", "hermes/*:deepseek*", "--as", "tester", "--why", "t")
    assert code == 0, err
    assert _routes_of(abx) == ["hermes/*:deepseek*"]
    code, _, err = abx("--json", "boards", "set", "jde", "--routes", "claude:sonnet", "--as", "tester", "--why", "t")
    assert code == 0, err
    assert _routes_of(abx) == ["claude:sonnet"]
    code, _, err = abx("--json", "boards", "set", "jde", "--routes", "", "--as", "tester", "--why", "t")
    assert code == 0, err
    assert _routes_of(abx) == []


def test_route_forms_match_provider_and_endpoint_spellings():
    c = _cand("hermes", "deepseek-v4.1-flash", provider="opencode-go")
    forms = store.route_forms(c)
    assert "hermes/opencode-go:deepseek-v4.1-flash" in forms
    assert "hermes:deepseek-v4.1-flash" in forms
    e = _cand("hermes", "tiny-model", None, None)
    e.endpoint = "localbox"
    assert "hermes@localbox:tiny-model" in store.route_forms(e)


def test_pick_skips_a_disallowed_route_and_keeps_the_allowed_one(abx, monkeypatch):
    """A board route allowlist bites per candidate, so tier fallback cannot carry a card
    onto a route the board did not name. Adapter availability is stubbed: the gate under
    test is the route check, not the harness install."""
    _board(abx, "hermes/*:deepseek*")
    c = store.connect(abx.home / store.DB_PATH.name)
    code, out, err = abx("--json", "add", "card", "--body", "x", "--board", "jde", "--as", "tester", "--why", "t")
    assert code == 0, err
    tid = json.loads(out)["id"]
    order = [_cand("claude", "sonnet", "high"), _cand("hermes", "deepseek-v4.1-flash", None, "opencode-go")]
    monkeypatch.setattr(runner.H, "candidates_for", lambda conn, task: (order, []))
    monkeypatch.setattr(runner.H, "ADAPTERS", {h: types.SimpleNamespace(available=lambda: True) for h in ("claude", "hermes")})
    r = runner.Runner(c, log=types.SimpleNamespace(write=lambda s: None, flush=lambda: None))
    task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    cand, bucket, why = r.pick(task, {})
    assert cand is not None and (cand.harness, cand.provider) == ("hermes", "opencode-go"), why

    # allowlist narrowed to the other route: nothing allowed, and the reason says why
    code, _, err = abx("--json", "boards", "set", "jde", "--routes", "claude:sonnet", "--as", "tester", "--why", "t")
    assert code == 0, err
    monkeypatch.setattr(runner.H, "candidates_for",
                        lambda conn, task: ([_cand("hermes", "deepseek-v4.1-flash", None, "opencode-go")], []))
    cand, _, why = r.pick(task, {})
    assert cand is None and "allows only claude:sonnet" in why, why


def test_board_without_routes_is_unrestricted(abx, monkeypatch):
    _board(abx, "")
    c = store.connect(abx.home / store.DB_PATH.name)
    cand = _cand("claude", "sonnet", "high")
    ok, why = store.board_route_ok(c, "jde", cand)
    assert ok and why is None
