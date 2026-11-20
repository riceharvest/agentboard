"""`ab` - agent-only task board. Every command takes --json for machine output."""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import runner as R
from . import snapshot as S
from . import store, usage
from ._lazy import Lazy
from .store import fmt_dur, fmt_ts, now, parse_dur, parse_time

A, H, subprocess = Lazy("automation"), Lazy("harness"), Lazy("subprocess", absolute=True)   # imported when a command needs them
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
    return text_arg(getattr(a, "body", None), getattr(a, "body_file", None))


def text_arg(value, path=None):
    """Text from a --FILE option, '-' (stdin, also as the file path) or the plain argument
    (avoids shell quoting of $ and backticks)."""
    if path == "-":
        return sys.stdin.read()
    if path:
        try:
            return Path(path).expanduser().read_text()
        except OSError as e:
            raise ValueError(f"cannot read file {path!r}: {e.strerror or e}") from None
    return sys.stdin.read() if value == "-" else (value or "")


def tline(d):
    lab = ",".join(f"{k}={v}" for k, v in (d.get("labels") or {}).items())
    extra = []
    if d.get("owner"):
        extra.append(f"by {d['owner']}")
    if d["state"] == "waiting" and d.get("not_before") and d["not_before"] > now():
        extra.append(f"after {fmt_ts(d['not_before'])}")
    if d.get("park") and not d["park"].get("woken_at"):
        p = d["park"]
        extra.append("parked: " + (f"pid {p['pid']}" if p.get("pid") else f"file {p.get('file')}"))
    if d.get("wait_reason"):
        extra.append(d["wait_reason"])
    if d.get("deps"):
        extra.append("deps " + ",".join(d["deps"]))
    if d.get("attempts"):
        extra.append(f"try {d['attempts']}/{d['max_attempts']}")
    return (f"{d['id']}  {d['state']:<9} p{d['priority']:<3} {'cmd' if d.get('cmd') else d['tier']:<6} {d['channel']:<12} {d['title'][:70]}"
            + (f"  [{lab}]" if lab else "") + (f"  ({'; '.join(extra)})" if extra else ""))


def show_text(d):
    lines = [f"{d['id']}  {d['title']}",
             f"state={d['state']} status={d['status']} board={d['board']} channel={d['channel']} priority={d['priority']} tier={d['tier']}"
             + (f" pin={d['harness']}:{d.get('model') or ''}" if d.get("harness") else "") + (" kind=cmd" if d.get("cmd") else "")
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
    if d.get("park"):
        p = d["park"]
        if p.get("woken_at"):
            lines.append(f"park woken {fmt_ts(p['woken_at'])} ({p.get('woke') or 'condition met'}); note: {p.get('note')}")
        else:
            lines.append(f"PARKED since {fmt_ts(p.get('at'))} on {store.park_cond(p)}; note: {p.get('note')}")
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
    if d.get("cmd"):
        lines.insert(2, f"cmd: {d['cmd']}")
    if d.get("verify"):
        lines.insert(2, f"verify: {d['verify']}")
    if d.get("runs"):
        lines.append("")
        for r in d["runs"]:
            if r["harness"] == "cmd":
                lines.append(f"run {r['id']} cmd {fmt_ts(r['started_at'])} {fmt_dur((r['ended_at'] or now()) - r['started_at'])} -> "
                             f"{r['outcome'] or 'running'}" + (f" exit={r['exit_code']}" if r.get("exit_code") is not None else ""))
                continue
            lines.append(f"run {r['id']} {r['harness']}{'/' + r['provider'] if r.get('provider') else ''}{'@' + r['endpoint'] if r.get('endpoint') else ''}:{r['model'] or ''} [{r['bucket']}] {fmt_ts(r['started_at'])} "
                         f"{fmt_dur((r['ended_at'] or now()) - r['started_at'])} -> {r['outcome'] or 'running'} "
                         f"in={r['input']} out={r['output']} cache_r={r['cache_read']}"
                         + (f" ${r['cost_usd']:.3f}" if r.get("cost_usd") else ""))
            for kept in ("hermes-agent.log", "hermes-session.jsonl"):
                f = store.HOME / "runs" / f"{r['id']}.{kept}"
                if f.is_file():
                    lines.append(f"  kept {f} ({f.stat().st_size} bytes)")
    return "\n".join(lines)


def actor_of(a):
    # Inside a runner-launched agent the identity is fixed: a worker can't promote itself with --as.
    if os.environ.get("AB_RUN") and os.environ.get("AB_ACTOR"):
        return os.environ["AB_ACTOR"]
    return a.actor or os.environ.get("AB_ACTOR") or "agent"


def board_of(a):
    return getattr(a, "board", None) or os.environ.get("AB_BOARD") or store.DEFAULT_BOARD


# ---- run-home identity: a worker terminal may lack AB_TASK/AB_ACTOR/AB_BOARD (the shared Hermes gateway never passes the
# runner's per-run env to its terminal tool). A shell whose HERMES_HOME or cwd is <root>/profiles/ab-run-N, with a marker
# the runner wrote into that home, takes its identity from live run N, and only from that run.
RUN_HOME_RE = re.compile(r"(?:^|/)profiles/ab-run-(\d+)(?=/|$)")
RUN_MARKER = "ab-run.json"
TASK_SLOT = {"cmd_note": "text", "cmd_done": "result", "cmd_fail": "why"}   # where the free text goes
TEXT_FILE_OPTS = ("file", "summary_file", "why_file")


def infer_run(c, board=None, env=None, cwd=None):
    """(ctx, reason). ctx = {task, actor, board, run} when this shell is a run home of a live run; reason explains a refusal
    (None when this shell is not a run home at all). Never writes."""
    env = os.environ if env is None else env
    cwd = os.getcwd() if cwd is None else cwd
    hits = {}
    for src, p in (("HERMES_HOME", env.get("HERMES_HOME")), ("cwd", cwd)):
        if not p:
            continue
        full = str(Path(p).expanduser().resolve())
        matches = list(RUN_HOME_RE.finditer(full))
        if matches:
            m = matches[-1]  # innermost: a home may be nested inside another profile (e.g. TMPDIR scratch dirs)
            hits[src] = (int(m.group(1)), Path(full[:m.end()]))
    if not hits:
        return None, None
    runs = {n for n, _ in hits.values()}
    if len(runs) > 1:
        return None, f"ambiguous: HERMES_HOME and cwd name different runs {sorted(runs)}"
    run_id, home = next(iter(hits.values()))
    try:
        mk = json.loads((home / RUN_MARKER).read_text())
    except (OSError, ValueError):
        return None, f"run home {home} has no {RUN_MARKER} written by the runner"
    if mk.get("run") != run_id or mk.get("ab_home") != str(store.HOME):
        return None, f"run home {home} marker does not match run {run_id} on this AB_HOME"
    r = c.execute("SELECT r.id, r.harness, r.pid, r.pid_start, r.ended_at, t.id AS task, t.board, t.owner, t.status "
                  "FROM runs r JOIN tasks t ON t.run_id = r.id WHERE r.id=?", (run_id,)).fetchone()
    if r is None:
        return None, f"run {run_id} not found on this board DB"
    if r["ended_at"] is not None:
        return None, f"run {run_id} already ended (stale run home)"
    if r["harness"] != "hermes" or r["task"] != mk.get("task") or r["board"] != mk.get("board") or r["owner"] != mk.get("actor"):
        return None, f"run {run_id} does not match its marker (task/board/actor changed)"
    if r["status"] != "claimed":
        return None, f"task {r['task']} is {r['status']}, not held by run {run_id}"
    if r["pid"] is None or R.pid_start(r["pid"]) is None or R.pid_start(r["pid"]) != r["pid_start"]:
        return None, f"run {run_id} process {r['pid']} is gone (run is ending)"
    if board and board != r["board"]:
        return None, f"run {run_id} is on board {r['board']}, not {board}: refusing to resolve it on {board}"
    return {"task": r["task"], "actor": r["owner"], "board": r["board"], "run": run_id}, None


def adopt_run_identity(c, board=None):
    """Fill AB_TASK/AB_ACTOR/AB_BOARD/AB_RUN from the live run this shell belongs to (only unset ones, only for the same task).
    Returns (ctx, reason) from infer_run; ctx is None when nothing was adopted."""
    if os.environ.get("AB_TASK") and os.environ.get("AB_ACTOR") and os.environ.get("AB_BOARD"):
        return None, None
    ctx, reason = infer_run(c, board=board or os.environ.get("AB_BOARD") or None)
    if ctx is None:
        return None, reason
    if os.environ.get("AB_TASK") and os.environ["AB_TASK"] != ctx["task"]:
        return None, f"AB_TASK={os.environ['AB_TASK']} is not the task of run {ctx['run']} ({ctx['task']})"
    for k, v in (("AB_TASK", ctx["task"]), ("AB_ACTOR", ctx["actor"]), ("AB_BOARD", ctx["board"]), ("AB_RUN", str(ctx["run"]))):
        if not os.environ.get(k):
            os.environ[k] = v
    return ctx, None


def admin(c, a, board=None):
    """Global settings need admin on the main board; board settings need admin on that board."""
    store.require(c, actor_of(a), "admin", board or store.DEFAULT_BOARD)


def visible(c, a, rows):
    me = actor_of(a)
    return [r for r in rows if store.can(c, me, "read", r["board"], r["channel"])]


# ---------------------------------------------------------------- commands
PLAN_FIELDS = {"key", "title", "body", "labels", "deps", "tier", "workdir", "after", "priority", "manual", "cmd", "channel",
               "harness", "model", "max_attempts", "timeout", "data", "result", "verify"}


def _card_kwargs(title, body, labels, priority, after, workdir, tier, harness, model, manual, max_attempts, timeout, data,
                 cmd, result, channel="main", verify=None):
    """Validated add_task kwargs, shared by `ab add` and `ab add --plan`."""
    if workdir is None and os.environ.get("AB_TASK"):
        workdir = os.getcwd()  # sub-tasks created inside a run inherit the run's workdir
    if workdir:
        workdir = str(Path(workdir).expanduser().resolve())
    check_harness_pin(harness)
    if cmd is not None:
        if not cmd.strip():
            raise ValueError("--cmd needs a command")
        if manual or harness or model:
            raise ValueError("--cmd cards run a command: no --manual/--harness/--model")
        if result:
            data = {**(data or {}), "cmd_result": result}
    elif result:
        raise ValueError("--result only applies to --cmd cards")
    if verify is not None and cmd is not None:
        raise ValueError("--verify gates agent cards; a --cmd card already settles by its exit code")
    if verify is not None and not str(verify).strip():
        verify = None
    return dict(title=title, body=body, channel=channel, labels=labels, priority=priority,
                not_before=parse_time(after) if after else None, workdir=workdir, tier=tier, harness=harness, model=model,
                runnable=not manual, max_attempts=max_attempts, timeout_s=parse_dur(timeout) if timeout else None, data=data, cmd=cmd, verify=verify)


def plan_specs(raw):
    """Parse + validate a plan (JSON array of card objects) BEFORE anything is written; unknown fields are refused."""
    try:
        cards = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"plan is not valid JSON: {e}") from None
    if isinstance(cards, dict) and isinstance(cards.get("cards"), list):
        cards = cards["cards"]
    if not isinstance(cards, list) or not cards:
        raise ValueError("a plan is a non-empty JSON array of cards")
    specs = []
    for i, cd in enumerate(cards):
        who = f"card {i}" + (f" ({cd.get('key')})" if isinstance(cd, dict) and cd.get("key") else "")
        if not isinstance(cd, dict):
            raise ValueError(f"{who}: not an object")
        bad = sorted(set(cd) - PLAN_FIELDS)
        if bad:
            raise ValueError(f"{who}: unknown field(s) {', '.join(bad)} (allowed: {', '.join(sorted(PLAN_FIELDS))})")
        if not isinstance(cd.get("title"), str) or not cd["title"].strip():
            raise ValueError(f"{who}: title is required")
        for f, typ in (("key", str), ("body", str), ("tier", str), ("workdir", str), ("after", str), ("cmd", str), ("result", str),
                       ("channel", str), ("harness", str), ("model", str), ("timeout", str), ("verify", str)):
            if cd.get(f) is not None and not isinstance(cd[f], typ):
                raise ValueError(f"{who}: {f} must be a string")
        if cd.get("key") is not None and not cd["key"].strip():
            raise ValueError(f"{who}: key must not be empty")
        for f in ("priority", "max_attempts"):
            if cd.get(f) is not None and (not isinstance(cd[f], int) or isinstance(cd[f], bool)):
                raise ValueError(f"{who}: {f} must be an integer")
        if cd.get("manual") is not None and not isinstance(cd["manual"], bool):
            raise ValueError(f"{who}: manual must be true or false")
        lab = cd.get("labels") or {}
        if not isinstance(lab, dict):
            raise ValueError(f"{who}: labels must be an object")
        deps = []
        for d in cd.get("deps") or []:
            if isinstance(d, str):
                d = {"id": d}
            if not isinstance(d, dict) or len(d) != 1 or next(iter(d)) not in ("key", "id") or not isinstance(next(iter(d.values())), str):
                raise ValueError(f"{who}: a dep is {{\"key\": K}} or {{\"id\": ID}} (or a bare task id string)")
            deps.append(d)
        try:
            kw = _card_kwargs(cd["title"], cd.get("body") or "", {str(k): str(v) for k, v in lab.items()}, cd.get("priority") or 0,
                              cd.get("after"), cd.get("workdir"), cd.get("tier"), cd.get("harness"), cd.get("model"),
                              bool(cd.get("manual")), cd.get("max_attempts") or 3, cd.get("timeout"), cd.get("data"),
                              cd.get("cmd"), cd.get("result"), cd.get("channel") or "main", cd.get("verify"))
        except ValueError as e:
            raise ValueError(f"{who}: {e}") from None
        specs.append({"key": cd.get("key"), "deps": deps, "kw": kw})
    return specs


def cmd_add(c, a, out):
    if a.plan:
        if a.title or a.dep or a.cmd is not None or a.manual or a.label or a.body or a.body_file or a.harness or a.model or a.data:
            raise ValueError("--plan takes the cards from the plan file: no title/--body/--dep/--cmd/--label/... (only --key, --key-live)")
        raw = a.plan if a.plan.lstrip().startswith("[") else (sys.stdin.read() if a.plan == "-" else text_arg(None, a.plan))  # inline JSON (MCP) | - | file
        specs = plan_specs(raw)
        # a dep given as a task id may be an alias the board resolves (like --dep)
        for sp in specs:
            sp["deps"] = [{"id": store.get_task(c, d["id"])["id"] if store.get_task(c, d["id"]) else d["id"]} if "id" in d else d
                          for d in sp["deps"]]
        r = store.add_plan(c, actor_of(a), specs, board_of(a), a.key, a.key_live)
        out(r, lambda r: "\n".join(f"{k}\t{v}" for k, v in r["ids"].items()))
        return
    if not a.title:
        raise ValueError("a title is required (or --plan FILE)")
    labels = kv_list(a.label)
    if a.reply_to:
        store.resolve_thread(c, a.reply_to)
        labels["reply_to"] = a.reply_to
    data = jarg(a.data)
    kw = _card_kwargs(a.title, body_arg(a), labels, a.priority, a.after, a.workdir, a.tier, a.harness, a.model, a.manual,
                      a.max_attempts, a.timeout, data, a.cmd, a.result, a.channel, a.verify)
    info = {}
    tid = store.add_task(c, actor_of(a), board=board_of(a), key=a.key, key_live=a.key_live, _info=info,
                         deps=[store.get_task(c, d)["id"] if store.get_task(c, d) else d for d in (a.dep or [])], **kw)
    stale = R.runner_status(c).get("stale")
    out({"id": tid, "existed": bool(info.get("existed"))}, tid)
    if stale:
        print(store.STALE_MSG, file=sys.stderr)


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
    if a.ready and items:
        held = store.busy_dirs(c)
        for d in items:
            why = store.workdir_wait_reason(c, d, held)
            if why:
                d["wait_reason"] = why
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


def rm_root():
    """The tree `ab rm` may delete in: this run's workdir ($AB_WORKDIR), else the git toplevel of the cwd."""
    import subprocess
    w = os.environ.get("AB_WORKDIR")
    if not w:
        p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        w = p.stdout.strip() if p.returncode == 0 else ""
    if not w:
        raise SystemExit("ab rm: no worktree (set AB_WORKDIR or run inside a git checkout)")
    return os.path.realpath(w)


def rm_check(path, root):
    """Absolute path to delete, or raise ValueError. The parent is resolved (symlinks followed), the last component is not,
    so a symlink inside the worktree is removed itself, never its target; .git and the root stay."""
    ap = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(path))), os.path.basename(path.rstrip("/")))
    if ap == root or not ap.startswith(root + os.sep):
        raise ValueError(f"{path}: outside the worktree {root}, refused")
    if ".git" in ap[len(root) + 1:].split(os.sep):
        raise ValueError(f"{path}: inside .git, refused")
    return ap


def cmd_rm(c, a, out):
    import shutil
    root = rm_root()
    done, bad = [], []
    for p in a.paths:
        try:
            ap = rm_check(p, root)
        except ValueError as e:
            bad.append(str(e))
            continue
        if os.path.islink(ap) or os.path.isfile(ap):
            os.unlink(ap)
        elif os.path.isdir(ap):
            if not a.recursive:
                bad.append(f"{p}: is a directory (use -r)")
                continue
            shutil.rmtree(ap)
        elif not a.force:
            bad.append(f"{p}: no such file")
            continue
        done.append(ap)
    for b in bad:
        print("ab rm: " + b, file=sys.stderr)
    out({"removed": done, "refused": bad}, "\n".join(f"removed {d}" for d in done))
    if bad:
        raise SystemExit(1)


def cmd_done(c, a, out):
    text = text_arg(a.result, a.summary_file)
    tid = store.complete(c, a.id, actor_of(a), text, jarg(a.data), a.force)
    out({"id": tid, "status": "done"}, f"{tid} done")


def cmd_fail(c, a, out):
    tid, state = store.fail(c, a.id, actor_of(a), text_arg(a.why, a.why_file), a.force, negative=a.negative)
    suffix = " (negative)" if a.negative else ""
    out({"id": tid, "status": state, "negative": bool(a.negative)}, f"{tid} failed -> {state}{suffix}")


def cmd_anomalies(c, a, out):
    store.reap(c)
    bsel = None if a.all_boards else board_of(a)
    since = (now() - parse_dur(a.since)) if a.since else None
    items = store.anomalies(c, actor=actor_of(a), board=bsel, since=since)
    out(items, lambda xs: "\n".join(x["message"] for x in xs) or "(no anomalies)")


def cmd_release(c, a, out):
    tid, state = store.release(c, a.id, actor_of(a), a.why or "", a.force)
    out({"id": tid, "status": state}, f"{tid} released")


def cmd_defer(c, a, out):
    deps = [store.get_task(c, d)["id"] if store.get_task(c, d) else d for d in (a.after_task or [])]
    tid = store.defer(c, a.id, actor_of(a), parse_time(a.until) if a.until else None, deps, a.why or "", a.force)
    out({"id": tid, "status": "open", "deferred": True}, f"{tid} deferred")


def cmd_park(c, a, out):
    tid = a.id or os.environ.get("AB_TASK")
    if not tid:
        raise ValueError("give a task ID (or run inside a runner-started agent where AB_TASK is set)")
    if (a.pid is not None) + bool(a.file) + bool(a.reply) != 1:
        raise ValueError("give exactly one of --pid PID, --file PATH or --reply THREAD")
    pid = pid_start = None
    if a.pid is not None:
        pid = int(a.pid)
        if pid <= 0:
            raise ValueError(f"bad pid {a.pid}")
        pid_start = R.pid_start(pid)
        if pid_start is None:
            raise ValueError(f"pid {pid} is not running (job already finished? read its outputs and continue)")
    grace = parse_dur(a.grace or "60s")
    grace = 60.0 if grace is None else grace
    tid, spec = store.park(c, tid, actor_of(a), text_arg(a.note, a.note_file), pid=pid, pid_start=pid_start,
                           file=a.file, grace_s=grace, force=a.force, reply=a.reply)
    out({"id": tid, "status": "open", "parked": True, "wake": {k: v for k, v in spec.items() if k in ("pid", "file", "grace_s")}},
        f"{tid} parked on {store.park_cond(spec)}; exit now - the runner wakes this card and a fresh agent continues with your note")


def cmd_cancel(c, a, out):
    row = store.must_task(c, a.id)
    if row["status"] in ("done", "cancelled"):
        raise ValueError(f"task {row['id']} is already {row['status']}")
    tid = store.cancel(c, a.id, actor_of(a), a.why or "")
    out({"id": tid, "status": "cancelled"}, f"{tid} cancelled")


def cmd_retry(c, a, out):
    tid = store.retry(c, a.id, actor_of(a))
    out({"id": tid, "status": "open"}, f"{tid} reopened")


def check_harness_pin(harness):
    """A card's --harness may carry a pin ('pi@endpoint', 'hermes/provider'): reject a bad one now, not when the runner gets to it."""
    if harness:
        if harness.split("@")[0].split("/")[0] not in H.ADAPTERS:
            raise ValueError(f"unknown harness {harness!r}; known: {', '.join(H.ADAPTERS)}")
        H.parse_spec(f"{harness}:x")


def cmd_edit(c, a, out):
    f = {}
    for k in ("title", "channel", "priority", "tier", "harness", "model", "max_attempts", "move_to", "verify"):
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
    check_harness_pin(f.get("harness"))

    def tid_of(x):
        r = store.get_task(c, x)
        if r is None:
            raise LookupError(f"no task {x}")
        return r["id"]
    tid = store.edit(c, a.id, actor_of(a), f, labels, [tid_of(d) for d in a.dep or []], [tid_of(d) for d in a.rm_dep or []])
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


def cmd_kill(c, a, out):
    """Stop one run without cancelling its task: the runner terminates the process, the task goes back to the queue
    (no attempt counted) and may be started again. Use `ab cancel` to stop the work for good."""
    try:
        rid = int(a.run)
    except ValueError:
        raise ValueError(f"bad run id {a.run!r} (a number, see `ab runner status`)")
    r = c.execute("SELECT r.id, r.task_id, r.harness, r.ended_at, r.outcome, t.board FROM runs r LEFT JOIN tasks t ON t.id=r.task_id "
                  "WHERE r.id=?", (rid,)).fetchone()
    if r is None:
        raise LookupError(f"no run {rid}")
    admin(c, a, r["board"] or None)
    if r["ended_at"] is not None:
        raise ValueError(f"run {rid} already ended ({r['outcome']})")
    with store.tx(c):
        ids = [int(i) for i in (store.cfg(c, "kill_runs") or [])]
        if rid not in ids:
            ids.append(rid)
        store.cfg_set(c, "kill_runs", ids, actor_of(a))
        store.event(c, r["task_id"], actor_of(a), "kill_requested", {"run": rid, "harness": r["harness"]})
    running = bool(R.runner_status(c).get("running"))
    out({"run": rid, "task_id": r["task_id"], "requested": True, "runner_running": running},
        f"kill requested for run {rid} ({r['task_id']})" + ("" if running else "; the runner is not running, it stops the run once it starts"))


def cmd_decision(c, a, out):
    """Decision cards are manual cards (`ab add --manual`, usually labelled owner=NAME) that wait for an answer."""
    act = a.action or "ls"
    if act == "answer":
        if not a.id or not (a.text or a.file):
            raise ValueError("usage: ab decision answer ID TEXT   (or --file F, or '-' for stdin)")
        tid = store.answer(c, a.id, actor_of(a), text_arg(a.text, a.file))
        out({"id": tid, "status": "done", "answered": True}, f"{tid} answered and closed")
        return
    sql, args = "SELECT * FROM tasks WHERE runnable=0 AND status IN ('open','claimed')", []
    if not a.all_boards:
        sql += " AND board=?"
        args.append(board_of(a))
    if a.owner:
        sql += " AND json_extract(labels, '$.owner')=?"
        args.append(a.owner)
    rows = c.execute(sql + " ORDER BY priority DESC, created_at LIMIT ?", args + [a.limit]).fetchall()
    items = [store.task_dict(c, r) for r in visible(c, a, rows)]
    out(items, lambda xs: "\n".join(tline(x) for x in xs) or "(no decisions waiting)")


def cmd_snapshot(c, a, out):
    if a.schema:
        print(S.SCHEMA_FILE.read_text(), end="")
        return
    tail = S.parse_tail_spec(a.tail) if a.tail else None
    import gc
    gc.disable()  # one burst of allocations that all live until the output is written
    try:
        snap = S.build(c, actor_of(a), a.board, a.closed, a.events_after, a.events_limit, tail)
        if out.as_json:
            print(json.dumps(snap, default=str, ensure_ascii=False, separators=(",", ":")))
        else:
            print(S.render_text(snap))
    finally:
        gc.enable()


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
            lines = ["bucket            5h out / calls      24h out / calls     7d out / calls      budget          state"]
            for r in xs:
                st = "ok"
                if r["limit"]:
                    st = f"{r['limit']['kind']} until {fmt_ts(r['limit']['until'])}"
                elif r["cap_hit"]:
                    st = "capped: " + r["cap_hit"]
                p = r.get("pacer") or {}
                bcell = f"{r.get('launch_budget', '-')}/{p.get('interval', '-')} ({r.get('budget_why', '-')})"
                cells = "".join(f"{_num(r[w]['output']):>8} / {r[w]['calls']:<8}  " for w in ("5h", "24h", "7d"))
                lines.append(f"{r['bucket']:<16} {cells}{bcell:<16}{st}" + (f"  cap={json.dumps(r['cap'])}" if r["cap"] else ""))
            return "\n".join(lines)
        out(rows, txt)
        return
    rows = usage.summarize(c, since, a.by)
    out(rows, lambda xs: "\n".join(f"{(r['k'] or '-'):<34} calls={r['calls']:<6} in={_num(r['input'])} out={_num(r['output'])} "
                                   f"cache_r={_num(r['cache_read'])} cache_w={_num(r['cache_write'])}"
                                   + (f" ${r['cost_usd']:.2f}" if r['cost_usd'] else "") for r in xs) or "(no usage)")


def cmd_outcomes(c, a, out):
    from . import outcomes
    rep = outcomes.ledger(c, parse_dur(a.since) if a.since else None, a.by)
    out(rep, outcomes.render)


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
        for k in ("max_output", "max_cost", "max_runs", "max_calls", "max_5h_pct", "max_7d_pct", "pace_7d_lead_h", "pace_lead_h"):
            v = getattr(a, k)
            if v is not None:
                cap[k] = v
        if getattr(a, "ignore_7d_until", None):
            cap["ignore_7d_until"] = parse_time(a.ignore_7d_until)
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
        running = bool(R.runner_status(c).get("running"))
        out({"ok": True, "action": act, "runner_running": running}, f"runner: {act}" + (" (now)" if act == "stop" and a.now else "")
            + (" (re-execs with the current code; running cards keep going)" if act == "reload" else "")
            + ("" if running else " (the runner is not running: the setting applies when it starts)"))
        return
    if act == "slots":
        try:
            n = int(a.value)
        except (TypeError, ValueError):
            n = -1
        if n < 0:
            raise ValueError("usage: ab runner slots N  (N is a whole number >= 0)")
        with store.tx(c):
            store.cfg_set(c, "slots", n, actor)
        out({"slots": n}, f"slots = {n}")
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
        if s.get("stale"):
            lines.append(store.STALE_MSG)
        if s.get("note"):
            lines.append("note: " + s["note"])
        for ch in s.get("children", []):
            lines.append(f"  run {ch['run']} {ch['task_id']} " + (f"cmd: {(ch['cmd'] or '')[:40]}" if ch['harness'] == 'cmd' else f"{ch['harness']}:{ch['model'] or ''} [{ch['bucket']}]") + " "
                         f"{fmt_dur(now() - ch['started_at'])}"
                         + (f"  [{ch['stall']}]" if ch.get("stall") else "") + f"  {(ch['title'] or '')[:60]}")
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


def cmd_run_tail(c, a, out):
    """`ab tail --run RUN_ID [--bytes N] [--follow DUR]`: the last bytes of a run's output, ANSI stripped."""
    rid = int(a.run)
    res = S.run_tail(c, rid, min(a.bytes, S.MAX_TAIL))
    if "task_id" not in res:
        raise LookupError(res["error"])
    if out.as_json:
        print(json.dumps(res, default=str, ensure_ascii=False))
    else:
        print(res["out"], end="" if res["out"].endswith("\n") or not res["out"] else "\n")
        if res["err"].strip():
            print("--- stderr ---\n" + res["err"], end="" if res["err"].endswith("\n") else "\n")
    if a.follow:
        row = c.execute("SELECT out_path FROM runs WHERE id=?", (rid,)).fetchone()
        pos, deadline = res["out_size"], now() + (parse_dur(a.follow) or 0)
        while row and row["out_path"] and now() < deadline:
            time.sleep(1)
            try:
                with open(row["out_path"], "rb") as f:
                    f.seek(pos)
                    data = f.read()
            except OSError:
                break
            if data:
                pos += len(data)
                txt = S.strip_ansi(data.decode("utf-8", errors="replace"))
                print(json.dumps({"run": rid, "out": txt}, ensure_ascii=False) if out.as_json else txt, end="", flush=True)


def cmd_tail(c, a, out):
    if a.run is not None:
        return cmd_run_tail(c, a, out)
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
                  "cooldown": a.cooldown or "0", "reply": not a.no_reply, "coalesce": not a.no_coalesce, "priority": a.priority or 0,
                  "labels": dict(kv.split("=", 1) for kv in (a.task_label or []))}
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
                "push": a.push, "close_on_done": a.close_on_done, "mirror_all": a.mirror_all, "manual": a.manual,
                "untrusted": a.untrusted}
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
    if act in ("add", "rm", "stop"):
        admin(c, a)
        if not a.name:
            raise ValueError(f"endpoints {act} NAME ...")
        if act == "add":
            if not a.base_url:
                raise ValueError("endpoints add NAME --base-url http://host:port/v1 [--api-key-env VAR] [--context N]")
            eps[a.name] = {"base_url": a.base_url.rstrip("/"), "api_key_env": a.api_key_env, "context": a.context,
                           "bucket": a.bucket}
            # managed lifecycle (opt-in): the runner starts the server when a card needs it and stops it when idle
            for k in ("start_cmd", "stop_cmd", "health_url", "health_match", "start_timeout", "idle_stop", "max_runs",
                      "group", "start_dir", "switch_after", "yield_file"):
                v = getattr(a, k, None)
                if v is not None:
                    eps[a.name][k] = v
        elif act == "stop":
            from . import managed
            if not managed.Lifecycle(c, lambda m: None).stop(a.name, f"stopped by {actor_of(a)}"):
                raise LookupError(f"endpoint {a.name} was not started by the runner (nothing to stop)")
            out({"stopping": a.name}, lambda d: f"stopping {d['stopping']} (the runner finishes the stop)")
            return 0
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
    state = store.cfg(c, "endpoint_state") or {}
    res = {"endpoints": eps, "harnesses": capable, "state": state}

    def _managed(n, e):
        if not e.get("start_cmd") and not e.get("max_runs"):
            return ""
        s = state.get(n) or {}
        bits = [f"max_runs={e['max_runs']}" if e.get("max_runs") else "", f"group={e['group']}" if e.get("group") else ""]
        if e.get("start_cmd"):
            if e.get("yield_file"):
                bits.append(f"yield_file={e['yield_file']}")
            bits.append(f"managed (idle_stop={e.get('idle_stop') if e.get('idle_stop') is not None else 300}s) "
                        + (f"{s['status']} pid {s.get('pid')}" if s else "not started by the board"))
        return " " + " ".join(b for b in bits if b)
    out(res, lambda d: "\n".join(f"{n}: {e['base_url']} context={e.get('context')}"
                                 + (f" key=${e['api_key_env']}" if e.get("api_key_env") else "")
                                 + f" bucket={e.get('bucket') or 'ep:' + n}" + _managed(n, e) for n, e in d["endpoints"].items())
        + ("\n" if d["endpoints"] else "(no endpoints)\n") + "harnesses that can run on endpoints: " + ", ".join(d["harnesses"])
        + "\nuse in tiers / pins as  HARNESS@ENDPOINT:MODEL  e.g. pi@rtx3060:spark-1.7b")


def cmd_mcp(c, a, out):
    from . import mcp
    mcp.serve()


# ---------------------------------------------------------------- parser
def snapshot_args(s):
    s.set_defaults(ro=True)
    s.add_argument("--closed", type=int, default=50, help="how many recently closed tasks to include (default 50)")
    s.add_argument("--events-after", type=int, help="include events with id > this (0 = the newest ones); omit for none")
    s.add_argument("--events-limit", type=int, default=200)
    s.add_argument("--tail", help="RUN_ID:BYTES, the last BYTES of that run's stdout/stderr, ANSI stripped")
    s.add_argument("--schema", action="store_true", help="print the JSON Schema of the output and exit")


def fast_snapshot_args(argv):
    """`ab snapshot` runs on every UI refresh, and just setting up argparse costs more than the query. This parses
    `ab [--json] [--as X] [-B X] snapshot [options]` by hand and returns a Namespace; anything unusual (other command, -h,
    a bad value) returns None so the normal parser handles it and reports the error."""
    ns = argparse.Namespace(fn=cmd_snapshot, cmd="snapshot", ro=True, json=os.environ.get("AB_JSON") == "1", actor=None, board=None,
                            closed=50, events_after=None, events_limit=200, tail=None, schema=False)
    valued = {"--as": "actor", "--board": "board", "-B": "board", "--closed": "closed", "--events-after": "events_after",
              "--events-limit": "events_limit", "--tail": "tail"}
    seen, i = False, 0
    while i < len(argv):
        t = argv[i]
        i += 1
        key, eq, val = t.partition("=") if t.startswith("--") else (t, "", None)
        if t == "snapshot" and not seen:
            seen = True
        elif key == "--json":
            ns.json = True
        elif key == "--schema" and seen:
            ns.schema = True
        elif key in valued and (key in ("--as", "--board", "-B") or seen):
            if not eq:
                if i >= len(argv):
                    return None
                val, i = argv[i], i + 1
            dest = valued[key]
            try:
                setattr(ns, dest, int(val) if dest in ("closed", "events_after", "events_limit") else val)
            except ValueError:
                return None
        else:
            return None
    return ns if seen else None


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
    s.add_argument("title", nargs="?")
    s.add_argument("--key", help="idempotency key: if a card with this key is already on the board (not cancelled) print its id and create nothing; with --plan, a rerun is a no-op that prints the existing key->id map")
    s.add_argument("--key-live", action="store_true", help="--key only matches open/claimed cards (a finished one frees the key)")
    s.add_argument("--plan", metavar="FILE", help="create a whole plan: JSON array of cards {key,title,body,labels,deps:[{key}|id],tier,workdir,after,priority,manual,cmd,...} in ONE transaction ('-' reads stdin)")
    s.add_argument("--body", "-b", help="description ('-' reads stdin)")
    s.add_argument("--body-file")
    s.add_argument("--channel", "-c", default="main")
    s.add_argument("--label", "-l", action="append", help="key=value (repeatable)")
    s.add_argument("--priority", "-p", type=int, default=0)
    s.add_argument("--dep", "-d", action="append", help="task id that must be done first (repeatable)")
    s.add_argument("--after", help="not before: +2h | 14:30 | 2026-11-20 03:00")
    s.add_argument("--workdir", "-w", "--cwd", help="where the work runs (--cwd for command cards)")
    s.add_argument("--cmd", help="command card: the runner runs this shell command itself (no model, no quota); exit 0=done, 75=release, else fail")
    s.add_argument("--result", help="command card: fixed result text on exit 0 (default: last output line)")
    s.add_argument("--verify", help="done gate: after the agent calls `ab done` the runner runs this shell command in the workdir; exit 0 accepts, else the card reopens with the last 40 lines of output in the next brief (config verify_rounds, default 2, then it fails)")
    s.add_argument("--tier", "-t", help="intelligence tier (see `ab tiers`)")
    s.add_argument("--harness", help="pin a harness (skips tier routing); HARNESS@ENDPOINT or hermes/PROVIDER pins where it runs")
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
    s.add_argument("id", nargs="?", default="", help="task id (default: inferred from your run home, or $AB_TASK)")
    s.add_argument("text", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--file", help="read the note from this file (avoids shell quoting of $ and backticks)")
    s.add_argument("--data", help="JSON (or @file)")

    s = sub("rm", cmd_rm, "delete files/dirs inside your own worktree (for harnesses whose shell refuses `rm -f`)")
    s.add_argument("paths", nargs="+")
    s.add_argument("-r", "--recursive", action="store_true")
    s.add_argument("-f", "--force", action="store_true", help="a missing path is not an error")

    s = sub("done", cmd_done, "complete your task", ["complete"])
    s.add_argument("id", nargs="?", default="", help="task id (default: inferred from your run home, or $AB_TASK)")
    s.add_argument("result", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--summary-file", help="read the result from this file (avoids shell quoting of $ and backticks)")
    s.add_argument("--data")
    s.add_argument("--force", action="store_true")

    s = sub("fail", cmd_fail, "give up this attempt (task retries until max attempts, then dead)")
    s.add_argument("id", nargs="?", default="", help="task id (default: inferred from your run home, or $AB_TASK)")
    s.add_argument("why", nargs="?", default="", help="'-' reads stdin")
    s.add_argument("--why-file", help="read the reason from this file")
    s.add_argument("--negative", action="store_true", help="mark as intended negative (dead immediately, label outcome=negative, skipped by anomaly scans)")
    s.add_argument("--force", action="store_true")

    s = sub("anomalies", cmd_anomalies, "list dead cards (excluding outcome=negative) and open cards blocked on dead/cancelled deps")
    s.add_argument("--all-boards", action="store_true")
    s.add_argument("--since", help="only dead cards updated within this window, e.g. 6h, 1d")

    s = sub("release", cmd_release, "hand the task back without counting an attempt (to wait on time or data use `ab defer ID --until T --why ...`, not release: repeated quick releases trigger a relaunch backoff)")
    s.add_argument("id")
    s.add_argument("why", nargs="?")
    s.add_argument("--force", action="store_true")

    s = sub("defer", cmd_defer, "park a task until a time and/or other tasks are done")
    s.add_argument("id")
    s.add_argument("--until")
    s.add_argument("--after", dest="after_task", action="append", help="task id (repeatable)")
    s.add_argument("--why")
    s.add_argument("--force", action="store_true")

    s = sub("park", cmd_park, "park a claimed card on a long external job and exit; the runner resumes it with a fresh agent")
    s.add_argument("id", nargs="?", help="default $AB_TASK")
    s.add_argument("--pid", type=int, help="wake when this process exits (start it detached: nohup/setsid)")
    s.add_argument("--file", help="wake when this file exists and stops growing for --grace")
    s.add_argument("--reply", metavar="THREAD", help="wake when someone else posts in THREAD (e.g. the m<id> printed by `ab ask`); the reply goes in the next brief")
    s.add_argument("--grace", default="60s", help="file stability window (default 60s)")
    s.add_argument("--note", help="what you wait for and what the next agent should do ('-' reads stdin)")
    s.add_argument("--note-file", help="read the note from this file")
    s.add_argument("--force", action="store_true")

    s = sub("kill", cmd_kill, "stop one run (the task goes back to the queue, no attempt counted); `cancel` stops the work for good")
    s.add_argument("run", help="run id (see `ab runner status`)")

    s = sub("decision", cmd_decision, "decision cards (manual tasks waiting for an answer): ls [--owner X] | answer ID TEXT")
    s.add_argument("action", nargs="?", choices=["ls", "answer"])
    s.add_argument("id", nargs="?")
    s.add_argument("text", nargs="?", help="the answer ('-' reads stdin)")
    s.add_argument("--file", help="read the answer from this file")
    s.add_argument("--owner", help="only cards labelled owner=NAME")
    s.add_argument("--limit", "-n", type=int, default=100)
    s.add_argument("--all-boards", action="store_true")

    s = sub("snapshot", cmd_snapshot, "everything a UI needs in one read-only call (use --json; schema: --schema)")
    snapshot_args(s)

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
    s.add_argument("--verify", help="done gate command (see `ab add --verify`); empty string removes it")
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

    s = sub("outcomes", cmd_outcomes, "outcome ledger: runs, tokens, $ per accepted outcome and failure classes (read-only)")
    s.add_argument("--since", help="e.g. 24h, 7d (runs started in that window)")
    s.add_argument("--by", default="label:type,harness,tier", help="comma list of label:NAME, harness, tier, model, board, bucket")

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
    s.add_argument("--pace-lead-h", type=float, help="pace every provider-reported quota window (codex: read from ~/.codex/sessions) "
                   "so it is used up this many hours before its reset")
    s.add_argument("--ignore-7d-until", help="pace/cap on the 5h window only until this time (e.g. +6d, ISO); "
                   "the weekly window is ignored until then (claude-sub only)")
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

    s = sub("merge", cmd_merge, "merge a card's branch (worktree ab/<id>) into its workdir; a dirty tree is fine unless git says local changes overlap")
    s.add_argument("id")
    s.add_argument("--branch")
    s.add_argument("--into", help="checkout to merge into (default: the card's workdir)")

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
    s.add_argument("--run", type=int, help="print the output of this run id instead of events (with --bytes)")
    s.add_argument("--bytes", type=int, default=4000, help="with --run: how much of the end of the output")

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
    s.add_argument("--task-label", action="append", metavar="K=V", help="label for the woken task, e.g. role=overseer (repeatable)")

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
    s.add_argument("--untrusted", action="store_true", help="label imported cards untrusted=1: --push never posts for them (any card with label untrusted=1 is skipped)")

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

    s = sub("endpoints", cmd_endpoints, "OpenAI-compatible servers: ls | add NAME --base-url URL [--api-key-env VAR] [--context N] "
            "[--start-cmd CMD ...] | rm NAME | check [NAME] | stop NAME")
    s.add_argument("action", nargs="?", choices=["ls", "add", "rm", "check", "stop"])
    s.add_argument("name", nargs="?")
    s.add_argument("--base-url", help="e.g. http://127.0.0.1:8093/v1")
    s.add_argument("--api-key-env", help="name of the env var holding the key (the key itself is never stored)")
    s.add_argument("--context", type=int, default=65536, help="context window to tell the harness")
    s.add_argument("--bucket", help="quota bucket name (default ep:NAME)")
    g = s.add_argument_group("managed server (opt-in): the runner starts it when a card needs it, stops it when idle")
    g.add_argument("--start-cmd", help="shell command that runs the server in the FOREGROUND (stopping = SIGTERM to its "
                                       "process group unless --stop-cmd is given)")
    g.add_argument("--stop-cmd", help="shell command that stops the server (run before the SIGTERM)")
    g.add_argument("--start-dir", help="working directory for --start-cmd (default AB_HOME)")
    g.add_argument("--health-url", help="ready check URL (default BASE_URL/models: HTTP 200 with a non-empty model list)")
    g.add_argument("--health-match", help="regex the health response must contain (e.g. the model id)")
    g.add_argument("--start-timeout", type=int, help="seconds from start to healthy before the start fails (default 900)")
    g.add_argument("--idle-stop", type=int, help="stop the server after this many seconds with no board run (default 300; "
                                                 "-1 = never)")
    g.add_argument("--max-runs", type=int, help="at most this many board runs on the endpoint at once (also for unmanaged)")
    g.add_argument("--group", help="endpoints sharing one device: only one of the group is up; the runner switches when idle")
    g.add_argument("--yield-file", help="while this path exists the runner starts no new runs on the endpoint (or its group), "
                                        "lets current runs finish, then stops the server; cards reroute. A first word that "
                                        "is a dead pid marks the file stale (removed, ignored)")
    g.add_argument("--switch-after", type=int, help="seconds a card waits for a group-mate other cards still want before "
                                                    "the runner switches anyway, once it is idle (default 1800)")

    sub("mcp", cmd_mcp, "serve the board as an MCP server on stdio")
    s = sub("tui", cmd_tui, "live terminal dashboard and control panel (runs the separate ab-tui binary)")
    s.add_argument("tui_args", nargs=argparse.REMAINDER)
    return p


TUI_INSTALL = ("cargo install --git https://github.com/riceharvest/agentboard ab-tui"
               "   (from a checkout: cargo install --path tui)")


def run_tui(rest):
    """`ab tui [ab-tui options]`: exec the terminal UI if it is installed, else say how to install it. Opens no database."""
    import shutil
    exe = os.environ.get("AB_TUI_BIN") or shutil.which("ab-tui")
    if not exe:
        print("ab: the terminal UI (ab-tui) is not installed. Install it with:\n    " + TUI_INSTALL, file=sys.stderr)
        return 127
    os.environ.setdefault("AB_BIN", shutil.which("ab") or shutil.which("agentboard") or os.path.abspath(sys.argv[0]))
    try:
        os.execv(exe, [exe, *rest])
    except OSError as e:
        print(f"ab: cannot run {exe}: {e}", file=sys.stderr)
        return 127


def git_merge(repo, branch):
    """Merge `branch` into the checkout at `repo` and let git decide: a dirty tree is fine unless local changes overlap the
    merged files, which is exactly when git refuses. Returns (ok, message, overlapping_files)."""
    p = subprocess.run(["git", "-C", str(repo), "merge", "--no-edit", branch], capture_output=True, text=True)
    text = (p.stdout + p.stderr).strip()
    if p.returncode == 0:
        return True, text, []
    files, grab = [], False
    for ln in text.splitlines():
        if re.search(r"(local changes to the following files|untracked working tree files) would be overwritten", ln):
            grab = True
        elif grab and ln.startswith(("\t", " ")) and ln.strip():
            files.append(ln.strip())
        elif grab:
            grab = False
    return False, text, files


def cmd_merge(c, a, out):
    row = store.get_task(c, a.id)
    if row is None:
        raise LookupError(f"no task {a.id}")
    labels = json.loads(row["labels"] or "{}")
    branch = a.branch or labels.get("worktree_branch") or f"ab/{row['id']}"
    repo = a.into or row["workdir"]
    if not repo:
        raise ValueError("no checkout to merge into: pass --into DIR")
    ok, msg, files = git_merge(repo, branch)
    out({"id": row["id"], "branch": branch, "into": str(repo), "merged": ok, "overlap": files, "git": msg},
        lambda r: msg if ok else None)
    if not ok:
        print(f"ab merge: git refused to merge {branch} into {repo}"
              + (f"; local changes overlap: {', '.join(files)}" if files else "") + f"\n{msg}", file=sys.stderr)
        return 1


def cmd_tui(c, a, out):  # only reached through `ab --json tui` style calls: main() handles plain `ab tui` before the db opens
    return run_tui(a.tui_args)


def _sanitize_worker_env():
    """When a runner-launched worker invokes `ab` against a different AB_HOME (e.g. a throwaway test board),
    don't carry over AB_RUN/AB_BOARD/AB_RUN_HOME from the worker's own board. AB_TASK/AB_ACTOR are dropped too,
    unless AB_HOME is set explicitly (`AB_HOME=/tmp/x AB_TASK=y ab ...`): then they are the caller's own, for that board."""
    run_home = os.environ.get("AB_RUN_HOME")
    if run_home and store.norm_dir(str(store.HOME)) != store.norm_dir(run_home):
        explicit = bool(os.environ.get("AB_HOME"))
        for k in ("AB_RUN", "AB_BOARD", "AB_RUN_HOME") + (() if explicit else ("AB_ACTOR", "AB_TASK")):
            os.environ.pop(k, None)


def _resolve_task_arg(conn, a, fn_name):
    """`ab done/note/fail` without a usable task id: take it from the run home. `ab done "summary"` with $AB_TASK empty
    reaches us with the summary in the id slot; a value that is not a task on this board moves into the text slot."""
    slot = TASK_SLOT[fn_name]
    if a.id and (t := store.get_task(conn, a.id)) is not None:
        if not os.environ.get("AB_ACTOR") and not getattr(a, "actor", None):
            # explicit id, identity unset: a run home still tells us who we are, when the id is that run's own task
            ctx, _ = infer_run(conn, board=getattr(a, "board", None) or os.environ.get("AB_BOARD") or None)
            if ctx and ctx["task"] == t["id"]:
                for k, v in (("AB_ACTOR", ctx["actor"]), ("AB_BOARD", ctx["board"]), ("AB_RUN", str(ctx["run"]))):
                    if not os.environ.get(k):
                        os.environ[k] = v
                os.environ.setdefault("AB_TASK", ctx["task"])
        return
    if a.id and getattr(a, slot) or a.id and any(getattr(a, k, None) for k in TEXT_FILE_OPTS):
        return  # an explicit id that is not a task: let the store report it
    ctx, reason = adopt_run_identity(conn, board=getattr(a, "board", None))
    if ctx is None:
        if a.id and re.search(r"\s", a.id) and not getattr(a, slot):
            # `ab done "$AB_TASK" "summary"` with AB_TASK empty: the summary landed in the id slot, not a task id
            raise ValueError(f"no task id: AB_TASK is empty, so the text {a.id[:60]!r} was read as the task id. "
                             f"Pass the id first: ab {fn_name[4:]} <task id> '<text>' (or export AB_TASK, AB_ACTOR, AB_BOARD)")
        if a.id:
            return  # no run home: keep the store's own error for a bad id
        raise ValueError(f"no task id: pass it, or export AB_TASK, AB_ACTOR and AB_BOARD" + (f" ({reason})" if reason else ""))
    if a.id:
        setattr(a, slot, a.id)
    a.id = ctx["task"]


def main(argv=None):
    _sanitize_worker_env()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "tui":
        return run_tui(argv[1:])
    a = fast_snapshot_args(argv)
    if a is None:
        a = build_parser().parse_args(argv)
    for k in ("actor", "board"):
        if not hasattr(a, k):
            setattr(a, k, None)
    if getattr(a, "who_flag", None):
        a.who = a.who_flag
    out = Out(getattr(a, "json", False))
    conn = (store.connect_ro() if getattr(a, "ro", False) else None) or store.connect()
    try:
        fn = getattr(a, "fn", None)
        if fn is not None and fn.__name__ in TASK_SLOT:
            _resolve_task_arg(conn, a, fn.__name__)
        rc = a.fn(conn, a, out)
        return rc or 0
    except (LookupError, PermissionError, ValueError, KeyError, sqlite3.Error) as e:
        msg = str(e).strip("'\"")
        if out.as_json:
            print(json.dumps({"error": msg, "type": type(e).__name__}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    finally:
        conn.close()
