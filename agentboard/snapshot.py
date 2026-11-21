"""`ab snapshot`: everything a UI needs in one read-only call (see snapshot.schema.json).

Rules: one read-only connection and one read transaction, batch queries (no per-task lookups), no network and no
harness probes. The provider plan percentage comes from the last reading the runner stored."""
import json
import re
import time
from pathlib import Path

from . import runner as R
from . import store, usage

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).resolve().parent / "snapshot.schema.json"
RESULT_HEAD = 300
MAX_TAIL = 4 * 1024 * 1024

TASK_COLS = ("id, board, title, channel, status, priority, labels, tier, harness, model, workdir, runnable, attempts, "
             "max_attempts, timeout_s, owner, lease_until, not_before, run_id, created_by, created_at, updated_at, park, "
             f"substr(result,1,{RESULT_HEAD}) AS result_head, cmd IS NOT NULL AS is_cmd")

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b[()][0-2AB]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text):
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\r" in text:  # progress bars redraw one line with \r: keep the final state of each line
        text = "\n".join(ln.rsplit("\r", 1)[-1] for ln in text.split("\n"))
    return _CTRL.sub("", text)


def read_tail(path, nbytes):
    """-> (text, file size, truncated). Last `nbytes` of a file, ANSI stripped, never splits a UTF-8 character."""
    if not path:
        return "", 0, False
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - nbytes)
            f.seek(start)
            data = f.read(nbytes)
    except OSError:
        return "", 0, False
    if start:
        i = 0
        while i < len(data) and i < 4 and 0x80 <= data[i] < 0xC0:  # cut in the middle of a character
            i += 1
        data = data[i:]
    return strip_ansi(data.decode("utf-8", errors="replace")), size, start > 0


def parse_tail_spec(spec):
    m = re.fullmatch(r"(\d+):(\d+)", (spec or "").strip())
    if not m:
        raise ValueError(f"bad --tail {spec!r}: use RUN_ID:BYTES, e.g. 12:4000")
    return int(m[1]), min(int(m[2]), MAX_TAIL)


def run_tail(conn, run_id, nbytes):
    r = conn.execute("SELECT id, task_id, harness, started_at, ended_at, out_path, err_path FROM runs WHERE id=?", (run_id,)).fetchone()
    if r is None:
        return {"run": run_id, "bytes": nbytes, "error": f"no run {run_id}"}
    out, out_size, out_cut = read_tail(r["out_path"], nbytes)
    err, err_size, err_cut = read_tail(r["err_path"], nbytes)
    return {"run": run_id, "task_id": r["task_id"], "harness": r["harness"], "running": r["ended_at"] is None, "bytes": nbytes,
            "out": out, "out_size": out_size, "out_truncated": out_cut, "err": err, "err_size": err_size, "err_truncated": err_cut,
            "error": None if (r["out_path"] or r["err_path"]) else "run has no output files yet"}


def _event(r):
    d = dict(r)
    d["payload"] = json.loads(d["payload"]) if d.get("payload") else None
    return d


def _chunks(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _task(r, keys, deps, dep_status, t):
    d = dict(zip(keys, r))
    d["runnable"] = bool(d["runnable"])
    d["kind"] = "cmd" if d.pop("is_cmd", 0) else "agent"
    lab = d["labels"]
    d["labels"] = json.loads(lab) if lab and lab != "{}" else {}
    d["park"] = json.loads(d["park"]) if d.get("park") else None
    ds = deps.get(d["id"], [])
    d["deps"] = ds
    d["deps_open"] = [x for x in ds if dep_status.get(x) != "done"]
    if d["status"] != "open":
        d["state"] = d["status"]
    elif store.park_running(d["park"]):
        d["state"] = "running"
    elif store.park_active(d["park"]):
        d["state"] = "waiting"
    elif any(dep_status.get(x) in ("dead", "cancelled") for x in ds):
        d["state"] = "stuck"
    elif d["deps_open"] or (d["not_before"] and d["not_before"] > t):
        d["state"] = "waiting"
    else:
        d["state"] = "ready"
    return d


def build(conn, actor="agent", board=None, closed=50, events_after=None, events_limit=200, tail=None):
    t0 = time.perf_counter()
    conn.execute("BEGIN")  # one consistent read: the event cursor can't run ahead of the tasks shown
    try:
        return _build(conn, t0, actor, board, closed, events_after, events_limit, tail)
    finally:
        conn.execute("ROLLBACK")


def _build(conn, t0, actor, board, closed, events_after, events_limit, tail):
    t = time.time()
    grants = conn.execute("SELECT * FROM grants").fetchall()
    memo = {}

    def can(b, ch=None):
        k = (b, ch)
        if k not in memo:
            memo[k] = store.explain(conn, actor, "read", b, ch, grants=grants)[0]
        return memo[k]

    if board:
        store.require_board(conn, board)
        if not can(board):
            raise PermissionError(f"{actor} has no 'read' on board {board}")
    ctl = store.cfg_all(conn)

    # ---- tasks: every open/claimed one plus the most recent closed ones
    bsql, bargs = (" AND board=?", [board]) if board else ("", [])
    rows = conn.execute(f"SELECT {TASK_COLS} FROM tasks WHERE status IN ('open','claimed'){bsql} "
                        "ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END, priority DESC, created_at", bargs).fetchall()
    open_n = len(rows)
    closed_rows = []
    if closed > 0:
        for st in ("done", "dead", "cancelled"):
            closed_rows += conn.execute(f"SELECT {TASK_COLS} FROM tasks WHERE status=?{bsql} ORDER BY updated_at DESC LIMIT ?",
                                        [st] + bargs + [closed]).fetchall()
        closed_rows.sort(key=lambda r: -(r["updated_at"] or 0))
        closed_rows = closed_rows[:closed]
    rows = [r for r in rows + closed_rows if can(r["board"], r["channel"])]
    ids = [r["id"] for r in rows]
    deps, dep_status = {}, {}
    for ch in _chunks(ids):
        q = ",".join("?" * len(ch))
        for x in conn.execute(f"SELECT x.task_id, x.dep_id, p.status FROM deps x LEFT JOIN tasks p ON p.id=x.dep_id "
                              f"WHERE x.task_id IN ({q})", ch):
            deps.setdefault(x["task_id"], []).append(x["dep_id"])
            dep_status[x["dep_id"]] = x["status"]
    keys = rows[0].keys() if rows else ()
    tasks = [_task(r, keys, deps, dep_status, t) for r in rows]
    held = store.busy_dirs(conn)
    for d in tasks:
        if d["state"] == "ready":
            why = store.workdir_wait_reason(conn, d, held)
            if why:
                d["wait_reason"] = why

    # ---- boards
    counts, running = {}, {}
    for r in conn.execute("SELECT board, status, COUNT(*) n FROM tasks GROUP BY board, status"):
        counts.setdefault(r["board"], {})[r["status"]] = r["n"]
    states = {}
    for d in tasks:
        if d["status"] in ("open", "claimed"):
            s = states.setdefault(d["board"], {})
            s[d["state"]] = s.get(d["state"], 0) + 1
    boards = []
    for b in conn.execute("SELECT * FROM boards ORDER BY name"):
        if not can(b["name"]):
            continue
        boards.append({"name": b["name"], "description": b["description"] or "", "paused": bool(b["paused"]),
                       "max_slots": b["max_slots"], "default_tier": b["default_tier"], "default_workdir": b["default_workdir"],
                       "counts": counts.get(b["name"], {}), "states": states.get(b["name"], {}), "running": 0})
    bmap = {b["name"]: b for b in boards}

    # ---- schedules (and which window is open now)
    schedules, active, override = [], [], None
    for s in conn.execute("SELECT * FROM schedules ORDER BY name"):
        d = {"name": s["name"], "kind": s["kind"], "spec": json.loads(s["spec"]), "action": json.loads(s["action"]),
             "enabled": bool(s["enabled"]), "last_fired": s["last_fired"], "active_now": False}
        if d["kind"] == "window" and d["enabled"] and R.window_active(d["spec"], t):
            d["active_now"] = True
            active.append(d["name"])
            override = max(override or 0, int(d["action"].get("slots", 0)))
        schedules.append(d)

    # ---- runner
    st = R.runner_status(conn)
    children = []
    for ch in st.pop("children", []):
        if (ch["board"] and ch["board"] not in bmap) or (board and ch["board"] != board):
            continue
        ch["elapsed_s"] = max(0.0, t - ch["started_at"])
        ch["kind"] = "cmd" if ch["harness"] == "cmd" else "agent"
        children.append(ch)
        if ch["board"] in bmap:
            bmap[ch["board"]]["running"] += 1
    live = {ch["task_id"] for ch in children}
    jobs = [{"task_id": d["id"], "board": d["board"], "title": d["title"], "pid": (d["park"] or {}).get("pid"),
             "file": (d["park"] or {}).get("file"), "since": (d["park"] or {}).get("at"), "note": (d["park"] or {}).get("note")}
            for d in tasks if d["state"] == "running" and d["id"] not in live and (not board or d["board"] == board)]
    for j in jobs:
        if j["board"] in bmap:
            bmap[j["board"]]["running"] += 1
    ready_runnable = sum(1 for d in tasks if d["state"] == "ready" and d["runnable"] and not bmap.get(d["board"], {}).get("paused"))
    hb = st.get("heartbeat")
    runner = {"running": bool(st.get("running")), "pid": st.get("pid"), "host": st.get("host"), "started_at": st.get("started_at"),
              "state": st.get("state") if st.get("running") else "stopped", "active": len(children), "slots": st.get("slots"),
              "slots_setting": int(ctl["slots"] or 0), "slots_effective": override if override is not None else int(ctl["slots"] or 0),
              "paused": bool(ctl["paused"]), "draining": bool(ctl["draining"]), "stop": ctl["stop"] or "",
              "heartbeat": hb, "heartbeat_age_s": (t - hb) if hb else None, "note": st.get("note"),
              "free_gib": round(R.free_gib(), 2), "min_free_gib": float(ctl["min_free_gib"] or 0), "ready": ready_runnable,
              "launch_per_tick": int(ctl.get("launch_per_tick") or 0), "pace_interval": usage.pace_interval(conn)[0],
              "pacer": bool(ctl.get("pacer", True)), "auto_worktree": bool(ctl.get("auto_worktree", True)),
              "windowed": override is not None, "children": children, "jobs": jobs}

    # ---- limits, caps, plan, usage
    limits = [{"scope": r["scope"], "kind": r["kind"], "until": r["until"], "reason": r["reason"], "set_at": r["set_at"],
               "remaining_s": max(0.0, r["until"] - t)}
              for r in conn.execute("SELECT * FROM limits WHERE until > ? ORDER BY until", (t,))]
    lim = {x["scope"]: x for x in limits}
    caps = ctl["caps"] or {}
    plan_raw, plan_age = usage.stored_plan(conn)
    stale = plan_age is not None and plan_age > usage.PLAN_STALE_S
    live_plan = None if stale else plan_raw
    plan = {"claude_plan_pct": ({**plan_raw, "age_s": plan_age, "stale": stale} if plan_raw else None),
            "weekly_pace_pct": None, "guards": []}
    for b, cap in caps.items():
        cap = usage.effective_cap(conn, cap)
        if not usage.uses_plan(cap):
            continue
        if live_plan:
            pace = usage.weekly_pace_pct(cap, live_plan, t)
            if pace is not None and plan["weekly_pace_pct"] is None:
                plan["weekly_pace_pct"] = round(pace, 1)
        reason = usage.plan_reason(cap, live_plan)
        if reason:
            plan["guards"].append({"bucket": b, "reason": reason})
    win = usage.all_window_totals(conn, t)
    pstate = ctl.get("_pacer") or {}
    zero = {"calls": 0, "output": 0, "fresh_in": 0, "cost_usd": 0, "runs": 0}
    names = set(win) | set(caps) | set(pstate) | {s.split(":", 1)[1] for s in lim if s.startswith("bucket:")}
    if live_plan:
        names.add("claude-sub")
    buckets = []
    for b in sorted(names):
        w = win.get(b) or {k: dict(zero) for k in ("5h", "24h", "7d")}
        l = lim.get(f"bucket:{b}")
        pinfo = usage.launch_budget(conn, b, plan=live_plan, now=t, pacer_state=pstate)
        buckets.append({"bucket": b, "5h": w["5h"], "24h": w["24h"], "7d": w["7d"],
                        "limit": {"kind": l["kind"], "until": l["until"], "reason": l["reason"]} if l else None,
                        "cap": caps.get(b), "cap_hit": usage.cap_status(conn, b, live_plan),
                        "launch_budget": pinfo["budget"], "budget_why": pinfo["why"], "pacer": pinfo})

    # ---- usage history for sparklines (optional keys: schema v1 clients may ignore them)
    usage_hourly, usage_split = usage.history(conn, t)

    # ---- watchers
    watchers = []
    for w in conn.execute("SELECT * FROM watchers ORDER BY board, name"):
        spec, act = json.loads(w["spec"]), json.loads(w["action"])
        if w["board"] not in bmap or (board and w["board"] != board and not spec.get("all_boards")):
            continue
        watchers.append({"id": w["id"], "board": w["board"], "name": w["name"], "enabled": bool(w["enabled"]),
                         "kinds": spec.get("kinds") or [], "all_boards": bool(spec.get("all_boards")), "fired": w["fired"],
                         "last_fired": w["last_fired"], "cooldown": act.get("cooldown"), "tier": act.get("tier"),
                         "spec": spec, "action": {k: v for k, v in act.items() if k != "brief"}})

    # ---- events (newest `events_limit` after the cursor, oldest first)
    cursor = conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    events, truncated = [], False
    if events_after is not None:
        esql, eargs = "SELECT * FROM events WHERE id > ?", [events_after]
        if board:
            esql += " AND (board=? OR board IS NULL)"
            eargs.append(board)
        got = conn.execute(esql + " ORDER BY id DESC LIMIT ?", eargs + [events_limit + 1]).fetchall()
        truncated = len(got) > events_limit
        events = [e for e in (_event(r) for r in reversed(got[:events_limit]))
                  if not e.get("board") or can(e["board"], e.get("channel"))]

    tiers = ctl["tiers"] or {}
    return {
        "schema": SCHEMA_VERSION, "generated_at": t, "board": board, "actor": actor, "db": str(store.DB_PATH),
        "boards": boards,
        "tasks": tasks, "tasks_open": open_n, "tasks_closed_shown": len(closed_rows),
        "runner": runner, "limits": limits, "caps": caps, "plan": plan, "usage": buckets,
        "usage_hourly": usage_hourly, "usage_split": usage_split,
        "watchers": watchers, "schedules": schedules, "active_windows": active,
        "tiers": {"order": ctl["tier_order"] or list(tiers), "tiers": tiers, "fallback": ctl["tier_fallback"],
                  "default": ctl["default_tier"]},
        "events": events, "events_cursor": cursor, "events_truncated": truncated,
        "tail": run_tail(conn, tail[0], tail[1]) if tail else None,
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def _ts(t):
    return store.fmt_ts(t)


def render_text(s):
    """Short human summary of a snapshot (the JSON is the interface)."""
    r = s["runner"]
    lines = [f"runner {'RUNNING' if r['running'] else 'NOT RUNNING'}"
             + (f" pid {r['pid']}" if r["pid"] else "") + f"  state={r['state']} slots={r['slots_effective']} active={r['active']} "
             f"paused={r['paused']} draining={r['draining']} ready={r['ready']} free={r['free_gib']}GiB"]
    for c in r["children"]:
        lines.append(f"  run {c['run']} {c['task_id']} " + (f"cmd: {(c['cmd'] or '')[:40]}" if c['harness'] == 'cmd' else f"{c['harness']}:{c['model'] or ''} [{c['bucket']}]") + " "
                     f"{store.fmt_dur(c['elapsed_s'])}/{store.fmt_dur(c['timeout_s'])}" + (f"  [{c['stall']}]" if c.get("stall") else "")
                     + f"  {(c['title'] or '')[:60]}")
    for j in r.get("jobs", []):
        lines.append(f"  job {j['task_id']} " + (f"pid {j['pid']}" if j["pid"] else f"file {j['file']}") + f"  (parked card, work runs in the background)  {(j['title'] or '')[:60]}")
    for b in s["boards"]:
        lines.append(f"board {b['name']:<12} {'PAUSED ' if b['paused'] else ''}running={b['running']} max_slots={b['max_slots']} "
                     + " ".join(f"{k}={v}" for k, v in sorted(b["counts"].items())))
    p = s["plan"]["claude_plan_pct"]
    if p:
        lines.append(f"plan 5h={p['five_hour']:.0f}% 7d={p['seven_day']:.0f}% pace={s['plan']['weekly_pace_pct']} (read {store.fmt_dur(p['age_s'])} ago)"
                     + "".join(f"  GUARD {g['bucket']}: {g['reason']}" for g in s["plan"]["guards"]))
    for u in s["usage"]:
        p = u.get("pacer") or {}
        lines.append(f"bucket {u['bucket']:<16} 5h={u['5h']['output']} 24h={u['24h']['output']} 7d={u['7d']['output']} output tokens"
                     + f"  budget={u.get('launch_budget', '-')}/{p.get('interval', '-')} ({u.get('budget_why', '-')})"
                     + (f"  LIMITED until {_ts(u['limit']['until'])}" if u["limit"] else "")
                     + (f"  CAP {u['cap_hit']}" if u["cap_hit"] else ""))
    lines.append(f"{s['tasks_open']} open/claimed + {s['tasks_closed_shown']} recent closed tasks, events cursor {s['events_cursor']}, "
                 f"{s['took_ms']} ms")
    return "\n".join(lines)
