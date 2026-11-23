"""Tests for versioned schemas, artifact attachments, compact summaries, and non-mutating reactions."""
import json
from pathlib import Path

from agentboard import runner, store


def _j(abx, *args, rc=0):
    extra = () if "--as" in args else ("--as", "tester")
    code, out, err = abx("--json", *args, *extra)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


def test_schemas_and_result_schema_enforcement(abx):
    spec = json.dumps({
        "type": "object",
        "required": ["verdict", "score"],
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
        },
    })
    added = _j(abx, "schemas", "add", "eval@1", "--spec", spec, "--description", "eval verdict", "--why", "test")
    assert added["ref"] == "eval@1"

    listed = _j(abx, "schemas", "ls")
    assert any(x["ref"] == "eval@1" for x in listed)

    shown = _j(abx, "schemas", "show", "eval")
    assert shown["ref"] == "eval@1" and shown["spec"]["required"] == ["verdict", "score"]

    tid = _j(abx, "add", "run eval", "--result-schema", "eval@1", "--why", "test")["id"]
    _j(abx, "claim", tid)

    # Missing --data fails
    code, out, err = abx("done", tid, "PASS: ok", "--as", "tester")
    assert code != 0 and "requires --data" in (out + err)

    # Invalid enum fails
    code, out, err = abx("done", tid, "PASS: ok", "--data", '{"verdict":"MAYBE","score":0.9}', "--as", "tester")
    assert code != 0 and "not in enum" in (out + err)

    # Valid payload succeeds and stamps _schema
    _j(abx, "done", tid, "PASS: score=0.92", "--data", '{"verdict":"PASS","score":0.92}')
    card = _j(abx, "show", tid)
    assert card["status"] == "done"
    assert card["data"] == {"_schema": "eval@1", "verdict": "PASS", "score": 0.92}


def test_artifacts_snapshot_and_brief_propagation(abx, tmp_path):
    f = tmp_path / "metrics.json"
    f.write_text('{"acc": 0.91}\n')

    t1 = _j(abx, "add", "produce metrics", "--why", "test")["id"]
    _j(abx, "claim", t1)
    done = _j(abx, "done", t1, "metrics ready", "--attach", str(f))
    assert len(done["artifacts"]) == 1
    art = done["artifacts"][0]
    assert art["name"] == "metrics.json"
    assert art["stored_path"] and Path(art["stored_path"]).is_file()

    # Delete original file: immutable snapshot in AB_HOME/artifacts survives
    f.unlink()
    assert Path(art["stored_path"]).read_text() == '{"acc": 0.91}\n'

    arts = _j(abx, "artifacts", t1)
    assert len(arts) == 1 and arts[0]["sha256"] == art["sha256"]

    # Dependent task brief includes the dependency's artifact path and sha prefix
    t2 = _j(abx, "add", "review metrics", "--dep", t1, "--why", "test")["id"]
    c = store.connect(abx.home / store.DB_PATH.name)
    try:
        row = store.must_task(c, t2)
        brief = runner.build_brief(c, row, str(tmp_path), 1, "agent#1")
        assert "metrics.json" in brief and art["sha256"][:8] in brief
    finally:
        c.close()


def test_reactions_are_non_mutating_and_no_message_event(abx):
    tid = _j(abx, "add", "card for reactions", "--why", "test")["id"]
    before = _j(abx, "show", tid)
    c = store.connect(abx.home / store.DB_PATH.name)
    try:
        ev_before = c.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='message'", (tid,)).fetchone()[0]
    finally:
        c.close()

    res = _j(abx, "react", tid, "ack", "--as", "watcher")
    assert res["reactions"] == {"ack": ["watcher"]}
    _j(abx, "react", tid, "verified", "--as", "reviewer")

    after = _j(abx, "show", tid)
    assert after["updated_at"] == before["updated_at"]
    assert after["notes"] == []
    assert after["reactions"] == {"ack": ["watcher"], "verified": ["reviewer"]}

    c = store.connect(abx.home / store.DB_PATH.name)
    try:
        ev_after = c.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='message'", (tid,)).fetchone()[0]
    finally:
        c.close()
    assert ev_after == ev_before

    # Remove reaction
    rem = _j(abx, "react", tid, "ack", "--rm", "--as", "watcher")
    assert "ack" not in rem["reactions"]


def test_read_summary_and_show_brief(abx):
    th = _j(abx, "post", "dev", "root line of discussion")["thread"]
    _j(abx, "reply", th, "first follow-up")
    _j(abx, "reply", th, "latest update here")
    _j(abx, "react", th.replace("m", "m"), "seen", "--as", "overseer")

    sums = _j(abx, "read", "dev", "--summary")
    assert len(sums) == 1
    s = sums[0]
    assert s["thread"] == th and s["messages"] == 3 and s["replies"] == 2
    assert "root line" in s["root_preview"] and "latest update" in s["last_preview"]
    assert s["reactions"] == {"seen": ["overseer"]}

    long_body = "x" * 1000
    tid = _j(abx, "add", "long card", "--body", long_body, "--why", "test")["id"]
    for i in range(6):
        _j(abx, "note", tid, f"note #{i}")
    brief = _j(abx, "show", tid, "--brief")
    assert brief["body_truncated"] is True and len(brief["body"]) < 500
    assert brief["notes_total"] == 6 and len(brief["notes"]) == 3
    assert brief["notes"][-1]["body"] == "note #5"
