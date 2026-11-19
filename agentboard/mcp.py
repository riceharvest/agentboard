"""Minimal MCP server (stdio, JSON-RPC 2.0). Each tool maps 1:1 onto an `ab` command and returns its --json output."""
import contextlib
import io
import json
import sys

S, I, B, L, KV = "string", "integer", "boolean", "list", "kv"

# tool: (description, subcommand, [positional args], {param: (flag, kind)}, [required])
TOOLS = {
    "ab_add": ("Create a task for agents. tier picks the intelligence (see ab_tiers); deps must finish first.", "add",
               ["title"], {"body": ("--body", S), "channel": ("--channel", S), "labels": ("--label", KV), "priority": ("--priority", I),
                           "deps": ("--dep", L), "after": ("--after", S), "workdir": ("--workdir", S), "tier": ("--tier", S),
                           "harness": ("--harness", S), "model": ("--model", S), "manual": ("--manual", B),
                           "max_attempts": ("--max-attempts", I), "timeout": ("--timeout", S), "data": ("--data", S)}, ["title"]),
    "ab_list": ("List tasks. Default open+claimed; ready=true for claimable now.", "ls", [],
                {"status": ("--status", S), "ready": ("--ready", B), "channel": ("--channel", S), "labels": ("--label", KV),
                 "limit": ("--limit", I)}, []),
    "ab_show": ("Full task: body, notes, runs, result.", "show", ["id"], {}, ["id"]),
    "ab_claim": ("Claim the next ready task (or id). Returns task:null when nothing is ready.", "claim", ["id"],
                 {"channel": ("--channel", S), "labels": ("--label", KV), "lease": ("--lease", S), "wait": ("--wait", S)}, []),
    "ab_renew": ("Extend your lease on a claimed task.", "renew", ["id"], {"lease": ("--lease", S)}, ["id"]),
    "ab_note": ("Append a note to a task thread.", "note", ["id", "text"], {"data": ("--data", S)}, ["id", "text"]),
    "ab_done": ("Complete your claimed task with a short result.", "done", ["id", "result"], {"data": ("--data", S), "force": ("--force", B)}, ["id"]),
    "ab_fail": ("Give up this attempt; the task retries until max attempts.", "fail", ["id", "why"], {"force": ("--force", B)}, ["id", "why"]),
    "ab_release": ("Hand a task back without counting an attempt.", "release", ["id", "why"], {"force": ("--force", B)}, ["id"]),
    "ab_defer": ("Park a task until a time and/or until other tasks are done.", "defer", ["id"],
                 {"until": ("--until", S), "after": ("--after", L), "why": ("--why", S), "force": ("--force", B)}, ["id"]),
    "ab_cancel": ("Cancel a task (stops its run).", "cancel", ["id", "why"], {}, ["id"]),
    "ab_retry": ("Reopen a dead/cancelled/done task with fresh attempts.", "retry", ["id"], {}, ["id"]),
    "ab_edit": ("Change task fields. labels: {key: value} ('' removes).", "edit", ["id"],
                {"title": ("--title", S), "body": ("--body", S), "channel": ("--channel", S), "priority": ("--priority", I),
                 "tier": ("--tier", S), "harness": ("--harness", S), "model": ("--model", S), "workdir": ("--workdir", S),
                 "after": ("--after", S), "timeout": ("--timeout", S), "max_attempts": ("--max-attempts", I),
                 "labels": ("--label", KV), "add_deps": ("--dep", L), "rm_deps": ("--rm-dep", L),
                 "manual": ("--manual", B), "auto": ("--auto", B)}, ["id"]),
    "ab_log": ("Event log, all or for one task.", "log", ["id"], {"limit": ("--limit", I)}, []),
    "ab_usage": ("Token usage. by=bucket gives 5h/24h/7d per quota bucket with limit state.", "usage", [],
                 {"by": ("--by", S), "since": ("--since", S)}, []),
    "ab_limits": ("Cooldowns. action ls|set|clear; scope bucket:NAME or harness:NAME.", "limits", ["action", "scope"],
                  {"until": ("--until", S), "reason": ("--reason", S), "kind": ("--kind", S)}, []),
    "ab_caps": ("Soft caps per bucket. action ls|set|clear.", "caps", ["action", "bucket"],
                {"window": ("--window", S), "max_output": ("--max-output", I), "max_cost": ("--max-cost", I),
                 "max_runs": ("--max-runs", I), "max_calls": ("--max-calls", I)}, []),
    "ab_tiers": ("Intelligence tiers -> ordered harness:model@effort lists. action ls|set|add|rm|order|check.", "tiers",
                 ["action", "tier", "specs"], {"pos": ("--pos", I)}, []),
    "ab_models": ("Models each harness offers right now.", "models", ["harness"], {"refresh": ("--refresh", B)}, []),
    "ab_harness": ("Installed harnesses and their auth/limit state.", "harness", [], {}, []),
    "ab_runner": ("Control the runner: start|stop|pause|resume|drain|slots|status (value for slots; now for stop).", "runner",
                  ["action", "value"], {"slots": ("--slots", I), "now": ("--now", B)}, []),
    "ab_sched": ("Schedules: ls|add|rm|enable|disable. add needs one of window (HH:MM-HH:MM + slots), at (+2h/14:30 + do), daily (HH:MM + do). "
                 "do: pause|resume|drain|stop|slots=N|set:key=value", "sched", ["action", "name"],
                 {"window": ("--window", S), "at": ("--at", S), "daily": ("--daily", S), "days": ("--days", S),
                  "slots": ("--slots", I), "do": ("--do", S)}, []),
    "ab_config": ("Runtime settings: ls|get|set|unset.", "config", ["action", "key", "value"], {}, []),
    "ab_guide": ("How agents use this board.", "guide", [], {}, []),
}

JS = {S: {"type": "string"}, I: {"type": "number"}, B: {"type": "boolean"},
      L: {"type": "array", "items": {"type": "string"}}, KV: {"type": "object", "additionalProperties": {"type": "string"}}}


def schema(pos, flags, req):
    props = {}
    for name in pos:
        props[name] = {"type": "array", "items": {"type": "string"}} if name == "specs" else {"type": "string"}
    for name, (_, kind) in flags.items():
        props[name] = dict(JS[kind])
    props["as"] = {"type": "string", "description": "your actor name (default $AB_ACTOR)"}
    return {"type": "object", "properties": props, "required": req}


def to_argv(tool, args):
    _, sub, pos, flags, _ = TOOLS[tool]
    argv = ["--json"]
    if args.get("as"):
        argv += ["--as", str(args["as"])]
    argv.append(sub)
    for name in pos:
        v = args.get(name)
        if v is None or v == "":
            continue
        argv += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    for name, (flag, kind) in flags.items():
        v = args.get(name)
        if v is None:
            continue
        if kind == B:
            if v:
                argv.append(flag)
        elif kind == L:
            for x in (v if isinstance(v, list) else [v]):
                argv += [flag, str(x)]
        elif kind == KV:
            for k, x in (v or {}).items():
                argv += [flag, f"{k}={x}"]
        else:
            argv += [flag, str(v)]
    return argv


def call(tool, args):
    from . import cli
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(to_argv(tool, args))
        except SystemExit as e:  # argparse errors
            rc = e.code if isinstance(e.code, int) else 1
    text = (buf.getvalue() + err.getvalue()).strip()
    return text or "{}", rc not in (0, 3)


def serve():
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue  # notification
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "agentboard", "version": "0.1.0"},
                   "instructions": "Task board for AI agents. Call ab_guide once to learn the workflow."}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": [{"name": n, "description": d, "inputSchema": schema(p, f, r)} for n, (d, _, p, f, r) in TOOLS.items()]}
        elif method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
                continue
            text, is_err = call(name, params.get("arguments") or {})
            res = {"content": [{"type": "text", "text": text}], "isError": is_err}
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": res})
