import sqlite3
import sys

from conftest import ROOT

sys.path.insert(0, ROOT + "/tui/tests")
import make_fixture  # noqa: E402
from agentboard import store  # noqa: E402


DB = store.DB_PATH.name


def test_two_builds_in_one_process(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    ta = make_fixture.build(a)
    tb = make_fixture.build(b, run_pids=[1])  # second preset: different runner pid, same process
    assert a != b
    for home, t in ((a, ta), (b, tb)):
        c = sqlite3.connect(home / DB)
        assert c.execute("SELECT count(*) FROM runs").fetchone()[0] == 3
        assert c.execute("SELECT id FROM tasks WHERE id=?", (t["run1"],)).fetchone()
        assert c.execute("SELECT count(*) FROM tasks").fetchone()[0] >= 14
    pid_b = sqlite3.connect(b / DB).execute("SELECT pid FROM runner").fetchone()[0]
    assert pid_b == 1
    assert make_fixture.snapshot(b)["db"] == str(b / DB)
