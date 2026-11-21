"""The hermes failure classifier reads provider/harness error lines only, not code or diffs echoed in the transcript."""
import sys

from conftest import ROOT

sys.path.insert(0, ROOT)
from agentboard import harness as H  # noqa: E402

CODE_OUT = ("session_id: s1\n"
            "╭─ patch ─────────╮\n"
            "│ +            if e.code in (429, 500, 502, 503, 504) and attempt < 3:\n"
            "╰─────────────────╯\n"
            "I changed the retry loop; it handles 429 and rate limit codes now.\n")


def _parse(rc, out, err):
    return H.ADAPTERS["hermes"].parse(rc, out, err)


def test_429_in_code_in_transcript_is_not_a_limit():
    boxed = CODE_OUT.rsplit("\n", 2)[0] + "\n"
    for out, err in ((CODE_OUT, ""), ("", boxed), (CODE_OUT, "+ if e.code in (429, 500):\n")):
        r = _parse(1, out, err)
        assert not r.ok and r.error_kind == "failed", (r.error_kind, r.error)


def test_real_429_provider_error_is_a_limit():
    err = "2026-11-19 16:13:01,123 WARNING [s1] agent.conversation_loop: API call failed (attempt 3): HTTP 429 Too Many Requests\n"
    r = _parse(1, "", err)
    assert r.error_kind == "limit" and "429" in r.error
    r = _parse(1, CODE_OUT, "Error: Nous 429 rate limit hit\n")
    assert r.error_kind == "limit" and "Nous 429" in r.error
    r = _parse(1, CODE_OUT + "Error: HTTP 429 too many requests\n", "")
    assert r.error_kind == "limit" and "HTTP 429" in r.error


def test_429_in_code_is_not_a_limit_for_any_adapter():
    diff = "+            if e.code in (429, 500, 502):\n"
    for name, a in H.ADAPTERS.items():
        for rc, out, err in ((1, diff, ""), (1, "", diff), (1, "I changed it\n" + diff, "boom\n")):
            r = a.parse(rc, out, err)
            assert r.error_kind != "limit", (name, rc, r.error_kind)


def test_real_429_still_a_limit_for_cli_adapters():
    r = H.ADAPTERS["codex"].parse(1, "", "Error: HTTP 429 Too Many Requests\n")
    assert r.error_kind == "limit"
