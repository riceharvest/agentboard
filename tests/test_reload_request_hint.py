"""An unprivileged `runner reload` refusal names `ab request-reload`; the request queues once and is idempotent."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HINT = "reload needs elevated grants; queue it with: ab request-reload [ID]  (runs once that card is done)"


def _setup(abx):
    assert abx("grants", "add", "--who", "worker#*", "--can", "read,post,create,claim", "--grant-board", "*")[0] == 0
    env = {"AB_ACTOR": "worker#1"}
    rc, o, e = abx("--json", "add", "x", env=env)
    assert rc == 0, e
    return env, json.loads(o)["id"]


def test_unprivileged_reload_refusal_has_hint(abx):
    env, _ = _setup(abx)
    rc, o, e = abx("runner", "reload", env=env)
    assert rc != 0
    assert HINT in e


def test_request_reload_queues_once_and_is_idempotent(abx):
    env, tid = _setup(abx)
    for _ in range(2):
        rc, o, e = abx("--json", "request-reload", tid, env=env)
        assert rc == 0, e
        d = json.loads(o)
        assert d["id"] == tid and d["reload_after"] == [tid]
