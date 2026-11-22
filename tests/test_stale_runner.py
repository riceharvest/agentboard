"""A runner records the code version it started with; `ab runner status` / `ab add` warn when the installed code differs."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import store  # noqa: E402

MSG = "runner code older than installed code"


def _set_runner(abx, ver):
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name))
    c.execute("INSERT OR REPLACE INTO runner(id,pid,host,started_at,heartbeat,state,active,slots,note,code_ver) "
              "VALUES(1,?,?,?,?,?,?,?,?,?)", (os.getpid(), "h", 1.0, __import__("time").time(), "idle", 0, 1, None, ver))
    c.commit()
    c.close()


def test_stale_warning_on_status_and_add(abx):
    abx("ls")  # create the board
    _set_runner(abx, "oldversion")
    rc, o, e = abx("runner", "status")
    assert MSG in o
    rc, o, e = abx("add", "x")
    assert rc == 0 and o.strip().startswith("t_") and MSG in e


def test_no_warning_when_versions_match(abx):
    abx("ls")
    _set_runner(abx, store.code_version())
    assert MSG not in abx("runner", "status")[1]
    assert MSG not in abx("add", "x")[2]


def test_no_warning_without_runner_or_without_recorded_version(abx):
    assert MSG not in abx("add", "x")[2]
    _set_runner(abx, None)  # runner from before this column existed
    assert MSG not in abx("runner", "status")[1]


def test_code_version_changes_when_code_is_touched(tmp_path, monkeypatch):
    v1 = store.code_version()
    f = os.path.join(os.path.dirname(store.__file__), "store.py")
    st = os.stat(f)
    try:
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        assert store.code_version() != v1
    finally:
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert store.code_version() == v1


def test_stale_hint_names_both_remedies():
    assert "older than installed code" in store.STALE_MSG
    assert "ab runner reload" in store.STALE_MSG and "ab request-reload" in store.STALE_MSG
