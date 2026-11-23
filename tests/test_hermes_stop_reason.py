"""Hermes client stops a turn itself (e.g. "Response Stopped — Repetition Detected") and exits non-zero with
nothing on stderr but the session-id bookkeeping line: the run detail must name the stop banner, not the session id.
Also: gateway outcomes are an `infra` failure class in `ab outcomes`, not `other`."""
from agentboard import harness as H
from agentboard import outcomes

REPETITION_OUT = """doing some work
│ +run the suite
⚠️ **Response Stopped — Repetition Detected**

The model fell into a repetition loop while writing this response, so the stream was stopped instead of running on. The partial response was discarded.

→ Switch to a different model with `/model`
"""
SESSION_ERR = "\nsession_id: local-353ad0c629a09775837928b6af1f8c692e44315239aeaeb6d88cb3863ad4c5eb\n"


def test_stop_banner_becomes_the_failure_cause():
    r = H.ADAPTERS["hermes"].parse(1, REPETITION_OUT, SESSION_ERR)
    assert not r.ok
    assert r.error.startswith("⚠️ **Response Stopped — Repetition Detected**"), r.error
    assert "repetition loop" in r.error, r.error
    assert "session_id:" not in r.error, r.error
    assert "local-353ad0c6" in r.error, r.error          # the session is still traceable from the detail
    assert r.session_id == "local-353ad0c629a09775837928b6af1f8c692e44315239aeaeb6d88cb3863ad4c5eb"
    assert r.error_kind == "failed"


def test_session_id_line_alone_is_never_the_cause():
    r = H.ADAPTERS["hermes"].parse(1, "", "\nSession: local-abc\n")
    assert r.error == "exit 1", r.error


def test_real_stderr_message_is_kept():
    r = H.ADAPTERS["hermes"].parse(1, "", "Error: provider rejected the request\nsession_id: local-abc98765\n")
    assert "provider rejected the request" in r.error and "session_id:" not in r.error, r.error


def test_gateway_outcomes_are_infra_not_other():
    for oc in ("gateway_lost", "gateway_down", "interrupted"):
        assert outcomes._OUT_CLASS[oc] == "infra"
    assert "infra" in outcomes.CLASSES
