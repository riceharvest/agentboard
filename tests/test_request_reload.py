"""`ab request-reload`: a worker asks for a runner reload that happens once its card is done."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import store, runner as R  # noqa: E402


def test_request_reload_cli_and_runner_fires_after_done(abx):
    rc, o, e = abx("add", "x")
    tid = o.strip()
    rc, o, e = abx("request-reload", tid)
    assert rc == 0, e
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name), isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        r = R.Runner.__new__(R.Runner)
        r.conn, r.log = c, lambda m: None
        assert store.cfg(c, "reload_after") == [tid]
        r.check_reload_requests()  # card still open: nothing happens
        assert not (store.cfg(c, "stop") or "")
        with store.tx(c):
            c.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        r.check_reload_requests()
        assert store.cfg(c, "stop") == "reload" and store.cfg(c, "reload_after") == []
    finally:
        c.close()
