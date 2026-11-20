"""Per-harness and per-tier extra CLI args (extra_args, extra_args_tier). Throwaway DB, no harness is run."""
from agentboard import harness as H, store


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _cands(conn, tier, **kw):
    t = {"harness": None, "model": None, "tier": tier, **kw}
    p, e = H.candidates_for(conn, t)
    return p, e


def _cmd(conn, cand):
    return H.ADAPTERS[cand.harness].build("BRIEF", cand, None, H.extra_args_for(conn, cand))


def test_default_config_is_byte_identical(tmp_path):
    conn = _conn(tmp_path)
    assert store.DEFAULTS["extra_args"] == {} and store.DEFAULTS["extra_args_tier"] == {}
    for tier in ("low", "medium", "high", "max"):
        p, e = _cands(conn, tier)
        for c in p + e:
            assert H.extra_args_for(conn, c) == []
            cmd, env = _cmd(conn, c)
            assert cmd == H.ADAPTERS[c.harness].build("BRIEF", c, None)[0]
    c = H.parse_spec("claude:sonnet@high")
    assert _cmd(conn, c)[0] == ["claude", "-p", "BRIEF", "--output-format", "json", "--dangerously-skip-permissions",
                                "--model", "sonnet", "--effort", "high"]


def test_harness_wide_args_only_hit_that_harness(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--a", "1"]})
    p, _ = _cands(conn, "medium")
    for c in p:
        want = ["--a", "1"] if c.harness == "claude" else []
        assert H.extra_args_for(conn, c) == want
    cmd, _ = _cmd(conn, next(c for c in p if c.harness == "claude"))
    assert cmd[-2:] == ["--a", "1"]


def test_tier_args_follow_harness_args_and_only_that_tier(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--g"]})
    store.cfg_set(conn, "extra_args_tier", {"low": {"claude": ["--lean"]}, "medium": {"claude": ["--m"], "codex": ["--cx"]}})
    by = {}
    for tier in ("low", "medium", "high", "max"):
        p, _ = _cands(conn, tier)
        by[tier] = {c.harness: H.extra_args_for(conn, c) for c in p}
    assert by["low"]["claude"] == ["--g", "--lean"]
    assert by["medium"]["claude"] == ["--g", "--m"] and by["medium"]["codex"] == ["--cx"]
    assert by["high"]["claude"] == ["--g"] and by["max"]["claude"] == ["--g"]
    assert "codex" not in by["low"] or by["low"]["codex"] == []


def test_fallback_entry_uses_its_own_tier(tmp_path):
    """A low card that falls back up to the max tier must not get low's lean flags on max's claude entry."""
    conn = _conn(tmp_path)
    store.cfg_set(conn, "tier_fallback", "up")
    store.cfg_set(conn, "extra_args_tier", {"low": {"claude": ["--lean"]}})
    p, e = _cands(conn, "low")
    assert [H.extra_args_for(conn, c) for c in p if c.harness == "claude"] == [["--lean"]]
    assert [c.tier for c in e if c.harness == "claude"] and all(H.extra_args_for(conn, c) == [] for c in e if c.harness == "claude")


def test_pinned_task_gets_harness_args_not_tier_args(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--g"]})
    store.cfg_set(conn, "extra_args_tier", {"low": {"claude": ["--lean"]}})
    p, _ = H.candidates_for(conn, {"harness": "claude", "model": "haiku", "tier": "low"})
    assert H.extra_args_for(conn, p[0]) == ["--g"]
