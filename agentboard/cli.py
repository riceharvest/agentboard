"""`ab` - agent-only task board. Every command takes --json for machine output."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import harness as H
from . import runner as R
from . import store, usage
from .store import fmt_dur, fmt_ts, now, parse_dur, parse_time

GUIDE = Path(__file__).resolve().parent.parent / "AGENT.md"


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
             f"state={d['state']} status={d['status']} channel={d['channel']} priority={d['priority']} tier={d['tier']}"
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
        lines += [f"[{fmt_ts(n['ts'])} {n['author']}] {n['body']}" for n in d["notes"]]
    if d.get("runs"):
        lines.append("")
        for r in d["runs"]:
            lines.append(f"run {r['id']} {r['harness']}:{r['model'] or ''} [{r['bucket']}] {fmt_ts(r['started_at'])} "
                         f"{fmt_dur((r['ended_at'] or now()) - r['started_at'])} -> {r['outcome'] or 'running'} "
                         f"in={r['input']} out={r['output']} cache_r={r['cache_read']}"
                         + (f" ${r['cost_usd']:.3f}" if r.get("cost_usd") else ""))
    return "\n".join(lines)


def actor_of(a):
    return a.actor or os.environ.get("AB_ACTOR") or "agent"


# ---------------------------------------------------------------- commands
def cmd_add(c, a, out):
    workdir = a.workdir
    if workdir is None and os.environ.get("AB_TASK"):
        workdir = os.getcwd()  # sub-tasks created inside a run inherit the run's workdir
    if workdir:
        workdir = str(Path(workdir).expanduser().resolve())
    tid = store.add_task(c, actor_of(a), a.title, body_arg(a), a.channel, kv_list(a.label), a.priority,
                         [store.get_task(c, d)["id"] if store.get_task(c, d) else d for d in (a.dep or [])],
                         parse_time(a.after) if a.after else None, workdir, a.tier, a.harness, a.model,
                         not a.manual, a.max_attempts, parse_dur(a.timeout) if a.timeout else None, jarg(a.data))
    out({"id": tid}, tid)


def cmd_ls(c, a, out):
    store.reap(c)
    sql, args = "SELECT * FROM tasks", []
    where = []
    status = a.status
    if a.ready:
        rows = store.ready_tasks(c, a.channel, kv_list(a.label), limit=a.limit)
    else:
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
    items = [store.task_dict(c, r) for r in rows]
    out(items, lambda xs: "\n".join(tline(x) for x in xs) or "(nothing)")


def cmd_show(c, a, out):
    row = store.get_task(c, a.id)
    if row is None:
        raise LookupError(f"no task {a.id}")
    out(store.task_dict(c, row, full=True), show_text)


def cmd_claim(c, a, out):
    deadline = now() + (parse_dur(a.wait) or 0)
    while True:
        tid, why = store.claim(c, actor_of(a), a.id, a.channel, kv_list(a.label), parse_dur(a.lease) if a.lease else None)
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
    text = a.text if a.text != "-" else sys.stdin.read()
    tid = store.note(c, a.id, actor_of(a), text, jarg(a.data))
    out({"id": tid, "noted": True}, f"{tid} noted")


def cmd_done(c, a, out):
    text = a.result if a.result != "-" else sys.stdin.read()
    tid = store.complete(c, a.id, actor_of(a), text or "", jarg(a.data), a.force)
    out({"id": tid, "status": "done"}, f"{tid} done")


def cmd_fail(c, a, out):
    tid, state = store.fail(c, a.id, actor_of(a), a.why, a.force)
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
    for k in ("title", "channel", "priority", "tier", "harness", "model", "max_attempts"):
        v = getattr(a, k)
        if v is not None:
            f[k] = v if v != "" else None
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
    rows = [dict(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", args + [a.limit]).fetchall()][::-1]
    for r in rows:
        r["payload"] = json.loads(r["payload"]) if r["payload"] else None
    out(rows, lambda xs: "\n".join(f"{fmt_ts(r['ts'])} {r['task_id'] or '-':<11} {r['actor']:<16} {r['kind']:<14} "
                                   f"{json.dumps(r['payload']) if r['payload'] else ''}"[:220] for r in xs))


def cmd_whoami(c, a, out):
    d = {"actor": actor_of(a), "task": os.environ.get("AB_TASK"), "run": os.environ.get("AB_RUN"), "board": str(store.DB_PATH)}
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
        until = parse_time(a.until)
        with store.tx(c):
            store.set_limit(c, a.scope, a.kind or "manual", until, a.reason or "set by agent", actor_of(a))
        out({"scope": a.scope, "until": until}, f"{a.scope} limited until {fmt_ts(until)}")
    elif act == "clear":
        with store.tx(c):
            store.clear_limit(c, a.scope, actor_of(a))
        out({"scope": a.scope, "cleared": True}, f"{a.scope} cleared")


def cmd_caps(c, a, out):
    caps = store.cfg(c, "caps") or {}
    if a.action == "set":
        cap = {"window": a.window or "5h"}
        for k in ("max_output", "max_cost", "max_runs", "max_calls"):
            v = getattr(a, k)
            if v is not None:
                cap[k] = v
        caps[a.bucket] = cap
    elif a.action == "clear":
        caps.pop(a.bucket, None)
    if a.action in ("set", "clear"):
        with store.tx(c):
            store.cfg_set(c, "caps", caps, actor_of(a))
    out(caps, lambda d: "\n".join(f"{k}: {json.dumps(v)}" for k, v in d.items()) or "(no caps: only real limit errors stop a bucket)")


# ---- harnesses / models / tiers
def cmd_harness(c, a, out):
    lim = store.active_limits(c)
    rows = []
    for name, ad in H.ADAPTERS.items():
        rows.append({"harness": name, "installed": ad.available(), "limit": lim.get(f"harness:{name}"),
                     "harvested_usage": ad.harvested})
    out(rows, lambda xs: "\n".join(f"{r['harness']:<9} {'installed' if r['installed'] else 'missing':<10} "
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
            out({"started": False, "running": True, "pid": st["pid"]}, f"runner already running (pid {st['pid']}); unpaused")
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
    sets = {"pause": {"paused": True}, "resume": {"paused": False, "draining": False}, "drain": {"draining": True},
            "stop": {"stop": "now" if a.now else "graceful"}}
    if act in sets:
        with store.tx(c):
            for k, v in sets[act].items():
                store.cfg_set(c, k, v, actor)
        out({"ok": True, "action": act}, f"runner: {act}" + (" (now)" if act == "stop" and a.now else ""))
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
    if s in ("pause", "resume", "drain", "stop"):
        return {"do": s}
    if s.startswith("slots="):
        return {"do": "slots", "value": int(s.split("=", 1)[1])}
    if s.startswith("set:") and "=" in s:
        k, v = s[4:].split("=", 1)
        return {"do": "set", "key": k, "value": store.coerce(v)}
    raise ValueError(f"bad action {s!r}: use pause|resume|drain|stop|slots=N|set:key=value")


def cmd_sched(c, a, out):
    act = a.action or "ls"
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


def cmd_mcp(c, a, out):
    from . import mcp
    mcp.serve()


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(prog="ab", description="agentboard: a task board for AI agents (no humans in the loop).")
    p.add_argument("--json", action="store_true", default=os.environ.get("AB_JSON") == "1", help="machine-readable output")
    p.add_argument("--as", dest="actor", help="who you are (default $AB_ACTOR)")
    sp = p.add_subparsers(dest="cmd", required=True)

    def sub(name, fn, help_, aliases=()):
        s = sp.add_parser(name, help=help_, aliases=list(aliases))
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("--as", dest="actor", default=argparse.SUPPRESS)
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

    s = sub("ls", cmd_ls, "list tasks (default: open+claimed)", ["list"])
    s.add_argument("--status", "-s", help="open,claimed,done,dead,cancelled or all")
    s.add_argument("--ready", "-r", action="store_true", help="only claimable now")
    s.add_argument("--channel", "-c")
    s.add_argument("--label", "-l", action="append")
    s.add_argument("--limit", "-n", type=int, default=50)

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
    s.add_argument("text", help="'-' reads stdin")
    s.add_argument("--data", help="JSON (or @file)")

    s = sub("done", cmd_done, "complete your task", ["complete"])
    s.add_argument("id")
    s.add_argument("result", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--data")
    s.add_argument("--force", action="store_true")

    s = sub("fail", cmd_fail, "give up this attempt (task retries until max attempts, then dead)")
    s.add_argument("id")
    s.add_argument("why")
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

    sub("harness", cmd_harness, "installed harnesses and their state", ["harnesses"])

    s = sub("models", cmd_models, "models each harness offers right now")
    s.add_argument("harness", nargs="?")
    s.add_argument("--refresh", action="store_true")

    s = sub("tiers", cmd_tiers, "intelligence tiers: ls | set T SPEC... | add T SPEC [--pos N] | rm T [SPEC|IDX] | order a,b,c | check")
    s.add_argument("action", nargs="?", choices=["ls", "set", "add", "rm", "order", "check"])
    s.add_argument("tier", nargs="?")
    s.add_argument("specs", nargs="*", help="harness:model@effort, e.g. claude:sonnet@high")
    s.add_argument("--pos", type=int)

    s = sub("runner", cmd_runner, "start | stop [--now] | pause | resume | drain | slots N | status | run | service [--enable]")
    s.add_argument("action", nargs="?", choices=["start", "stop", "pause", "resume", "drain", "slots", "status", "run", "service"])
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

    sub("mcp", cmd_mcp, "serve the board as an MCP server on stdio")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not hasattr(a, "actor"):
        a.actor = None
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
