"""Idempotent adds (`ab add --key`) and plan files (`ab add --plan`). Throwaway AB_HOME."""
import json
import subprocess
import sys

from conftest import ROOT, clean_env

PLAN = [{"key": "a", "title": "A"},
        {"key": "b", "title": "B", "deps": [{"key": "a"}], "labels": {"k": "v"}, "priority": 2},
        {"key": "c", "title": "C", "deps": [{"key": "a"}, {"key": "b"}], "tier": "high", "manual": True}]


def j(abx, *args, stdin=None, rc=0):
    code, out, err = abx("--json", *args, stdin=stdin)
    assert code == rc, (args, code, out, err)
    return json.loads(out) if out.strip() else None


def cards(abx):
    return j(abx, "ls", "--status", "all")


def test_same_key_twice_one_card(abx):
    a = j(abx, "add", "x", "--key", "K")
    b = j(abx, "add", "x again", "--key", "K")
    assert a["id"] == b["id"] and not a["existed"] and b["existed"]
    assert len(cards(abx)) == 1
    assert j(abx, "show", a["id"])["key"] == "K"


def test_cancelled_frees_key_and_key_live(abx):
    a = j(abx, "add", "x", "--key", "K")["id"]
    j(abx, "cancel", a)
    b = j(abx, "add", "x", "--key", "K")["id"]
    assert b != a
    code, *_ = abx("claim", b, "--as", "w")
    abx("done", b, "ok", "--as", "w")
    assert j(abx, "add", "x", "--key", "K")["id"] == b           # done still holds the key
    c = j(abx, "add", "x", "--key", "K", "--key-live")["id"]      # --key-live: only unfinished cards
    assert c != b


def test_plan_creates_chain_and_rerun_is_noop(abx):
    r = j(abx, "add", "--plan", "-", "--key", "P", stdin=json.dumps(PLAN))
    ids = r["ids"]
    assert r["created"] == 3 and not r["existed"]
    assert j(abx, "show", ids["a"])["deps"] == []
    assert j(abx, "show", ids["b"])["deps"] == [ids["a"]]
    assert sorted(j(abx, "show", ids["c"])["deps"]) == sorted([ids["a"], ids["b"]])
    assert j(abx, "show", ids["c"])["tier"] == "high" and j(abx, "show", ids["c"])["runnable"] == 0
    r2 = j(abx, "add", "--plan", "-", "--key", "P", stdin=json.dumps(PLAN))
    assert r2["ids"] == ids and r2["created"] == 0 and r2["existed"]
    assert len(cards(abx)) == 3
    # without a plan key, per-card keys still make a rerun a no-op
    r3 = j(abx, "add", "--plan", "-", stdin=json.dumps(PLAN))
    assert r3["ids"] == ids and r3["created"] == 0 and len(cards(abx)) == 3


def test_plan_dep_on_existing_key_and_id(abx):
    base = j(abx, "add", "base", "--key", "base")["id"]
    other = j(abx, "add", "other")["id"]
    plan = [{"key": "x", "title": "X", "deps": [{"key": "base"}, other]}]
    ids = j(abx, "add", "--plan", "-", stdin=json.dumps(plan))["ids"]
    assert sorted(j(abx, "show", ids["x"])["deps"]) == sorted([base, other])


def test_plan_out_of_order_deps(abx):
    plan = [{"key": "b", "title": "B", "deps": [{"key": "a"}]}, {"key": "a", "title": "A"}]
    ids = j(abx, "add", "--plan", "-", stdin=json.dumps(plan))["ids"]
    assert j(abx, "show", ids["b"])["deps"] == [ids["a"]]


def test_plan_refused_writes_nothing(abx):
    bad = [
        [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "need": ["a"]}],        # typo field
        [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "deps": [{"key": "zz"}]}],  # unknown dep key
        [{"key": "a", "title": "A", "deps": [{"key": "b"}]}, {"key": "b", "title": "B", "deps": [{"key": "a"}]}],  # cycle
        [{"key": "a", "title": "A"}, {"key": "a", "title": "A2"}],                       # duplicate key
        [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "deps": ["t_nope"]}],    # unknown id (fails mid-way)
        [{"key": "a", "title": "A"}, {"title": "B", "tier": "nosuchtier"}],              # bad tier (fails mid-way)
        [{"key": "a", "title": "A"}, {"title": ""}],
    ]
    for plan in bad:
        code, out, err = abx("add", "--plan", "-", "--key", "P", stdin=json.dumps(plan))
        assert code != 0, plan
        assert cards(abx) in ([], None), plan
    assert abx("add", "--plan", "-", stdin="not json")[0] != 0
    assert abx("add", "--plan", "-", "extra title", stdin=json.dumps(PLAN))[0] != 0
    assert cards(abx) in ([], None)


def test_plan_killed_midway_leaves_nothing(abx, tmp_path):
    code = (
        "import os, signal, sys\n"
        "from agentboard import cli, store\n"
        "real = store.add_task\n"
        "n = [0]\n"
        "def boom(*a, **k):\n"
        "    n[0] += 1\n"
        "    r = real(*a, **k)\n"
        "    if n[0] == 2:\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    return r\n"
        "store.add_task = boom\n"
        "sys.exit(cli.main(['--json', 'add', '--plan', sys.argv[1], '--key', 'P']))\n")
    (tmp_path / "plan.json").write_text(json.dumps(PLAN))
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path / "plan.json")], cwd=ROOT, env=clean_env(abx.home, AB_ACTOR="tester"),
                       capture_output=True, text=True)
    assert p.returncode == -9, (p.returncode, p.stderr)
    assert cards(abx) in ([], None)
    # and the plan key was not recorded: a rerun creates everything
    assert j(abx, "add", "--plan", str(tmp_path / "plan.json"), "--key", "P")["created"] == 3


def test_mcp_argv():
    from agentboard import mcp
    argv = mcp.to_argv("ab_add", {"plan": "[]", "key": "P"})
    assert "--plan" in argv and "--key" in argv


def test_key_live_ignores_the_callers_own_card(abx):
    """A card that re-queues itself (same key) from inside its run must get a NEW card, not its own id."""
    a = j(abx, "add", "loop", "--key", "loop", "--key-live")["id"]
    assert j(abx, "add", "loop", "--key", "loop", "--key-live")["id"] == a     # a retry from outside: no-op
    code, out, err = abx("--json", "add", "loop", "--key", "loop", "--key-live", env={"AB_TASK": a})
    assert code == 0, err
    b = json.loads(out)["id"]
    assert b != a and len(cards(abx)) == 2


def test_plan_body_id_placeholder(abx):
    plan = [{"key": "rev", "title": "R", "body": "review of {{id:work}}", "labels": {"reviews": "{{id:work}}"}},
            {"key": "work", "title": "W"}]
    ids = j(abx, "add", "--plan", "-", stdin=json.dumps(plan))["ids"]
    assert j(abx, "show", ids["rev"])["body"] == f"review of {ids['work']}"
    assert j(abx, "show", ids["rev"])["labels"]["reviews"] == ids["work"]
    bad = [{"key": "r", "title": "R", "body": "x {{id:nope}}"}]
    assert abx("add", "--plan", "-", stdin=json.dumps(bad))[0] != 0


def test_replace_same_actor_cancels_old(abx):
    old = j(abx, "add", "x", "--key", "k1", "--as", "A")["id"]
    new = j(abx, "add", "x2", "--key", "k1", "--replace", "--as", "A")
    assert new["id"] != old and not new["existed"]
    o = j(abx, "show", old)
    assert o["status"] == "cancelled" and new["id"] in (o.get("result") or "")
    assert j(abx, "show", new["id"])["key"] == "k1"


def test_replace_other_actor_refused(abx):
    old = j(abx, "add", "x", "--key", "k1", "--as", "A")["id"]
    assert j(abx, "add", "y", "--key", "k1", "--as", "B")["id"] == old      # reproduce: plain --key returns the existing id
    j(abx, "grants", "add", "B", "--can", "read,post,create,claim", "--grant-board", "*")   # worker without manage
    code, out, err = abx("--json", "add", "y", "--key", "k1", "--replace", "--as", "B")
    assert code != 0 and "another owner" in (out + err)
    assert j(abx, "show", old)["status"] == "open" and len(cards(abx)) == 1


def test_replace_same_owner_label(abx):
    old = j(abx, "add", "x", "--key", "k1", "--label", "owner=team1", "--as", "A")["id"]
    new = j(abx, "add", "x", "--key", "k1", "--label", "owner=team1", "--replace", "--as", "B")["id"]
    assert new != old and j(abx, "show", old)["status"] == "cancelled"
