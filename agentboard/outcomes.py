"""Outcome ledger: what each group of runs cost per ACCEPTED outcome. Read-only SQL over runs, tasks and events.

A run is an *accepted outcome* when all of these hold:
  - its outcome is agent_done, done or done_auto, and it is the last such run of its card;
  - the card is still `done` and its result is not empty;
  - the card was not reopened (`ab retry`) after that run ended;
  - no review card pointing at it (label `reviews=<id>`) finished with a result starting with FAIL.
Every other run lands in one failure class. Cost and tokens of ALL runs in a group count against the group's
accepted outcomes (failed attempts are what an accepted outcome really cost). Only some harnesses report `cost_usd`;
`usd_known_runs` says how many runs in the group had one, so a $ figure never hides missing data.
"""
import json
from datetime import datetime, timezone

from . import store

DONE = ("agent_done", "done", "done_auto")
CLASSES = ("limit", "stall_killed", "expired", "failed", "parked", "no_op_done", "release_loop", "reopened", "review_fail", "infra", "running", "other")
_OUT_CLASS = {"limit": "limit", "killed": "stall_killed", "expired": "expired", "failed": "failed", "agent_dead": "failed",
              "auth": "failed", "release_loop": "release_loop", "parked": "parked", None: "running", "agent_open": "running",
              "agent_claimed": "running", "gateway_lost": "infra", "gateway_down": "infra", "interrupted": "infra"}

DEFAULT_BY = "label:type,harness,tier"


def _key(name, run, labels):
    if name.startswith("label:"):
        return str(labels.get(name[6:]) or "-")
    if name == "tier":
        return run["tier"] or "-"
    if name in ("harness", "model", "board", "bucket"):
        return run.get(name) or "-"
    raise ValueError(f"bad --by key {name!r}: use label:NAME, harness, tier, model, board or bucket")


def ledger(conn, since_s=None, by=DEFAULT_BY, t=None):
    """Rows per group (sorted by agent-hours) plus totals. `since_s` keeps runs started in the last N seconds."""
    t = t or store.now()
    keys = [k.strip() for k in (by or DEFAULT_BY).split(",") if k.strip()]
    for k in keys:
        if not (k.startswith("label:") or k in ("tier", "harness", "model", "board", "bucket")):
            raise ValueError(f"bad --by key {k!r}: use label:NAME, harness, tier, model, board or bucket")
    cutoff = t - since_s if since_s else 0
    runs = conn.execute(
        "SELECT r.id, r.task_id, r.harness, r.model, r.tier, r.bucket, r.outcome, r.started_at, r.ended_at, r.input, r.output,"
        " r.cache_read, r.cache_write, r.cost_usd, k.labels, k.status tstatus, k.result, k.board"
        " FROM runs r JOIN tasks k ON k.id=r.task_id ORDER BY r.id").fetchall()
    last_done = {}
    for r in runs:
        if r["outcome"] in DONE:
            last_done[r["task_id"]] = r["id"]
    retries = {}
    for e in conn.execute("SELECT task_id, MAX(ts) ts FROM events WHERE kind='retry' GROUP BY task_id"):
        retries[e["task_id"]] = e["ts"]
    bad_review = set()
    for k in conn.execute("SELECT labels, result FROM tasks WHERE status='done' AND labels LIKE '%\"reviews\"%'"):
        res = (k["result"] or "").strip().upper()
        if res.startswith("FAIL") or res.startswith("VERDICT: FAIL"):
            tgt = json.loads(k["labels"] or "{}").get("reviews")
            if tgt:
                bad_review.add(tgt)
    groups = {}
    first_ts = None
    for r in runs:
        if r["started_at"] is not None and r["started_at"] < cutoff:
            continue
        if r["started_at"] is not None and (first_ts is None or r["started_at"] < first_ts):
            first_ts = r["started_at"]
        labels = json.loads(r["labels"] or "{}")
        row = dict(r)
        g = tuple(_key(k, row, labels) for k in keys)
        s = groups.setdefault(g, {"runs": 0, "accepted": 0, "agent_s": 0.0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                  "usd": 0.0, "usd_known_runs": 0, "failures": {c: 0 for c in CLASSES}})
        s["runs"] += 1
        s["agent_s"] += max(0.0, (r["ended_at"] or t) - (r["started_at"] or t))
        for f in ("input", "output", "cache_read", "cache_write"):
            s[f] += r[f] or 0
        if r["cost_usd"]:
            s["usd"] += r["cost_usd"]
            s["usd_known_runs"] += 1
        oc = r["outcome"]
        if oc in DONE:
            if last_done.get(r["task_id"]) != r["id"] or r["tstatus"] != "done":
                cls = None  # superseded by a later done run, or the card is no longer done: neither accepted nor a failure class
            elif not (r["result"] or "").strip():
                cls = "no_op_done"
            elif retries.get(r["task_id"], 0) > (r["ended_at"] or 0):
                cls = "reopened"
            elif r["task_id"] in bad_review:
                cls = "review_fail"
            else:
                s["accepted"] += 1
                cls = None
        elif oc in _OUT_CLASS:
            cls = _OUT_CLASS[oc]
        else:
            cls = "other"
        if cls:
            s["failures"][cls] += 1
    rows, tot = [], {"runs": 0, "accepted": 0, "agent_h": 0.0, "output": 0, "tokens": 0, "usd": 0.0,
                     "failures": {c: 0 for c in CLASSES}}
    for g, s in groups.items():
        tokens = s["input"] + s["output"] + s["cache_read"] + s["cache_write"]
        a = s["accepted"]
        rows.append({"group": dict(zip(keys, g)), "runs": s["runs"], "accepted": a, "agent_h": round(s["agent_s"] / 3600, 2),
                     "input": s["input"], "output": s["output"], "cache_read": s["cache_read"], "cache_write": s["cache_write"],
                     "tokens": tokens, "usd": round(s["usd"], 2) if s["usd_known_runs"] else None, "usd_known_runs": s["usd_known_runs"],
                     "usd_per_accepted": round(s["usd"] / a, 2) if a and s["usd_known_runs"] else None,
                     "output_per_accepted": round(s["output"] / a) if a else None,
                     "tokens_per_accepted": round(tokens / a) if a else None,
                     "failures": {c: n for c, n in s["failures"].items() if n}})
        tot["runs"] += s["runs"]
        tot["accepted"] += a
        tot["agent_h"] += s["agent_s"] / 3600
        tot["output"] += s["output"]
        tot["tokens"] += tokens
        tot["usd"] += s["usd"]
        for c, n in s["failures"].items():
            tot["failures"][c] += n
    rows.sort(key=lambda x: -x["agent_h"])
    tot["agent_h"] = round(tot["agent_h"], 2)
    tot["usd"] = round(tot["usd"], 2)
    tot["failures"] = {c: n for c, n in tot["failures"].items() if n}
    first_run = datetime.fromtimestamp(first_ts, timezone.utc).isoformat(timespec="seconds") if first_ts is not None else None
    tot["first_run"] = first_run
    tot["runs_in_scope"] = tot["runs"]
    return {"since_s": since_s, "by": keys, "generated_at": t, "first_run": first_run, "runs_in_scope": tot["runs"],
            "totals": tot, "groups": rows}


def _n(n):
    if n is None:
        return "-"
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n))


def render(rep):
    keys = rep["by"]
    head = [k.replace("label:", "") for k in keys] + ["runs", "acc", "agent-h", "out", "tokens", "$", "$/acc", "tok/acc", "failures"]
    lines = []
    for r in rep["groups"]:
        f = " ".join(f"{c}={n}" for c, n in r["failures"].items())
        lines.append([r["group"][k] for k in keys] + [str(r["runs"]), str(r["accepted"]), f"{r['agent_h']:.1f}", _n(r["output"]),
                      _n(r["tokens"]), "-" if r["usd"] is None else f"{r['usd']:.2f}",
                      "-" if r["usd_per_accepted"] is None else f"{r['usd_per_accepted']:.2f}", _n(r["tokens_per_accepted"]), f])
    t = rep["totals"]
    lines.append(["TOTAL"] + [""] * (len(keys) - 1) + [str(t["runs"]), str(t["accepted"]), f"{t['agent_h']:.1f}", _n(t["output"]),
                 _n(t["tokens"]), f"{t['usd']:.2f}", "", "", " ".join(f"{c}={n}" for c, n in t["failures"].items())])
    w = [max(len(str(x[i])) for x in lines + [head]) for i in range(len(head) - 1)]
    fmt = lambda x: "  ".join(str(v).ljust(w[i]) for i, v in enumerate(x[:-1])) + "  " + x[-1]
    first = f"first run: {rep['first_run'] or '-'} ({rep['runs_in_scope']} runs in scope)"
    return "\n".join([fmt(head)] + [fmt(x) for x in lines] + [first])
