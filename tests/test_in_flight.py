"""`ab ls --in-flight` and the snapshot in_flight / in_flight_reason fields, on a 6-card fixture."""
import json
import sqlite3
import time

OLD_KEYS = ["id", "board", "title", "channel", "status", "state", "runnable", "priority", "tier", "labels", "park",
            "not_before", "deps", "deps_open", "kind"]


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def _fixture(abx):
    ids = {}
    ids["ready"] = _add(abx, "ready", "--tier", "low")
    ids["parked"] = _add(abx, "parked", "--tier", "low")
    ids["future"] = _add(abx, "future", "--tier", "low", "--after", "+2h")
    blocker = _add(abx, "blocker", "--tier", "high")
    ids["blocker"] = blocker
    ids["depopen"] = _add(abx, "depopen", "--tier", "low", "--dep", blocker)
    done_dep = _add(abx, "donedep", "--tier", "high")
    ids["depdone_parked"] = _add(abx, "depdone_parked", "--tier", "medium", "--dep", done_dep)
    ids["claimed"] = _add(abx, "claimed", "--tier", "medium")
    code, o, e = abx("--json", "snapshot")
    db = json.loads(o)["db"]
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET status='done' WHERE id=?", (done_dep,))
    for k in ("parked", "depdone_parked"):
        con.execute("UPDATE tasks SET park=? WHERE id=?",
                    (json.dumps({"pid": 999999, "note": "job", "at": time.time()}), ids[k]))
    con.execute("UPDATE tasks SET status='claimed' WHERE id=?", (ids["claimed"],))
    con.commit()
    con.close()
    return ids


def test_in_flight_list_and_counts(abx):
    ids = _fixture(abx)
    code, o, e = abx("ls", "--in-flight", "--tier", "low,medium", "--limit", "0")
    assert code == 0, e
    lines = o.strip().splitlines()
    assert lines[-1] == "in-flight: 2 (parked 2, deferred 1, dep-waiting 1)"
    body = "\n".join(lines[:-1])
    assert ids["ready"] in body and ids["claimed"] in body
    for k in ("parked", "future", "depopen", "depdone_parked", "blocker"):
        assert ids[k] not in body
    code, o, e = abx("--json", "ls", "--in-flight", "--tier", "low")
    assert [d["id"] for d in json.loads(o)] == [ids["ready"]]


def test_snapshot_reasons_and_old_keys(abx):
    ids = _fixture(abx)
    snap = json.loads(abx("--json", "snapshot")[1])
    by = {d["id"]: d for d in snap["tasks"]}
    want = {"ready": (True, None), "parked": (False, "parked"), "future": (False, "not_before"),
            "depopen": (False, "dep_open"), "depdone_parked": (False, "parked"), "claimed": (True, None)}
    for k, (fl, why) in want.items():
        assert (by[ids[k]]["in_flight"], by[ids[k]]["in_flight_reason"]) == (fl, why), k
    for d in by.values():
        for k in OLD_KEYS:
            assert k in d
    schema = json.load(open(__import__("agentboard.snapshot", fromlist=["x"]).SCHEMA_FILE))
    for k in schema["definitions"]["task"]["required"]:
        assert all(k in d for d in by.values()), k


def test_ls_status_and_stuck_unchanged(abx):
    ids = _fixture(abx)
    code, o, e = abx("ls", "--status", "open")
    assert code == 0 and "in-flight" not in o
    assert ids["parked"] in o and ids["future"] in o and ids["depopen"] in o
    code, o, e = abx("ls", "--stuck")
    assert code == 0 and "in-flight" not in o
