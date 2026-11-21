"""Same-family review guard: a review card labelled exclude_family=<harness:model> is not handed to that family."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import store  # noqa: E402

LAB = {"exclude_family": "claude:claude-sonnet-5-5", "reviews": "t_x"}


def test_same_family_refused():
    ok, msg = store.family_guard(LAB, "claude", "claude-sonnet-5-5")
    assert not ok and "exclude_family=claude:claude-sonnet-5-5" in msg and "--allow-same-family" in msg


def test_alias_counts_as_same_family():
    assert not store.family_guard(LAB, "claude", "sonnet")[0]


def test_different_family_allowed():
    assert store.family_guard(LAB, "claude", "claude-opus-5-5")[0]
    assert store.family_guard(LAB, "codex", "claude-sonnet-5-5")[0]
    assert store.family_guard({}, "claude", "sonnet")[0]


def test_allow_same_family_allowed_and_label_recorded(abx):
    assert store.family_guard(LAB, "claude", "claude-sonnet-5-5", allow_why="only reviewer left")[0]
    assert store.family_guard({**LAB, "same_family_ok": "x"}, "claude", "claude-sonnet-5-5")[0]
    abx("init")
    abx("whoami", env={"AB_ACTOR": "dario"})
    c = sqlite3.connect(str(abx.home / store.DB_PATH.name)); c.row_factory = sqlite3.Row
    c.execute("INSERT INTO runs(id,task_id,harness,model,started_at) VALUES(7,NULL,'claude','claude-sonnet-5-5',0)")
    c.commit(); c.close()
    rc, out, err = abx("add", "rev", "--label", "exclude_family=claude:claude-sonnet-5-5", "--json", env={"AB_ACTOR": "dario"})
    assert rc == 0, err
    import json
    tid = json.loads(out)["id"]
    rc, out, err = abx("claim", tid, "--as", "claude#7")
    assert rc == 3 and "--allow-same-family" in out + err
    rc, out, err = abx("claim", tid, "--as", "claude#7", "--allow-same-family", "only reviewer left")
    assert rc == 0, out + err
    rc, out, err = abx("show", tid, "--json")
    assert json.loads(out)["labels"]["same_family_ok"] == "only reviewer left"
