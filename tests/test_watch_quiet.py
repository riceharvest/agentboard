"""A task labelled watch_quiet=done does not wake `--on done` watchers; its failures still do."""
import json


def _j(abx, *args, rc=0):
    code, out, err = abx("--json", *args)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


def test_watch_quiet_done_skips_done_but_not_dead(abx):
    from agentboard import automation, store

    _j(abx, "watch", "add", "ovs", "--on", "done,dead", "--brief", "look")
    quiet = _j(abx, "add", "routine cycle", "-l", "watch_quiet=done", "--max-attempts", "1")["id"]
    _j(abx, "claim", quiet)
    _j(abx, "done", quiet, "0 new rows")

    conn = store.connect(abx.home / store.DB_PATH.name)
    try:
        assert automation.run_watchers(conn) == []
        loud = _j(abx, "add", "routine cycle 2", "-l", "watch_quiet=done", "--max-attempts", "1")["id"]
        _j(abx, "claim", loud)
        _j(abx, "fail", loud, "collector crashed")
        fired = automation.run_watchers(conn)
        assert len(fired) == 1 and fired[0][0] == "ovs"
    finally:
        conn.close()
