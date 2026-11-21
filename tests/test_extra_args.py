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


def test_claude_settings_merge_into_one(tmp_path):
    """base + tier both pass --settings: claude keeps only the last, so they must reach it as ONE merged JSON."""
    import json
    conn = _conn(tmp_path)
    base = {"attribution": {"commit": ""}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "base-hook"}]}]}}
    tier = {"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "tier-hook"}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "stop-hook"}]}]}, "env": {"X": "1"}}
    store.cfg_set(conn, "extra_args", {"claude": ["--settings", json.dumps(base), "--flag"]})
    store.cfg_set(conn, "extra_args_tier", {"medium": {"claude": ["--settings", json.dumps(tier), "--m"]}})
    c = next(c for c in _cands(conn, "medium")[0] if c.harness == "claude")
    cmd, _ = _cmd(conn, c)
    assert cmd.count("--settings") == 1
    merged = json.loads(cmd[cmd.index("--settings") + 1])
    cmds = [h["hooks"][0]["command"] for h in merged["hooks"]["PreToolUse"]]
    assert cmds == ["base-hook", "tier-hook"]
    assert merged["hooks"]["Stop"] and merged["attribution"] == {"commit": ""} and merged["env"] == {"X": "1"}
    assert "--flag" in cmd and "--m" in cmd


def test_single_settings_untouched(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--settings", "{ \"a\": 1 }"]})
    c = next(c for c in _cands(conn, "medium")[0] if c.harness == "claude")
    assert H.extra_args_for(conn, c) == ["--settings", "{ \"a\": 1 }"]


def _claude(conn, tier="medium"):
    return next(c for c in _cands(conn, tier)[0] if c.harness == "claude")


def test_collision_settings_merge_one_flag(tmp_path):
    import json
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--settings", json.dumps({"base_only": 1, "both": "b"})]})
    store.cfg_set(conn, "extra_args_tier", {"medium": {"claude": ["--settings", json.dumps({"both": "t", "tier_only": 2})]}})
    cmd, _ = _cmd(conn, _claude(conn))
    assert cmd.count("--settings") == 1
    d = json.loads(cmd[cmd.index("--settings") + 1])
    assert d == {"base_only": 1, "both": "t", "tier_only": 2}
    assert H.extra_args_resolved(conn, _claude(conn))[1] == []


def test_collision_boolean_flag_keeps_base_and_warns(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--lean"]})
    store.cfg_set(conn, "extra_args_tier", {"medium": {"claude": ["--lean", "--m"]}})
    args, warns = H.extra_args_resolved(conn, _claude(conn))
    assert args == ["--lean", "--m"] and args.count("--lean") == 1
    assert len(warns) == 1 and all(x in warns[0] for x in ("claude", "medium", "--lean"))
    store.cfg_set(conn, "extra_args", {"claude": ["--k", "base"]})
    store.cfg_set(conn, "extra_args_tier", {"medium": {"claude": ["--k", "tier"]}})
    args, warns = H.extra_args_resolved(conn, _claude(conn))
    assert args == ["--k", "base"] and "'base'" in warns[0] and "'tier'" in warns[0]


def test_no_collision_unchanged_no_warning(tmp_path):
    conn = _conn(tmp_path)
    store.cfg_set(conn, "extra_args", {"claude": ["--g", "1"]})
    store.cfg_set(conn, "extra_args_tier", {"medium": {"claude": ["--m"]}})
    args, warns = H.extra_args_resolved(conn, _claude(conn))
    assert args == ["--g", "1", "--m"] and warns == []


def test_config_show_warnings_section_and_set_time(abx):
    abx("init")
    abx("config", "set", "extra_args", '{"claude": ["--lean"]}', env={"AB_ACTOR": "admin"})
    _, out, _ = abx("config", "show")
    assert "warnings" not in out
    rc, out, err = abx("config", "set", "extra_args_tier", '{"low": {"claude": ["--lean"]}}')
    print(rc, out, err)
    assert "warning" in out and "--lean" in out
    _, out, _ = abx("config", "show")
    assert "warnings = " in out and "--lean" in out
    abx("config", "set", "extra_args_tier", '{"low": {"claude": ["--other"]}}')
    _, out, _ = abx("config", "show")
    assert "warnings" not in out
