"""Tests for negative-outcome marking (`ab fail --negative` / `outcome=negative`) and anomaly scan skipping."""
import json


def _j(abx, *args, rc=0):
    code, out, err = abx("--json", *args)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


def test_negative_marked_dead_card_skipped_by_anomalies(abx):
    """Normal dead card appears in `ab anomalies`; `ab fail --negative` card does not."""
    normal = _j(abx, "add", "flaky build", "--max-attempts", "1")["id"]
    _j(abx, "claim", normal)
    res = _j(abx, "fail", normal, "timed out")
    assert res["status"] == "dead"

    neg = _j(abx, "add", "kill-gate experiment", "--max-attempts", "3")["id"]
    _j(abx, "claim", neg)
    res_neg = _j(abx, "fail", neg, "--negative", "kill gate failed: delta -0.2%")
    assert res_neg["status"] == "dead"
    assert res_neg["negative"] is True

    shown = _j(abx, "show", neg)
    assert shown["status"] == "dead"
    assert shown["labels"].get("outcome") == "negative"
    assert "kill gate failed" in (shown["result"] or "")

    anoms = _j(abx, "anomalies")
    dead_ids = [a["task_id"] for a in anoms if a["kind"] == "dead"]
    assert normal in dead_ids
    assert neg not in dead_ids


def test_retroactive_negative_mark_works_without_manage_grant(abx):
    """Any actor with write (`post`/`claim`, no `manage`) can mark an already-dead card negative."""
    _j(abx, "boards", "create", "proj")
    # Grant worker-like permissions (read, post, create, claim) without manage
    _j(abx, "grants", "add", "--grant-board", "proj", "--who", "overseer#*", "--can", "read,post,create,claim")
    # Grant read-only actor
    _j(abx, "grants", "add", "--grant-board", "proj", "--who", "viewer", "--can", "read")

    t1 = _j(abx, "-B", "proj", "add", "recorded negative 1", "--max-attempts", "1")["id"]
    _j(abx, "-B", "proj", "claim", t1)
    _j(abx, "-B", "proj", "fail", t1, "failed kill gate")

    t2 = _j(abx, "-B", "proj", "add", "recorded negative 2", "--max-attempts", "1")["id"]
    _j(abx, "-B", "proj", "claim", t2)
    _j(abx, "-B", "proj", "fail", t2, "failed kill gate 2")

    before = [a["task_id"] for a in _j(abx, "anomalies", "--all-boards") if a["kind"] == "dead"]
    assert t1 in before and t2 in before

    # Read-only actor is refused
    code, _, err = abx("--as", "viewer", "-B", "proj", "fail", t1, "--negative", "nope")
    assert code != 0 and "no write access" in err

    # Non-manage actor with write can mark via `ab fail --negative`
    _j(abx, "--as", "overseer#1", "-B", "proj", "fail", t1, "--negative", "intended negative")
    # Non-manage actor with write can also mark via `ab edit -l outcome=negative`
    _j(abx, "--as", "overseer#1", "-B", "proj", "edit", t2, "-l", "outcome=negative")
    # Existing RESULT is preserved on retroactive mark
    assert "failed kill gate" in (_j(abx, "show", t1)["result"] or "")

    after = [a["task_id"] for a in _j(abx, "anomalies", "--all-boards") if a["kind"] == "dead"]
    assert t1 not in after and t2 not in after

    # Retrying clears outcome=negative so if it later dies normally it shows up again
    _j(abx, "retry", t1)
    assert "outcome" not in (_j(abx, "show", t1)["labels"] or {})


def test_dead_watcher_skips_negative_outcome(abx):
    """A `--on dead` watcher does not fire for `ab fail --negative`, but still fires for normal `dead`."""
    from agentboard import automation, store

    _j(abx, "watch", "add", "on-dead", "--on", "dead", "--brief", "investigate dead card")

    neg = _j(abx, "add", "negative run", "--max-attempts", "1")["id"]
    _j(abx, "claim", neg)
    _j(abx, "fail", neg, "--negative", "kill gate missed")

    conn = store.connect(abx.home / store.DB_PATH.name)
    try:
        fired = automation.run_watchers(conn)
        assert fired == []

        norm = _j(abx, "add", "real crash", "--max-attempts", "1")["id"]
        _j(abx, "claim", norm)
        _j(abx, "fail", norm, "oom crash")

        fired2 = automation.run_watchers(conn)
        assert len(fired2) == 1 and fired2[0][0] == "on-dead"
    finally:
        conn.close()
