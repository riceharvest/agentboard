"""`ab ls --ready` footer/JSON buckets: why an empty (or filtered) ready population is empty."""
import json
import sqlite3
import time


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def _fixture(abx):
    """6 cards: ready, parked (payload), not_before in the future, open dep, runnable=0 hold=manual,
    runnable=0 with no hold reason. The dep card depends on the future card, so no extra ready card."""
    ids = {}
    ids["ready"] = _add(abx, "ready card", "--tier", "low")
    ids["parked"] = _add(abx, "parked card", "--tier", "low")
    ids["future"] = _add(abx, "future card", "--tier", "low", "--after", "+2h")
    ids["depopen"] = _add(abx, "dep card", "--tier", "low", "--dep", ids["future"])
    ids["held"] = _add(abx, "held card", "--manual", "-l", "hold=manual", "--tier", "low")
    ids["plain"] = _add(abx, "plain hold card", "--tier", "low")
    db = json.loads(abx("--json", "snapshot")[1])["db"]
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET park=? WHERE id=?",
                (json.dumps({"pid": 999999, "note": "job", "at": time.time()}), ids["parked"]))
    con.execute("UPDATE tasks SET runnable=0, labels='{}' WHERE id=?", (ids["plain"],))
    con.commit()
    con.close()
    return ids, db


def _close(db, tid):
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    con.commit()
    con.close()


def test_ready_rows_have_no_footer(abx):
    ids, _ = _fixture(abx)
    code, o, e = abx("ls", "--ready")
    assert code == 0, e
    # byte-for-byte: one card, no summary line, and the runnable=0 cards are not "ready" rows.
    assert o == f"{ids['ready']}  ready     p0   low    main         ready card\n", o
    code, o, e = abx("--json", "ls", "--ready")
    payload = json.loads(o)
    assert [d["id"] for d in payload["items"]] == [ids["ready"]]
    assert payload["buckets"] == {"ready": 1, "hold": 2, "dep_open": 1, "park": 1, "not_before": 1}


def test_empty_ready_prints_bucket_footer(abx):
    ids, db = _fixture(abx)
    _close(db, ids["ready"])
    code, o, e = abx("ls", "--ready")
    assert code == 0, e
    assert o.strip().splitlines() == ["(nothing)",
                                      "ready: 0 (dep_open 1, not_before 1, parked 1, hold 2)"], o
    payload = json.loads(abx("--json", "ls", "--ready")[1])
    assert payload["items"] == []
    assert payload["buckets"] == {"ready": 0, "hold": 2, "dep_open": 1, "park": 1, "not_before": 1}


def test_footer_respects_board_label_and_limit(abx):
    ids, db = _fixture(abx)
    _close(db, ids["ready"])
    code, o, e = abx("ls", "--ready", "--board", "main")
    assert o.strip().splitlines()[-1] == "ready: 0 (dep_open 1, not_before 1, parked 1, hold 2; board filter: main)", o
    code, o, e = abx("ls", "--ready", "--board", "elsewhere")
    assert o.strip().splitlines()[-1] == "ready: 0 (dep_open 0, not_before 0, parked 0, hold 0; board filter: elsewhere)", o
    code, o, e = abx("ls", "--ready", "--label", "hold=manual")
    assert o.strip().splitlines()[-1] == "ready: 0 (dep_open 0, not_before 0, parked 0, hold 1)", o
    # --limit describes the first N candidates in the same order ready_tasks uses (priority, then oldest).
    code, o, e = abx("ls", "--ready", "--limit", "2")
    assert o.strip().splitlines()[-1] == "ready: 0 (dep_open 0, not_before 1, parked 1, hold 0)", o
