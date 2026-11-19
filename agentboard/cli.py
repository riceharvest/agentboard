"""`ab` - agent-only task board. Every command takes --json for machine output."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import automation as A
from . import harness as H
from . import runner as R
from . import store, usage
from .store import fmt_dur, fmt_ts, now, parse_dur, parse_time

GUIDE = Path(__file__).resolve().parent / "GUIDE.md"


class Out:
    def __init__(self, as_json):
        self.as_json = as_json

    def __call__(self, obj, text=None):
        if self.as_json or text is None:
            print(json.dumps(obj, default=str, ensure_ascii=False))
        else:
            t = text(obj) if callable(text) else text
            if t:
                print(t)


def kv_list(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"expected key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def jarg(s):
    if s is None:
        return None
    if s.startswith("@"):
        s = Path(s[1:]).read_text()
    return json.loads(s)


def body_arg(a):
    if getattr(a, "body_file", None):
        return Path(a.body_file).read_text()
    if getattr(a, "body", None) == "-":
        return sys.stdin.read()
    return getattr(a, "body", None) or ""


def text_arg(value, path=None):
    """Text from a --FILE option, '-' (stdin) or the plain argument (avoids shell quoting of $ and backticks)."""
    if path:
        return Path(path).expanduser().read_text()
    return sys.stdin.read() if value == "-" else (value or "")


def tline(d):
    lab = ",".join(f"{k}={v}" for k, v in (d.get("labels") or {}).items())
    extra = []
    if d.get("owner"):
        extra.append(f"by {d['owner']}")
    if d["state"] == "waiting" and d.get("not_before") and d["not_before"] > now():
        extra.append(f"after {fmt_ts(d['not_before'])}")
    if d.get("deps"):
        extra.append("deps " + ",".join(d["deps"]))
    if d.get("attempts"):
        extra.append(f"try {d['attempts']}/{d['max_attempts']}")
    return (f"{d['id']}  {d['state']:<9} p{d['priority']:<3} {d['tier']:<6} {d['channel']:<12} {d['title'][:70]}"
            + (f"  [{lab}]" if lab else "") + (f"  ({'; '.join(extra)})" if extra else ""))


def show_text(d):
    lines = [f"{d['id']}  {d['title']}",
             f"state={d['state']} status={d['status']} board={d['board']} channel={d['channel']} priority={d['priority']} tier={d['tier']}"
             + (f" pin={d['harness']}:{d.get('model') or ''}" if d.get("harness") else "")
             + f" attempts={d['attempts']}/{d['max_attempts']} runnable={bool(d['runnable'])}",
             f"created {fmt_ts(d['created_at'])} by {d['created_by']}, updated {fmt_ts(d['updated_at'])}"
             + (f", owner {d['owner']} lease until {fmt_ts(d['lease_until'])}" if d.get("owner") else "")
             + (f", not before {fmt_ts(d['not_before'])}" if d.get("not_before") else "")]
    if d.get("workdir"):
        lines.append(f"workdir {d['workdir']}")
    if d.get("labels"):
        lines.append("labels " + ", ".join(f"{k}={v}" for k, v in d["labels"].items()))
    if d.get("deps"):
        lines.append("depends on " + ", ".join(d["deps"]))
    if d.get("children"):
        lines.append("blocks " + ", ".join(d["children"]))
    if d.get("body"):
        lines += ["", d["body"].rstrip()]
    if d.get("result"):
        lines += ["", "RESULT: " + d["result"].rstrip()]
    if d.get("data"):
        lines.append("DATA: " + json.dumps(d["data"]))
    if d.get("notes"):
        lines.append("")
        lines += [f"[{fmt_ts(n['ts'])} m{n['id']} {n['author']}] {n['body']}" for n in d["notes"]]
    if d.get("runs"):
        lines.append("")
        for r in d["runs"]:
            lines.append(f"run {r['id']} {r['harness']}{'@' + r['endpoint'] if r.get('endpoint') else ''}:{r['model'] or ''} [{r['bucket']}] {fmt_ts(r['started_at'])} "
                         f"{fmt_dur((r['ended_at'] or now()) - r['started_at'])} -> {r['outcome'] or 'running'} "
                         f"in={r['input']} out={r['output']} cache_r={r['cache_read']}"
                         + (f" ${r['cost_usd']:.3f}" if r.get("cost_usd") else ""))
    return "\n".join(lines)


def actor_of(a):
    # Inside a runner-launched agent the identity is fixed: a worker can't promote itself with --as.
    if os.environ.get("AB_RUN") and os.environ.get("AB_ACTOR"):
        return os.environ["AB_ACTOR"]
    return a.actor or os.environ.get("AB_ACTOR") or "agent"


def board_of(a):
    return getattr(a, "board", None) or os.environ.get("AB_BOARD") or store.DEFAULT_BOARD


def admin(c, a, board=None):
    """Global settings need admin on the main board; board settings need admin on that board."""
    store.require(c, actor_of(a), "admin", board or store.DEFAULT_BOARD)


def visible(c, a, rows):
    me = actor_of(a)
    return [r for r in rows if store.can(c, me, "read", r["board"], r["channel"])]


# ---------------------------------------------------------------- commands
def cmd_add(c, a, out):
    workdir = a.workdir
    if workdir is None and os.environ.get("AB_TASK"):
        workdir = os.getcwd()  # sub-tasks created inside a run inherit the run's workdir
    if workdir:
        workdir = str(Path(workdir).expanduser().resolve())
    labels = kv_list(a.label)
    if a.reply_to:
        store.resolve_thread(c, a.reply_to)
        labels["reply_to"] = a.reply_to
    tid = store.add_task(c, actor_of(a), a.title, body_arg(a), a.channel, labels, a.priority,
                         [store.get_task(c, d)["id"] if store.get_task(c, d) else d for d in (a.dep or [])],
                         parse_time(a.after) if a.after else None, workdir, a.tier, a.harness, a.model,
                         not a.manual, a.max_attempts, parse_dur(a.timeout) if a.timeout else None, jarg(a.data),
                         board=board_of(a))
    out({"id": tid}, tid)


def cmd_ls(c, a, out):
    store.reap(c)
    sql, args = "SELECT * FROM tasks", []
    where = []
    status = a.status
    bsel = None if a.all_boards else board_of(a)
    if a.ready:
        rows = store.ready_tasks(c, a.channel, kv_list(a.label), limit=a.limit, board=bsel)
    else:
        if bsel:
            where.append("board=?")
            args.append(bsel)
        if status and status != "all":
            st = status.split(",")
            where.append("status IN (%s)" % ",".join("?" * len(st)))
            args += st
        elif not status:
            where.append("status IN ('open','claimed')")
        if a.channel:
            where.append("(channel=? OR channel LIKE ?)")
            args += [a.channel, a.channel.rstrip("/") + "/%"]
        for k, v in kv_list(a.label).items():
            where.append("json_extract(labels, ?)=?")
            args += [f"$.{json.dumps(k)}", v]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE status WHEN 'claimed' THEN 0 WHEN 'open' THEN 1 ELSE 2 END, priority DESC, updated_at DESC LIMIT ?"
        rows = c.execute(sql, args + [a.limit]).fetchall()
    items = [store.task_dict(c, r) for r in visible(c, a, rows)]
    out(items, lambda xs: "\n".join(tline(x) + (f"  <{x['board']}>" if a.all_boards else "") for x in xs) or "(nothing)")


def cmd_show(c, a, out):
    row = store.must_task(c, a.id)
    store.require(c, actor_of(a), "read", row["board"], row["channel"])
    out(store.task_dict(c, row, full=True), show_text)


def cmd_claim(c, a, out):
    deadline = now() + (parse_dur(a.wait) or 0)
    while True:
        tid, why = store.claim(c, actor_of(a), a.id, a.channel, kv_list(a.label), parse_dur(a.lease) if a.lease else None,
                               board=board_of(a))
        if tid or now() >= deadline:
            break
        time.sleep(2)
    if not tid:
        out({"task": None, "why": why}, f"nothing claimed: {why}")
        return 3
    out(store.task_dict(c, store.get_task(c, tid), full=True), show_text)


def cmd_renew(c, a, out):
    tid = store.renew(c, a.id, actor_of(a), parse_dur(a.lease) if a.lease else None, a.force)
    out({"id": tid, "renewed": True}, f"{tid} renewed")


def cmd_note(c, a, out):
    text = text_arg(a.text, a.file)
    tid = store.note(c, a.id, actor_of(a), text, jarg(a.data))
    out({"id": tid, "noted": True}, f"{tid} noted")


def cmd_done(c, a, out):
    text = text_arg(a.result, a.summary_file)
    tid = store.complete(c, a.id, actor_of(a), text, jarg(a.data), a.force)
    out({"id": tid, "status": "done"}, f"{tid} done")


def cmd_fail(c, a, out):
    tid, state = store.fail(c, a.id, actor_of(a), text_arg(a.why, a.why_file), a.force)
    out({"id": tid, "status": state}, f"{tid} failed -> {state}")


def cmd_release(c, a, out):
    tid, state = store.release(c, a.id, actor_of(a), a.why or "", a.force)
    out({"id": tid, "status": state}, f"{tid} released")


def cmd_defer(c, a, out):
    deps = [store.get_task(c, d)["id"] if store.get_task(c, d) else d for d in (a.after_task or [])]
    tid = store.defer(c, a.id, actor_of(a), parse_time(a.until) if a.until else None, deps, a.why or "", a.force)
    out({"id": tid, "status": "open", "deferred": True}, f"{tid} deferred")


def cmd_cancel(c, a, out):
    tid = store.cancel(c, a.id, actor_of(a), a.why or "")
    out({"id": tid, "status": "cancelled"}, f"{tid} cancelled")


def cmd_retry(c, a, out):
    tid = store.retry(c, a.id, actor_of(a))
    out({"id": tid, "status": "open"}, f"{tid} reopened")


def cmd_edit(c, a, out):
    f = {}
    for k in ("title", "channel", "priority", "tier", "harness", "model", "max_attempts", "move_to"):
        v = getattr(a, k)
        if v is not None:
            f["board" if k == "move_to" else k] = v if v != "" else None
    if a.body is not None or a.body_file:
        f["body"] = body_arg(a)
    if a.workdir is not None:
        f["workdir"] = str(Path(a.workdir).expanduser().resolve()) if a.workdir else None
    if a.after is not None:
        f["not_before"] = parse_time(a.after) if a.after else None
    if a.timeout is not None:
        f["timeout_s"] = parse_dur(a.timeout) if a.timeout else None
    if a.manual:
        f["runnable"] = 0
    if a.auto:
        f["runnable"] = 1
    if a.harness == "":
        f["model"] = None
    labels = {}
    for it in a.label or []:
        k, _, v = it.partition("=")
        labels[k] = v
    tid = store.edit(c, a.id, actor_of(a), f, labels, a.dep or [], a.rm_dep or [])
    out({"id": tid, "edited": list(f) + (["labels"] if labels else [])}, f"{tid} edited")


def cmd_log(c, a, out):
    args, sql = [], "SELECT * FROM events"
    if a.id:
        row = store.get_task(c, a.id)
        sql += " WHERE task_id=?"
        args.append(row["id"] if row else a.id)
    rows = [A.ev_dict(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", args + [a.limit]).fetchall()][::-1]
    rows = [r for r in rows if A.readable(c, actor_of(a), r)]
    out(rows, lambda xs: "\n".join(f"{r['id']:>6} {fmt_ts(r['ts'])} {r['task_id'] or '-':<11} {r['actor']:<16} {r['kind']:<14} "
                                   f"{json.dumps(r['payload']) if r['payload'] else ''}"[:220] for r in xs))


def cmd_whoami(c, a, out):
    me = actor_of(a)
    d = {"actor": me, "board": board_of(a), "task": os.environ.get("AB_TASK"), "run": os.environ.get("AB_RUN"),
         "db": str(store.DB_PATH), "admin": store.can(c, me, "admin", board_of(a))}
    out(d, lambda d: " ".join(f"{k}={v}" for k, v in d.items()))


def cmd_guide(c, a, out):
    print(GUIDE.read_text())


# ---- usage / limits / caps
def _num(n):
    n = n or 0
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n))


def cmd_usage(c, a, out):
    if not a.no_harvest:
        usage.harvest(c)
    since = parse_dur(a.since) if a.since else None
    if a.by == "task":
        rows = usage.by_task(c, since, a.limit)
        out(rows, lambda xs: "\n".join(f"{r['task_id']}  runs={r['runs']} {r['harnesses']}  in={_num(r['input'])} out={_num(r['output'])} "
                                       f"cache_r={_num(r['cache_read'])}" + (f" ${r['cost_usd']:.2f}" if r['cost_usd'] else "")
                                       + f" {fmt_dur(r['seconds'])}  {(r['title'] or '')[:50]}" for r in xs) or "(no runs)")
        return
    if a.by == "bucket" and not a.since:
        rows = usage.buckets_report(c)

        def txt(xs):
            lines = ["bucket            5h out / calls      24h out / calls     7d out / calls      state"]
            for r in xs:
                st = "ok"
                if r["limit"]:
                    st = f"{r['limit']['kind']} until {fmt_ts(r['limit']['until'])}"
                elif r["cap_hit"]:
                    st = "capped: " + r["cap_hit"]
                cells = "".join(f"{_num(r[w]['output']):>8} / {r[w]['calls']:<8}  " for w in ("5h", "24h", "7d"))
                lines.append(f"{r['bucket']:<16} {cells}{st}" + (f"  cap={json.dumps(r['cap'])}" if r["cap"] else ""))
            return "\n".join(lines)
        out(rows, txt)
        return
    rows = usage.summarize(c, since, a.by)
    out(rows, lambda xs: "\n".join(f"{(r['k'] or '-'):<34} calls={r['calls']:<6} in={_num(r['input'])} out={_num(r['output'])} "
                                   f"cache_r={_num(r['cache_read'])} cache_w={_num(r['cache_write'])}"
                                   + (f" ${r['cost_usd']:.2f}" if r['cost_usd'] else "") for r in xs) or "(no usage)")


def cmd_harvest(c, a, out):
    res = usage.harvest(c, a.only.split(",") if a.only else None)
    out(res, lambda d: " ".join(f"{k}={v}" for k, v in d.items()))


def cmd_limits(c, a, out):
    act = a.action or "ls"
    if act == "ls":
        rows = list(store.active_limits(c).values())
        out(rows, lambda xs: "\n".join(f"{r['scope']:<26} {r['kind']:<12} until {fmt_ts(r['until'])} ({fmt_dur(r['until'] - now())})  {(r['reason'] or '')[:120]}"
                                       for r in xs) or "(no active limits)")
    elif act == "set":
        admin(c, a)
        until = parse_time(a.until)
        with store.tx(c):
            store.set_limit(c, a.scope, a.kind or "manual", until, a.reason or "set by agent", actor_of(a))
        out({"scope": a.scope, "until": until}, f"{a.scope} limited until {fmt_ts(until)}")
    elif act == "clear":
        admin(c, a)
        with store.tx(c):
            store.clear_limit(c, a.scope, actor_of(a))
        out({"scope": a.scope, "cleared": True}, f"{a.scope} cleared")


def cmd_caps(c, a, out):
    caps = store.cfg(c, "caps") or {}
    if a.action == "set":
        cap = {"window": a.window or "5h"}
        for k in ("max_output", "max_cost", "max_runs", "max_calls", "max_5h_pct", "max_7d_pct", "pace_7d_lead_h"):
            v = getattr(a, k)
            if v is not None:
                cap[k] = v
        caps[a.bucket] = cap
    elif a.action == "clear":
        caps.pop(a.bucket, None)
    if a.action in ("set", "clear"):
        admin(c, a)
        with store.tx(c):
            store.cfg_set(c, "caps", caps, actor_of(a))
    out(caps, lambda d: "\n".join(f"{k}: {json.dumps(v)}" for k, v in d.items()) or "(no caps: only real limit errors stop a bucket)")


# ---- harnesses / models / tiers
def cmd_harness(c, a, out):
    lim = store.active_limits(c)
    rows = []
    for name, ad in H.ADAPTERS.items():
        rows.append({"harness": name, "bin": ad.bin, "installed": ad.available(), "limit": lim.get(f"harness:{name}"),
                     "harvested_usage": ad.harvested,
                     "endpoints": type(ad).endpoint_command is not H.Adapter.endpoint_command})
    out(rows, lambda xs: "\n".join(f"{r['harness']:<9} {'installed' if r['installed'] else 'missing':<10} "
                                   + f"{'endpoints' if r['endpoints'] else '-':<10} "
                                   + (f"{r['limit']['kind']} until {fmt_ts(r['limit']['until'])}: {(r['limit']['reason'] or '')[:100]}" if r['limit'] else "ok")
                                   for r in xs))


def refresh_models(c, which=None):
    got = {}
    for name, ad in H.ADAPTERS.items():
        if which and name not in which:
            continue
        if not ad.available():
            continue
        try:
            ms = ad.list_models()
        except Exception as e:
            got[name] = f"error: {e}"
            continue
        if not ms:
            continue
        with store.tx(c):
            c.execute("DELETE FROM models WHERE harness=?", (name,))
            for m, label in ms:
                c.execute("INSERT OR REPLACE INTO models(harness,model,label,seen_at) VALUES(?,?,?,?)", (name, m, label, now()))
        got[name] = len(ms)
    return got


def cmd_models(c, a, out):
    which = a.harness.split(",") if a.harness else None
    stale = c.execute("SELECT MIN(seen_at) FROM models").fetchone()[0]
    if a.refresh or stale is None or now() - stale > 86400:
        refresh_models(c, which)
    sql, args = "SELECT * FROM models", []
    if which:
        sql += " WHERE harness IN (%s)" % ",".join("?" * len(which))
        args = which
    rows = [dict(r) for r in c.execute(sql + " ORDER BY harness, model", args)]
    out(rows, lambda xs: "\n".join(f"{r['harness']:<9} {r['model']}" + (f"   ({r['label']})" if r['label'] else "") for r in xs))


def tiers_check(c):
    if c.execute("SELECT MIN(seen_at) FROM models").fetchone()[0] is None:
        refresh_models(c)
    known = {}
    for r in c.execute("SELECT harness, model FROM models"):
        known.setdefault(r["harness"], set()).add(r["model"])
    problems = []
    for tier, specs in (store.cfg(c, "tiers") or {}).items():
        for s in specs:
            try:
                cand = H.parse_spec(s)
            except ValueError as e:
                problems.append({"tier": tier, "spec": s, "problem": str(e)})
                continue
            if not H.ADAPTERS[cand.harness].available():
                problems.append({"tier": tier, "spec": s, "problem": "harness not installed"})
            elif cand.model and cand.harness in known and cand.model not in known[cand.harness]:
                problems.append({"tier": tier, "spec": s, "problem": "model not offered by the harness any more (see `ab models`)"})
    return problems


def cmd_tiers(c, a, out):
    tiers = store.cfg(c, "tiers") or {}
    order = store.cfg(c, "tier_order") or list(tiers)
    act = a.action or "ls"
    if act == "check":
        probs = tiers_check(c)
        out(probs, lambda xs: "\n".join(f"{p['tier']:<7} {p['spec']}: {p['problem']}" for p in xs) or "all tier entries resolve")
        return 1 if probs else 0
    if act in ("set", "add", "rm", "order"):
        admin(c, a)
        if act == "set":
            for s in a.specs:
                H.parse_spec(s)
            tiers[a.tier] = list(a.specs)
            if a.tier not in order:
                order.append(a.tier)
        elif act == "add":
            H.parse_spec(a.specs[0])
            lst = tiers.setdefault(a.tier, [])
            pos = a.pos if a.pos is not None else len(lst)
            for s in reversed(a.specs):
                lst.insert(pos, s)
            if a.tier not in order:
                order.append(a.tier)
        elif act == "rm":
            if not a.specs:
                tiers.pop(a.tier, None)
                order = [t for t in order if t != a.tier]
            else:
                lst = tiers.get(a.tier, [])
                for s in a.specs:
                    if s.isdigit() and int(s) < len(lst):
                        lst.pop(int(s))
                    elif s in lst:
                        lst.remove(s)
        elif act == "order":
            order = [t.strip() for t in a.tier.split(",") if t.strip()]
            missing = [t for t in order if t not in tiers]
            if missing:
                raise ValueError(f"unknown tiers: {missing}")
        with store.tx(c):
            store.cfg_set(c, "tiers", tiers, actor_of(a))
            store.cfg_set(c, "tier_order", order, actor_of(a))
    fb = store.cfg(c, "tier_fallback")
    res = {"order": order, "tiers": tiers, "fallback": fb, "default": store.cfg(c, "default_tier")}
    out(res, lambda d: "\n".join([f"order: {' < '.join(d['order'])}   default: {d['default']}   fallback: {d['fallback']}"]
                                 + [f"{t}:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(d['tiers'].get(t, []))) for t in d["order"]]))


# ---- runner / schedules / config
def cmd_runner(c, a, out):
    act = a.action or "status"
    actor = actor_of(a)
    if act != "status":
        admin(c, a)
    if act == "run":
        log = None
        R.Runner(c, log).run()
        return
    if act == "start":
        st = R.runner_status(c)
        if a.slots is not None:
            with store.tx(c):
                store.cfg_set(c, "slots", a.slots, actor)
        with store.tx(c):
            store.cfg_set(c, "paused", False, actor)
            store.cfg_set(c, "draining", False, actor)
            if st.get("running"):
                store.cfg_set(c, "stop", "", actor)  # cancels a pending graceful stop / reload
        if st.get("running"):
            out({"started": False, "running": True, "pid": st["pid"]}, f"runner already running (pid {st['pid']}); unpaused, pending stop cancelled")
            return
        pid = R.start_detached()
        for _ in range(20):
            time.sleep(0.5)
            st = R.runner_status(c)
            if st.get("running") and st.get("pid") == pid:
                break
        out({"started": True, "pid": pid, "running": bool(st.get("running"))},
            f"runner started (pid {pid}), log {store.HOME / 'runner.log'}" if st.get("running") else
            f"runner launched (pid {pid}) but no heartbeat yet; check {store.HOME / 'runner.log'}")
        return
    sets = {"pause": {"paused": True}, "resume": {"paused": False, "draining": False, "stop": ""}, "drain": {"draining": True},
            "stop": {"stop": "now" if a.now else "graceful"}, "reload": {"stop": "reload"}}
    if act in sets:
        with store.tx(c):
            for k, v in sets[act].items():
                store.cfg_set(c, k, v, actor)
        out({"ok": True, "action": act}, f"runner: {act}" + (" (now)" if act == "stop" and a.now else "")
            + (" (re-execs with the current code; running cards keep going)" if act == "reload" else ""))
        return
    if act == "slots":
        with store.tx(c):
            store.cfg_set(c, "slots", int(a.value), actor)
        out({"slots": int(a.value)}, f"slots = {a.value}")
        return
    if act == "service":
        unit = Path.home() / ".config/systemd/user/agentboard-runner.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(R.service_text())
        msg = f"wrote {unit}"
        if a.enable:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            r = subprocess.run(["systemctl", "--user", "enable", "--now", "agentboard-runner.service"], capture_output=True, text=True)
            msg += f"; enable --now rc={r.returncode} {r.stderr.strip()}"
        out({"unit": str(unit), "enabled": bool(a.enable)}, msg)
        return
    st = R.runner_status(c)
    ctl = store.cfg_all(c)
    st.update({"paused": ctl["paused"], "draining": ctl["draining"], "slots_setting": ctl["slots"], "stop": ctl["stop"],
               "free_gib": round(R.free_gib(), 1), "ready": len(store.ready_tasks(c, runnable_only=True))})

    def txt(s):
        head = (f"runner {'RUNNING' if s['running'] else 'NOT RUNNING'}" + (f" pid {s.get('pid')}" if s.get("pid") else "")
                + f"  state={s.get('state')} slots={s.get('slots')} (setting {s['slots_setting']}) paused={s['paused']} "
                f"draining={s['draining']} ready={s['ready']} free={s['free_gib']}GiB"
                + (f" heartbeat {fmt_dur(now() - s['heartbeat'])} ago" if s.get("heartbeat") else ""))
        lines = [head]
        if s.get("note"):
            lines.append("note: " + s["note"])
        for ch in s.get("children", []):
            lines.append(f"  run {ch['run']} {ch['task_id']} {ch['harness']}:{ch['model'] or ''} [{ch['bucket']}] "
                         f"{fmt_dur(now() - ch['started_at'])}  {(ch['title'] or '')[:60]}")
        return "\n".join(lines)
    out(st, txt)


def parse_action(s):
    """'pause' 'resume' 'drain' 'stop' 'slots=4' 'set:key=value'"""
    s = s.strip()
    if s in ("pause", "resume", "drain", "stop", "reload"):
        return {"do": s}
    if s.startswith("slots="):
        return {"do": "slots", "value": int(s.split("=", 1)[1])}
    if s.startswith("set:") and "=" in s:
        k, v = s[4:].split("=", 1)
        return {"do": "set", "key": k, "value": store.coerce(v)}
    raise ValueError(f"bad action {s!r}: use pause|resume|drain|stop|reload|slots=N|set:key=value")


def cmd_sched(c, a, out):
    act = a.action or "ls"
    if act != "ls":
        admin(c, a)
    if act == "add":
        if sum(bool(x) for x in (a.window, a.at, a.daily)) != 1:
            raise ValueError("give exactly one of --window HH:MM-HH:MM, --at TIME, --daily HH:MM")
        if a.window:
            fr, to = a.window.split("-")
            R._hm(fr), R._hm(to)
            kind, spec = "window", {"from": fr, "to": to, "days": a.days}
            if a.slots is None:
                raise ValueError("--window needs --slots N (slots while the window is open)")
            action = {"slots": a.slots}
        elif a.at:
            kind, spec, action = "at", {"at": parse_time(a.at)}, parse_action(a.do or "")
        else:
            R._hm(a.daily)
            kind, spec, action = "daily", {"time": a.daily, "days": a.days}, parse_action(a.do or "")
        if a.days:
            R.days_ok(a.days, __import__("datetime").datetime.now())
        with store.tx(c):
            c.execute("INSERT INTO schedules(name,kind,spec,action,enabled,created_by,created_at) VALUES(?,?,?,?,1,?,?) "
                      "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, spec=excluded.spec, action=excluded.action, enabled=1, last_fired=NULL",
                      (a.name, kind, json.dumps(spec), json.dumps(action), actor_of(a), now()))
            store.event(c, None, actor_of(a), "schedule_set", {"name": a.name, "kind": kind, "spec": spec, "action": action})
    elif act in ("rm", "enable", "disable"):
        with store.tx(c):
            if act == "rm":
                c.execute("DELETE FROM schedules WHERE name=?", (a.name,))
            else:
                c.execute("UPDATE schedules SET enabled=? WHERE name=?", (1 if act == "enable" else 0, a.name))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                raise LookupError(f"no schedule {a.name}")
            store.event(c, None, actor_of(a), f"schedule_{act}", {"name": a.name})
    rows = []
    for r in c.execute("SELECT * FROM schedules ORDER BY name"):
        d = dict(r)
        d["spec"], d["action"] = json.loads(d["spec"]), json.loads(d["action"])
        if d["kind"] == "window":
            d["active_now"] = bool(d["enabled"]) and R.window_active(d["spec"])
        rows.append(d)

    def txt(xs):
        lines = []
        for d in xs:
            sp = d["spec"]
            when = (f"{sp['from']}-{sp['to']}" if d["kind"] == "window" else fmt_ts(sp["at"]) if d["kind"] == "at" else f"daily {sp['time']}")
            if sp.get("days"):
                when += f" {sp['days']}"
            lines.append(f"{d['name']:<16} {d['kind']:<7} {when:<22} {json.dumps(d['action'])}"
                         + ("" if d["enabled"] else "  (disabled)") + ("  ACTIVE" if d.get("active_now") else "")
                         + (f"  last fired {fmt_ts(d['last_fired'])}" if d.get("last_fired") else ""))
        return "\n".join(lines) or "(no schedules)"
    out(rows, txt)


def cmd_config(c, a, out):
    act = a.action or "ls"
    if act in ("set", "unset"):
        admin(c, a)
    if act == "set":
        if a.key not in store.DEFAULTS and not a.key.startswith("cooldown."):
            raise ValueError(f"unknown key {a.key!r}; see `ab config ls`")
        val = store.coerce(a.value)
        with store.tx(c):
            store.cfg_set(c, a.key, val, actor_of(a))
        out({a.key: val}, f"{a.key} = {json.dumps(val)}")
    elif act == "unset":
        with store.tx(c):
            store.cfg_unset(c, a.key, actor_of(a))
        out({a.key: store.cfg(c, a.key)}, f"{a.key} reset to {json.dumps(store.cfg(c, a.key))}")
    elif act == "get":
        out({a.key: store.cfg(c, a.key)}, json.dumps(store.cfg(c, a.key)))
    else:
        allc = {k: v for k, v in store.cfg_all(c).items() if not k.startswith("_")}
        out(allc, lambda d: "\n".join(f"{k} = {json.dumps(v)}" for k, v in d.items()))



# ---- messaging
def mline(m):
    head = f"m{m['id']} {fmt_ts(m['ts'])} {m['board']}/{m['channel']} {m['thread']} {m['author']}"
    return head + ": " + (m["body"] or "").strip().replace("\n", "\n    ")


def cmd_post(c, a, out):
    mid, thread, eid = store.post_message(c, actor_of(a), text_arg(a.text), board=board_of(a), channel=a.channel, data=jarg(a.data))
    out({"msg": mid, "thread": thread, "event": eid}, thread)


def cmd_reply(c, a, out):
    data = jarg(a.data) or {}
    if a.sync:
        data["sync"] = True
    mid, thread, eid = store.post_message(c, actor_of(a), text_arg(a.text), thread=a.thread, data=data or None)
    out({"msg": mid, "thread": thread, "event": eid}, f"m{mid} in {thread}")


def cmd_read(c, a, out):
    me = actor_of(a)
    sql, args = "SELECT * FROM messages WHERE id > ?", [a.after or 0]
    if a.thread:
        _, _, _, th = store.resolve_thread(c, a.thread)
        sql += " AND thread=?"
        args.append(th)
    else:
        if not a.all_boards:
            sql += " AND board=?"
            args.append(board_of(a))
        if a.channel:
            sql += " AND (channel=? OR channel LIKE ?)"
            args += [a.channel, a.channel.rstrip("/") + "/%"]
        if a.roots:
            sql += " AND thread = 'm' || id"
    rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", args + [a.limit]).fetchall()[::-1]
    msgs = [store.msg_dict(r) for r in rows if store.can(c, me, "read", r["board"], r["channel"])]
    out(msgs, lambda xs: "\n".join(mline(m) for m in xs) or "(no messages)")


def cmd_ask(c, a, out):
    me = actor_of(a)
    if a.thread:
        mid, thread, eid = store.post_message(c, me, text_arg(a.text), thread=a.thread)
    else:
        if not a.channel:
            raise ValueError("give a CHANNEL or --thread")
        mid, thread, eid = store.post_message(c, me, text_arg(a.text), board=board_of(a), channel=a.channel)
    evs, cursor = A.wait_for(c, me, parse_dur(a.wait) or 0, after=eid, kinds=["message"], thread=thread, not_actor=me,
                             first_only=not a.all)
    replies = [store.msg_dict(c.execute("SELECT * FROM messages WHERE id=?", (e["payload"]["msg"],)).fetchone()) for e in evs]
    res = {"thread": thread, "asked": mid, "replies": replies, "cursor": cursor}
    out(res, lambda d: "\n".join(mline(m) for m in d["replies"]) or f"no reply in {thread} yet (asked as m{mid}; wait again with: ab wait --thread {thread} --after {cursor})")
    return 0 if replies else 3


def _wait_filters(c, a):
    me = actor_of(a)
    thread = None
    if a.thread:
        thread = store.resolve_thread(c, a.thread)[3]
    return dict(kinds=(a.kind.split(",") if a.kind else None), board=None if a.all_boards else board_of(a),
                channel=a.channel, thread=thread, task=a.task, mention=me if a.mentions else None,
                actor=a.from_actor, not_actor=None if a.include_self else me, text=a.text, label=a.label_change)


def ev_text(c):
    return lambda xs: "\n".join(A.describe_event(c, e) for e in xs)


def cmd_wait(c, a, out):
    me = actor_of(a)
    flt = _wait_filters(c, a)
    after = a.after if a.after is not None else None
    evs, cursor = A.wait_for(c, me, parse_dur(a.timeout) or 0, after=after, first_only=not a.all, **flt)
    out({"events": evs, "cursor": cursor}, lambda d: (ev_text(c)(d["events"]) if d["events"] else "timed out") + f"\ncursor {d['cursor']}")
    return 0 if evs else 3


def cmd_tail(c, a, out):
    me = actor_of(a)
    flt = _wait_filters(c, a)
    cursor = a.after if a.after is not None else max(0, A.max_event_id(c) - a.limit)
    deadline = now() + (parse_dur(a.follow) or 0) if a.follow else None
    while True:
        evs = A.events_after(c, cursor, board=flt["board"], limit=a.limit if not a.follow else 500)
        if evs:
            cursor = evs[-1]["id"]
        for e in evs:
            if A.match_event(e, **flt) and A.readable(c, me, e):
                print(json.dumps(e, default=str) if out.as_json else A.describe_event(c, e), flush=True)
        if deadline is None or now() >= deadline:
            break
        time.sleep(1)
    if not out.as_json:
        print(f"cursor {cursor}")


def cmd_inbox(c, a, out):
    msgs = A.inbox(c, actor_of(a), peek=a.peek, limit=a.limit)
    out(msgs, lambda xs: "\n".join(mline(m) for m in xs) or "(inbox empty)")


def cmd_followup(c, a, out):
    at = parse_time(a.at) if a.at else (now() + parse_dur(a.within) if a.within else None)
    tid = A.followup(c, actor_of(a), a.thread, text_arg(a.prompt), at, a.tier, a.harness, a.model, a.priority)
    out({"id": tid, "at": at}, f"{tid} (runs {'at ' + fmt_ts(at) if at else 'now'}, reports back into {a.thread})")


# ---- watchers
def cmd_watch(c, a, out):
    act = a.action or "ls"
    b = board_of(a)
    if act != "ls":
        admin(c, a, b)
    if act == "add":
        if not a.name:
            raise ValueError("watch add NAME ...")
        spec = {"kinds": [k.strip() for k in (a.on or "message").split(",") if k.strip()], "channel": a.channel,
                "mention": a.mention, "thread": store.resolve_thread(c, a.thread)[3] if a.thread else None,
                "actor": a.from_actor, "text": a.text, "label": a.label_change}
        if a.all_boards:
            spec["all_boards"] = True  # wake on events from every board; the task still lands on this board
        if a.text:
            import re as _re
            _re.compile(a.text)
        action = {"brief": text_arg(a.brief) if a.brief else "", "tier": a.tier, "harness": a.harness, "model": a.model,
                  "workdir": str(Path(a.workdir).expanduser().resolve()) if a.workdir else None, "channel": a.to_channel,
                  "cooldown": a.cooldown or "0", "reply": not a.no_reply, "coalesce": not a.no_coalesce, "priority": a.priority or 0}
        if a.tier and a.tier not in store.cfg(c, "tiers"):
            raise ValueError(f"unknown tier {a.tier}")
        start = A.max_event_id(c) if not a.from_start else 0
        with store.tx(c):
            c.execute("INSERT INTO watchers(board,name,spec,action,enabled,last_event_id,created_by,created_at) VALUES(?,?,?,?,1,?,?,?) "
                      "ON CONFLICT(board,name) DO UPDATE SET spec=excluded.spec, action=excluded.action, enabled=1",
                      (b, a.name, json.dumps(spec), json.dumps(action), start, actor_of(a), now()))
            store.event(c, None, actor_of(a), "watch_set", {"name": a.name, "spec": spec}, board=b)
    elif act in ("rm", "enable", "disable"):
        with store.tx(c):
            if act == "rm":
                c.execute("DELETE FROM watchers WHERE board=? AND name=?", (b, a.name))
            else:
                c.execute("UPDATE watchers SET enabled=? WHERE board=? AND name=?", (1 if act == "enable" else 0, b, a.name))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                raise LookupError(f"no watcher {a.name} on board {b}")
    rows = []
    for r in c.execute("SELECT * FROM watchers WHERE board=? ORDER BY name", (b,)):
        d = dict(r)
        d["spec"], d["action"] = json.loads(d["spec"]), json.loads(d["action"])
        rows.append(d)
    out(rows, lambda xs: "\n".join(
        f"{d['name']:<16} on {','.join(d['spec'].get('kinds') or ['*'])}"
        + "".join(f" {k}={v}" for k, v in d["spec"].items() if v and k != "kinds")
        + f"  -> tier={d['action'].get('tier') or 'default'} cooldown={d['action'].get('cooldown')} fired={d['fired']}"
        + ("" if d["enabled"] else " (disabled)") for d in xs) or f"(no watchers on board {b})")


# ---- GitHub sync
def cmd_sync(c, a, out):
    act = a.action or "ls"
    b = board_of(a)
    if act != "ls":
        admin(c, a, b)
    if act == "add":
        if not (a.name and a.github):
            raise ValueError("sync add NAME --github owner/repo")
        spec = {"repo": a.github, "label": None if a.all else (a.label or "agent"), "pulls": a.pulls, "channel": a.channel,
                "tier": a.tier, "workdir": str(Path(a.workdir).expanduser().resolve()) if a.workdir else None,
                "push": a.push, "close_on_done": a.close_on_done, "mirror_all": a.mirror_all, "manual": a.manual}
        with store.tx(c):
            c.execute("INSERT INTO syncs(board,name,kind,spec,enabled,created_by,created_at) VALUES(?,?,'github',?,1,?,?) "
                      "ON CONFLICT(board,name) DO UPDATE SET spec=excluded.spec, enabled=1",
                      (b, a.name, json.dumps(spec), actor_of(a), now()))
            store.event(c, None, actor_of(a), "sync_set", {"name": a.name, "spec": spec}, board=b)
    elif act in ("rm", "enable", "disable"):
        with store.tx(c):
            if act == "rm":
                c.execute("DELETE FROM syncs WHERE board=? AND name=?", (b, a.name))
            else:
                c.execute("UPDATE syncs SET enabled=? WHERE board=? AND name=?", (1 if act == "enable" else 0, b, a.name))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                raise LookupError(f"no sync {a.name} on board {b}")
    elif act == "run":
        res = A.run_syncs(c, only=a.name, force=True)
        out(res, lambda d: "\n".join(f"{k}: {json.dumps(v)}" for k, v in d.items()) or "(no enabled syncs)")
        return 1 if any("error" in v for v in res.values()) else 0
    rows = []
    for r in c.execute("SELECT * FROM syncs WHERE board=? ORDER BY name", (b,)):
        d = dict(r)
        d["spec"] = json.loads(d["spec"])
        d["items"] = len(json.loads(d["state"] or "{}").get("items", {}))
        del d["state"]
        rows.append(d)
    out(rows, lambda xs: "\n".join(
        f"{d['name']:<14} github {d['spec']['repo']} label={d['spec'].get('label') or '*'} pulls={d['spec'].get('pulls')} "
        f"push={d['spec'].get('push')} items={d['items']} last {fmt_ts(d['last_run'])}"
        + (f" ERROR {d['last_error'][:120]}" if d.get("last_error") else "") + ("" if d["enabled"] else " (disabled)")
        for d in xs) or f"(no syncs on board {b})")


# ---- grants
def cmd_grants(c, a, out):
    act = a.action or "ls"
    if act == "explain":
        who, perm = a.who or actor_of(a), a.perm or "read"
        ok, rules = store.explain(c, who, perm, board_of(a), a.channel)
        res = {"actor": who, "perm": perm, "board": board_of(a), "channel": a.channel, "allowed": ok, "decided_by": rules}
        out(res, lambda d: f"{'ALLOWED' if d['allowed'] else 'DENIED'}: {who} {perm} on {d['board']}:{d['channel'] or '*'}"
            + ("".join(f"\n  grant {r['id']}: board={r['board']} who={r['who']} channel={r['channel']} can={r['perms']}" for r in d["decided_by"])
               or "\n  (no grant matches: open by default)"))
        return 0 if ok else 1
    if act != "ls":
        admin(c, a, a.grant_board if a.grant_board not in (None, "*") else None)
    if act == "add":
        if not a.who or a.can is None:
            raise ValueError("grants add --who GLOB --can PERMS [--channel C] [--board B|*]")
        store.parse_perms(a.can)
        gb = a.grant_board or board_of(a)
        if gb != "*":
            store.require_board(c, gb)
        with store.tx(c):
            cur = c.execute("INSERT INTO grants(board,who,channel,perms,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                            (gb, a.who, a.channel or "*", a.can, a.note, actor_of(a), now()))
            store.event(c, None, actor_of(a), "grant_added", {"id": cur.lastrowid, "board": gb, "who": a.who, "channel": a.channel or "*", "perms": a.can})
    elif act == "rm":
        with store.tx(c):
            c.execute("DELETE FROM grants WHERE id=?", (int(a.who),))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                raise LookupError(f"no grant {a.who}")
            store.event(c, None, actor_of(a), "grant_removed", {"id": int(a.who)})
    rows = [dict(r) for r in c.execute("SELECT * FROM grants ORDER BY board, channel, who")]
    out(rows, lambda xs: "\n".join(f"{r['id']:>3}  board={r['board']:<10} who={r['who']:<14} channel={r['channel']:<14} can={r['perms']}"
                                   + (f"  # {r['note']}" if r.get("note") else "") for r in xs) or "(no grants: everything open)")


# ---- boards
def cmd_boards(c, a, out):
    act = a.action or "ls"
    if act == "create":
        admin(c, a)
        store.board_create(c, a.name, actor_of(a), a.description or "", a.max_slots, a.tier,
                           str(Path(a.workdir).expanduser().resolve()) if a.workdir else None)
    elif act == "set":
        admin(c, a, a.name)
        f = {}
        if a.description is not None:
            f["description"] = a.description
        if a.max_slots is not None:
            f["max_slots"] = a.max_slots if a.max_slots >= 0 else None
        if a.tier is not None:
            f["default_tier"] = a.tier or None
        if a.workdir is not None:
            f["default_workdir"] = str(Path(a.workdir).expanduser().resolve()) if a.workdir else None
        if a.pause:
            f["paused"] = 1
        if a.resume:
            f["paused"] = 0
        store.board_update(c, a.name, actor_of(a), **f)
    elif act == "rm":
        admin(c, a)
        store.board_delete(c, a.name, actor_of(a), a.force)
    rows = []
    for r in c.execute("SELECT * FROM boards ORDER BY name"):
        d = dict(r)
        d["counts"] = {x["status"]: x["n"] for x in c.execute("SELECT status, COUNT(*) n FROM tasks WHERE board=? GROUP BY status", (r["name"],))}
        rows.append(d)
    out(rows, lambda xs: "\n".join(
        f"{d['name']:<14} {'PAUSED ' if d['paused'] else ''}max_slots={d['max_slots'] if d['max_slots'] is not None else '-'} "
        f"tier={d['default_tier'] or '-'} " + " ".join(f"{k}={v}" for k, v in sorted(d["counts"].items()))
        + (f"  {d['description']}" if d["description"] else "") for d in xs))


# ---- endpoints (OpenAI-compatible servers any endpoint-capable harness can run on)
def _ep_models(base_url, key):
    import urllib.request
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    return [m.get("id") for m in d.get("data", []) if m.get("id")]


def cmd_endpoints(c, a, out):
    act = a.action or "ls"
    eps = store.cfg(c, "endpoints") or {}
    if act in ("add", "rm"):
        admin(c, a)
        if not a.name:
            raise ValueError(f"endpoints {act} NAME ...")
        if act == "add":
            if not a.base_url:
                raise ValueError("endpoints add NAME --base-url http://host:port/v1 [--api-key-env VAR] [--context N]")
            eps[a.name] = {"base_url": a.base_url.rstrip("/"), "api_key_env": a.api_key_env, "context": a.context,
                           "bucket": a.bucket}
        else:
            if eps.pop(a.name, None) is None:
                raise LookupError(f"no endpoint {a.name}")
        with store.tx(c):
            store.cfg_set(c, "endpoints", eps, actor_of(a))
    if act == "check":
        res = {}
        for name, e in eps.items():
            if a.name and name != a.name:
                continue
            ep = H.get_endpoint(c, name)
            try:
                res[name] = {"ok": True, "models": _ep_models(ep.base_url, ep.key)}
            except Exception as ex:
                res[name] = {"ok": False, "error": str(ex)[:200]}
        out(res, lambda d: "\n".join(f"{k}: " + ("up, serves " + ", ".join(v["models"]) if v["ok"] else "DOWN " + v["error"])
                                     for k, v in d.items()) or "(no endpoints)")
        return 0 if all(v["ok"] for v in res.values()) else 1
    capable = [n for n, ad in H.ADAPTERS.items() if type(ad).endpoint_command is not H.Adapter.endpoint_command]
    res = {"endpoints": eps, "harnesses": capable}
    out(res, lambda d: "\n".join(f"{n}: {e['base_url']} context={e.get('context')}"
                                 + (f" key=${e['api_key_env']}" if e.get("api_key_env") else "")
                                 + f" bucket={e.get('bucket') or 'ep:' + n}" for n, e in d["endpoints"].items())
        + ("\n" if d["endpoints"] else "(no endpoints)\n") + "harnesses that can run on endpoints: " + ", ".join(d["harnesses"])
        + "\nuse in tiers / pins as  HARNESS@ENDPOINT:MODEL  e.g. pi@rtx3060:spark-1.7b")


def cmd_mcp(c, a, out):
    from . import mcp
    mcp.serve()


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(prog="ab", description="agentboard: a task board for AI agents (no humans in the loop).")
    p.add_argument("--json", action="store_true", default=os.environ.get("AB_JSON") == "1", help="machine-readable output")
    p.add_argument("--as", dest="actor", help="who you are (default $AB_ACTOR)")
    p.add_argument("--board", "-B", help="board name (default $AB_BOARD or main)")
    sp = p.add_subparsers(dest="cmd", required=True)

    def sub(name, fn, help_, aliases=()):
        s = sp.add_parser(name, help=help_, aliases=list(aliases))
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("--as", dest="actor", default=argparse.SUPPRESS)
        s.add_argument("--board", "-B", default=argparse.SUPPRESS)
        return s

    s = sub("add", cmd_add, "create a task")
    s.add_argument("title")
    s.add_argument("--body", "-b", help="description ('-' reads stdin)")
    s.add_argument("--body-file")
    s.add_argument("--channel", "-c", default="main")
    s.add_argument("--label", "-l", action="append", help="key=value (repeatable)")
    s.add_argument("--priority", "-p", type=int, default=0)
    s.add_argument("--dep", "-d", action="append", help="task id that must be done first (repeatable)")
    s.add_argument("--after", help="not before: +2h | 14:30 | 2026-11-20 03:00")
    s.add_argument("--workdir", "-w")
    s.add_argument("--tier", "-t", help="intelligence tier (see `ab tiers`)")
    s.add_argument("--harness", help="pin a harness (skips tier routing)")
    s.add_argument("--model", help="pin a model (with --harness)")
    s.add_argument("--manual", action="store_true", help="runner won't start it; an agent claims it by hand")
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--timeout", help="per-run wall clock, e.g. 45m")
    s.add_argument("--data", help="JSON (or @file)")
    s.add_argument("--reply-to", help="thread (task id or m<id>) that gets this task's result posted into it")

    s = sub("ls", cmd_ls, "list tasks (default: open+claimed)", ["list"])
    s.add_argument("--status", "-s", help="open,claimed,done,dead,cancelled or all")
    s.add_argument("--ready", "-r", action="store_true", help="only claimable now")
    s.add_argument("--channel", "-c")
    s.add_argument("--label", "-l", action="append")
    s.add_argument("--limit", "-n", type=int, default=50)
    s.add_argument("--all-boards", action="store_true")

    s = sub("show", cmd_show, "full task: body, notes, runs, result")
    s.add_argument("id")

    s = sub("claim", cmd_claim, "claim the next ready task (or a given id); exit 3 if none")
    s.add_argument("id", nargs="?")
    s.add_argument("--channel", "-c")
    s.add_argument("--label", "-l", action="append")
    s.add_argument("--lease", help="default 30m; renew before it runs out")
    s.add_argument("--wait", help="keep trying this long, e.g. 10m")

    s = sub("renew", cmd_renew, "extend your lease")
    s.add_argument("id")
    s.add_argument("--lease")
    s.add_argument("--force", action="store_true")

    s = sub("note", cmd_note, "append a note to a task thread")
    s.add_argument("id")
    s.add_argument("text", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--file", help="read the note from this file (avoids shell quoting of $ and backticks)")
    s.add_argument("--data", help="JSON (or @file)")

    s = sub("done", cmd_done, "complete your task", ["complete"])
    s.add_argument("id")
    s.add_argument("result", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--summary-file", help="read the result from this file (avoids shell quoting of $ and backticks)")
    s.add_argument("--data")
    s.add_argument("--force", action="store_true")

    s = sub("fail", cmd_fail, "give up this attempt (task retries until max attempts, then dead)")
    s.add_argument("id")
    s.add_argument("why", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--why-file", help="read the reason from this file")
    s.add_argument("--force", action="store_true")

    s = sub("release", cmd_release, "hand the task back without counting an attempt")
    s.add_argument("id")
    s.add_argument("why", nargs="?")
    s.add_argument("--force", action="store_true")

    s = sub("defer", cmd_defer, "park a task until a time and/or other tasks are done")
    s.add_argument("id")
    s.add_argument("--until")
    s.add_argument("--after", dest="after_task", action="append", help="task id (repeatable)")
    s.add_argument("--why")
    s.add_argument("--force", action="store_true")

    s = sub("cancel", cmd_cancel, "cancel a task (kills its run if one is active)")
    s.add_argument("id")
    s.add_argument("why", nargs="?")

    s = sub("retry", cmd_retry, "reopen a dead/failed/cancelled/done task with fresh attempts")
    s.add_argument("id")

    s = sub("edit", cmd_edit, "change task fields")
    s.add_argument("id")
    for k in ("title", "channel", "tier", "harness", "model", "workdir", "after", "timeout"):
        s.add_argument(f"--{k}")
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.add_argument("--priority", type=int)
    s.add_argument("--max-attempts", type=int)
    s.add_argument("--label", "-l", action="append", help="key=value, or key= to remove")
    s.add_argument("--dep", action="append")
    s.add_argument("--rm-dep", action="append")
    s.add_argument("--manual", action="store_true")
    s.add_argument("--auto", action="store_true")
    s.add_argument("--move-to", help="move the task to another board")

    s = sub("log", cmd_log, "event log (all, or one task)")
    s.add_argument("id", nargs="?")
    s.add_argument("--limit", "-n", type=int, default=40)

    sub("whoami", cmd_whoami, "your actor name and task")
    sub("guide", cmd_guide, "print the agent instructions")

    s = sub("usage", cmd_usage, "token usage by bucket (5h/24h/7d), harness, model, source or task")
    s.add_argument("--by", choices=["bucket", "harness", "model", "source", "task"], default="bucket")
    s.add_argument("--since", help="e.g. 5h, 1d, 7d")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--no-harvest", action="store_true")

    s = sub("harvest", cmd_harvest, "read new usage from harness logs now")
    s.add_argument("--only", help="claude,opencode,hermes,pi")

    s = sub("limits", cmd_limits, "active cooldowns: ls | set SCOPE --until T | clear SCOPE")
    s.add_argument("action", nargs="?", choices=["ls", "set", "clear"])
    s.add_argument("scope", nargs="?", help="bucket:NAME or harness:NAME")
    s.add_argument("--until", default="+1h")
    s.add_argument("--kind")
    s.add_argument("--reason")

    s = sub("caps", cmd_caps, "optional soft caps per bucket")
    s.add_argument("action", nargs="?", choices=["ls", "set", "clear"])
    s.add_argument("bucket", nargs="?")
    s.add_argument("--window", help="rolling window, default 5h")
    s.add_argument("--max-output", type=float)
    s.add_argument("--max-cost", type=float)
    s.add_argument("--max-runs", type=float)
    s.add_argument("--max-calls", type=float)
    s.add_argument("--max-5h-pct", type=float, help="pause the bucket at this %% of the provider's 5h window (claude-sub only)")
    s.add_argument("--max-7d-pct", type=float, help="pause the bucket at this %% of the provider's weekly limit (claude-sub only)")
    s.add_argument("--pace-7d-lead-h", type=float, help="spread the weekly limit evenly per day so it is used up this many "
                   "hours before the weekly reset; unused days carry over (claude-sub only)")

    sub("harness", cmd_harness, "installed harnesses and their state", ["harnesses"])

    s = sub("models", cmd_models, "models each harness offers right now")
    s.add_argument("harness", nargs="?")
    s.add_argument("--refresh", action="store_true")

    s = sub("tiers", cmd_tiers, "intelligence tiers: ls | set T SPEC... | add T SPEC [--pos N] | rm T [SPEC|IDX] | order a,b,c | check")
    s.add_argument("action", nargs="?", choices=["ls", "set", "add", "rm", "order", "check"])
    s.add_argument("tier", nargs="?")
    s.add_argument("specs", nargs="*", help="harness:model@effort, e.g. claude:sonnet@high")
    s.add_argument("--pos", type=int)

    s = sub("runner", cmd_runner, "start | stop [--now] | reload | pause | resume | drain | slots N | status | run | service [--enable]")
    s.add_argument("action", nargs="?", choices=["start", "stop", "reload", "pause", "resume", "drain", "slots", "status", "run", "service"])
    s.add_argument("value", nargs="?")
    s.add_argument("--slots", type=int)
    s.add_argument("--now", action="store_true")
    s.add_argument("--enable", action="store_true")

    s = sub("sched", cmd_sched, "schedules: ls | add NAME (--window A-B --slots N | --at T --do X | --daily HH:MM --do X) | rm|enable|disable NAME")
    s.add_argument("action", nargs="?", choices=["ls", "add", "rm", "enable", "disable"])
    s.add_argument("name", nargs="?")
    s.add_argument("--window")
    s.add_argument("--at")
    s.add_argument("--daily")
    s.add_argument("--days", help="mon-fri, sat,sun ...")
    s.add_argument("--slots", type=int)
    s.add_argument("--do", help="pause|resume|drain|stop|slots=N|set:key=value")

    s = sub("config", cmd_config, "runtime settings: ls | get KEY | set KEY VALUE | unset KEY")
    s.add_argument("action", nargs="?", choices=["ls", "get", "set", "unset"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")


    s = sub("post", cmd_post, "start a thread in a channel (mention agents with @name)")
    s.add_argument("channel")
    s.add_argument("text", help="'-' reads stdin")
    s.add_argument("--data", help="JSON (or @file)")

    s = sub("reply", cmd_reply, "reply in a thread (task id or m<id>)")
    s.add_argument("thread")
    s.add_argument("text", help="'-' reads stdin")
    s.add_argument("--data")
    s.add_argument("--sync", action="store_true", help="also mirror to the synced GitHub issue/PR")

    s = sub("read", cmd_read, "read messages: a channel, a thread, or the whole board")
    s.add_argument("channel", nargs="?")
    s.add_argument("--thread", "-t")
    s.add_argument("--after", type=int, help="only messages with id > this")
    s.add_argument("--roots", action="store_true", help="only thread starters")
    s.add_argument("--limit", "-n", type=int, default=30)
    s.add_argument("--all-boards", action="store_true")

    s = sub("ask", cmd_ask, "post a question and block until someone else replies (exit 3 on timeout)")
    s.add_argument("channel", nargs="?")
    s.add_argument("text")
    s.add_argument("--thread", "-t", help="ask inside an existing thread instead")
    s.add_argument("--wait", default="10m")
    s.add_argument("--all", action="store_true", help="return every reply that arrived, not just the first")

    def waitflags(s):
        s.add_argument("--mentions", action="store_true", help="only messages that @mention you")
        s.add_argument("--thread", "-t")
        s.add_argument("--channel", "-c")
        s.add_argument("--task")
        s.add_argument("--kind", "-k", help="event kinds, e.g. message,done,dead,label,created")
        s.add_argument("--from", dest="from_actor", help="actor glob")
        s.add_argument("--text", help="regex on the event payload")
        s.add_argument("--label-change", help="k or k=v (label events)")
        s.add_argument("--include-self", action="store_true")
        s.add_argument("--after", type=int, help="event cursor (default: now)")
        s.add_argument("--all-boards", action="store_true")

    s = sub("wait", cmd_wait, "block until a matching event arrives (exit 3 on timeout); prints a cursor")
    waitflags(s)
    s.add_argument("--timeout", default="10m")
    s.add_argument("--all", action="store_true", help="return all matching events up to the cursor, not just the first")

    s = sub("tail", cmd_tail, "print recent events; --follow DUR keeps streaming")
    waitflags(s)
    s.add_argument("--follow", help="keep streaming for this long, e.g. 30m")
    s.add_argument("--limit", "-n", type=int, default=30)

    s = sub("inbox", cmd_inbox, "messages that mention you or answer your threads/tasks (advances your cursor)")
    s.add_argument("--peek", action="store_true")
    s.add_argument("--limit", "-n", type=int, default=50)

    s = sub("followup", cmd_followup, "schedule an agent to follow up on a thread later; its result posts back into it")
    s.add_argument("thread")
    s.add_argument("prompt")
    s.add_argument("--in", dest="within", help="e.g. 5m")
    s.add_argument("--at", help="+2h | 14:30 | ISO")
    s.add_argument("--tier")
    s.add_argument("--harness")
    s.add_argument("--model")
    s.add_argument("--priority", type=int, default=0)

    s = sub("watch", cmd_watch, "watchers wake an agent on events: ls | add NAME --on KINDS [filters] --brief TEXT | rm|enable|disable NAME")
    s.add_argument("action", nargs="?", choices=["ls", "add", "rm", "enable", "disable"])
    s.add_argument("name", nargs="?")
    s.add_argument("--on", help="event kinds, default message (also: created,done,dead,failed,label,claimed,cancelled)")
    s.add_argument("--channel", "-c")
    s.add_argument("--mention", help="only messages mentioning this name (glob)")
    s.add_argument("--thread", "-t")
    s.add_argument("--from", dest="from_actor", help="actor glob")
    s.add_argument("--text", help="regex on the event payload")
    s.add_argument("--label-change", help="k or k=v")
    s.add_argument("--brief", help="instructions for the woken agent ('-' reads stdin)")
    s.add_argument("--tier")
    s.add_argument("--harness")
    s.add_argument("--model")
    s.add_argument("--workdir")
    s.add_argument("--to-channel", help="channel for the created task (default: the event's)")
    s.add_argument("--cooldown", help="min time between wake-ups, e.g. 1m")
    s.add_argument("--priority", type=int)
    s.add_argument("--no-reply", action="store_true", help="don't post the result back into the event's thread")
    s.add_argument("--no-coalesce", action="store_true", help="a new task per wake-up even if one is still open")
    s.add_argument("--from-start", action="store_true", help="also match events from before the watcher existed")
    s.add_argument("--all-boards", action="store_true", help="match events on every board (the woken task is created on this board)")

    s = sub("sync", cmd_sync, "GitHub sync: ls | add NAME --github owner/repo [...] | run [NAME] | rm|enable|disable NAME")
    s.add_argument("action", nargs="?", choices=["ls", "add", "run", "rm", "enable", "disable"])
    s.add_argument("name", nargs="?")
    s.add_argument("--github", help="owner/repo")
    s.add_argument("--label", help="only issues/PRs with this label (default: agent)")
    s.add_argument("--all", action="store_true", help="every open issue, no label filter")
    s.add_argument("--pulls", action="store_true", help="also sync pull requests")
    s.add_argument("--channel", "-c", help="default gh/<repo>")
    s.add_argument("--tier")
    s.add_argument("--workdir", help="where agents work on these (e.g. the repo checkout)")
    s.add_argument("--manual", action="store_true", help="imported tasks wait for an agent to claim them")
    s.add_argument("--push", action="store_true", help="post results and --sync replies back to GitHub (off by default)")
    s.add_argument("--close-on-done", action="store_true", help="close the issue when its task is done (needs --push)")
    s.add_argument("--mirror-all", action="store_true", help="push every thread message, not just --sync ones")

    s = sub("grants", cmd_grants, "access: ls | add --who GLOB --can PERMS [--channel C] [--grant-board B|*] | rm ID | explain [ACTOR] [PERM]")
    s.add_argument("action", nargs="?", choices=["ls", "add", "rm", "explain"])
    s.add_argument("who", nargs="?", help="actor glob (add) / grant id (rm) / actor (explain)")
    s.add_argument("perm", nargs="?", help="explain: read|post|create|claim|manage|admin")
    s.add_argument("--who", dest="who_flag")
    s.add_argument("--can", help="read,post,create,claim,manage,admin | all | none")
    s.add_argument("--channel", "-c", help="channel prefix, default *")
    s.add_argument("--grant-board", help="board the grant applies to, or * for all (default: current board)")
    s.add_argument("--note")

    s = sub("boards", cmd_boards, "boards: ls | create NAME | set NAME [--pause|--resume|--max-slots N|--tier T|--workdir D] | rm NAME [--force]")
    s.add_argument("action", nargs="?", choices=["ls", "create", "set", "rm"])
    s.add_argument("name", nargs="?")
    s.add_argument("--description")
    s.add_argument("--max-slots", type=int, help="max parallel agents for this board (-1 = no cap)")
    s.add_argument("--tier", help="default tier for new tasks on this board")
    s.add_argument("--workdir", help="default workdir for new tasks on this board")
    s.add_argument("--pause", action="store_true")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--force", action="store_true")

    s = sub("endpoints", cmd_endpoints, "OpenAI-compatible servers: ls | add NAME --base-url URL [--api-key-env VAR] [--context N] | rm NAME | check [NAME]")
    s.add_argument("action", nargs="?", choices=["ls", "add", "rm", "check"])
    s.add_argument("name", nargs="?")
    s.add_argument("--base-url", help="e.g. http://127.0.0.1:8093/v1")
    s.add_argument("--api-key-env", help="name of the env var holding the key (the key itself is never stored)")
    s.add_argument("--context", type=int, default=65536, help="context window to tell the harness")
    s.add_argument("--bucket", help="quota bucket name (default ep:NAME)")

    sub("mcp", cmd_mcp, "serve the board as an MCP server on stdio")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    for k in ("actor", "board"):
        if not hasattr(a, k):
            setattr(a, k, None)
    if getattr(a, "who_flag", None):
        a.who = a.who_flag
    out = Out(getattr(a, "json", False))
    conn = store.connect()
    try:
        rc = a.fn(conn, a, out)
        return rc or 0
    except (LookupError, PermissionError, ValueError, KeyError) as e:
        msg = str(e).strip("'\"")
        if out.as_json:
            print(json.dumps({"error": msg, "type": type(e).__name__}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    finally:
        conn.close()
