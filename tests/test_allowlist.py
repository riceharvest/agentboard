"""Cache-stable brief (fixed block first) and the opt-in claude load-trimming labels."""
from agentboard import harness as H
from agentboard import runner


def test_labels_to_args():
    assert H.allowlist_args({}, "claude") == []
    assert H.allowlist_args({"tools": "Bash,Read"}, "hermes") == []
    assert H.allowlist_args({"tools": "Bash+Read Edit", "skills": "none", "mcp": "none"}, "claude") == [
        "--tools", "Bash,Read,Edit", "--disable-slash-commands", "--strict-mcp-config"]
    assert H.allowlist_args({"skills": "x"}, "claude") == []


def test_brief_fixed_prefix():
    a = runner.BRIEF.format(id="t_1", title="A", channel="main", tier="low", actor="claude#1", board="b", context="", park="",
                            attempt=1, max_attempts=3, labels="", resume="", workdir="/x", body="one", deps="", notes="")
    b = runner.BRIEF.format(id="t_2", title="B", channel="x", tier="max", actor="codex#9", board="c", context="", park="",
                            attempt=2, max_attempts=3, labels=" labels=k=v", resume="r", workdir="/y", body="two", deps="d", notes="n")
    assert a.startswith(runner.BRIEF_FIXED) and b.startswith(runner.BRIEF_FIXED)
    assert "t_1" not in runner.BRIEF_FIXED and "{" not in runner.BRIEF_FIXED
