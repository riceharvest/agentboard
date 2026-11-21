"""Outcome ledger on a tiny synthetic board: accepted vs reopened / failed-review / no-op done, failure classes, $ per accepted."""
import json

from agentboard import outcomes, store

T = 1_000_000.0


def _card(c, title, status, result, labels=None):
    tid = store.add_task(c, "main", title, labels=labels or {}, board="main")
    c.execute("UPDATE tasks SET status=?, result=? WHERE id=?", (status, result, tid))
    return tid


def _run(c, tid, harness, tier, outcome, dur_s=3600, out=1000, cost=None, start=T):
    c.execute("INSERT INTO runs(task_id,harness,tier,outcome,started_at,ended_at,input,output,cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
              (tid, harness, tier, outcome, start, start + dur_s, 500, out, cost))


def test_ledger_synthetic(tmp_path):
    c = store.connect(tmp_path / "b.db")
    ok = _card(c, "ok", "done", "did it", {"type": "build"})
    _run(c, ok, "claude", "medium", "limit", dur_s=1800, out=100, cost=1.0)
    _run(c, ok, "claude", "medium", "agent_done", out=900, cost=3.0)
    reopened = _card(c, "reopened", "done", "x", {"type": "build"})
    _run(c, reopened, "claude", "medium", "agent_done", cost=2.0)
    store.event(c, reopened, "u", "retry", {"was": "done"})
    c.execute("UPDATE events SET ts=? WHERE kind='retry'", (T + 7200,))
    reviewed = _card(c, "reviewed", "done", "y", {"type": "build"})
    _run(c, reviewed, "hermes", "low", "agent_done")
    _card(c, "rev", "done", "FAIL: wrong", {"type": "review", "reviews": reviewed})
    noop = _card(c, "noop", "done", "", {"type": "build"})
    _run(c, noop, "hermes", "low", "agent_done")
    _run(c, noop, "hermes", "low", "killed", dur_s=7200)
    old = _card(c, "old", "done", "z", {"type": "build"})
    _run(c, old, "hermes", "low", "agent_done", start=T - 10 * 86400)

    rep = outcomes.ledger(c, since_s=7 * 86400, by="label:type,harness,tier", t=T + 86400)
    g = {(r["group"]["label:type"], r["group"]["harness"], r["group"]["tier"]): r for r in rep["groups"]}
    cl = g[("build", "claude", "medium")]
    assert (cl["runs"], cl["accepted"]) == (3, 1)
    assert cl["failures"] == {"limit": 1, "reopened": 1}
    assert cl["usd"] == 6.0 and cl["usd_per_accepted"] == 6.0 and cl["usd_known_runs"] == 3
    assert cl["output_per_accepted"] == 2000
    he = g[("build", "hermes", "low")]
    assert he["accepted"] == 0 and he["usd"] is None and he["usd_per_accepted"] is None
    assert he["failures"] == {"review_fail": 1, "no_op_done": 1, "stall_killed": 1}
    assert rep["totals"]["runs"] == 6  # the 10-day-old run is outside --since 7d
    assert "TOTAL" in outcomes.render(rep)
    json.dumps(rep)
    assert outcomes.ledger(c, by="harness")["totals"]["runs"] == 7


def test_cli_outcomes_json(abx):
    code, out, err = abx("outcomes", "--json", "--by", "harness")
    assert code == 0, err
    assert json.loads(out)["groups"] == []
    code, _, err = abx("outcomes", "--by", "nonsense")
    assert code != 0


def test_first_run(tmp_path):
    c = store.connect(tmp_path / "f.db")
    rep = outcomes.ledger(c, t=T)
    assert rep["first_run"] is None and rep["runs_in_scope"] == 0
    assert "first run: -" in outcomes.render(rep)
    a = _card(c, "a", "done", "x", {"type": "build"})
    b = _card(c, "b", "done", "y", {"type": "build"})
    c.execute("UPDATE tasks SET board='other' WHERE id=?", (b,))
    _run(c, a, "claude", "low", "agent_done", start=T - 5 * 86400)
    _run(c, b, "claude", "low", "agent_done", start=T - 2 * 86400)
    _run(c, a, "claude", "low", "agent_done", start=T - 3600)
    rep = outcomes.ledger(c, t=T)  # spans every board
    assert rep["runs_in_scope"] == 3 and rep["totals"]["first_run"] == rep["first_run"]
    assert rep["first_run"].startswith("1970-01-07")  # T-5d
    # --since narrows the window: first_run is the earliest run INSIDE it
    w = outcomes.ledger(c, since_s=3 * 86400, t=T)
    assert w["runs_in_scope"] == 2 and w["first_run"].startswith("1970-01-10")
    assert "first run: 1970-01-10" in outcomes.render(w)
