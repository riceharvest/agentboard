"""Minimal MCP server (stdio, JSON-RPC 2.0). Each tool maps 1:1 onto an `ab` command and returns its --json output."""
import contextlib
import io
import json
import sys

S, I, B, L, KV = "string", "integer", "boolean", "list", "kv"

# tool: (description, subcommand, [positional args], {param: (flag, kind)}, [required])
TOOLS = {
    "ab_add": ("Create a task for agents. tier picks the intelligence (see ab_tiers); deps must finish first. cmd=SHELL COMMAND makes a command card: the runner runs it itself, no model. key=K is an idempotency key (a card with K already on the board -> its id, nothing created). plan=JSON array of cards {key,title,body,labels,deps:[{key}|{id}],tier,workdir,after,priority,manual,cmd} creates the whole chain in one transaction (title not needed); with key a rerun is a no-op returning the key->id map.", "add",
               ["title"], {"body": ("--body", S), "channel": ("--channel", S), "labels": ("--label", KV), "priority": ("--priority", I),
                           "deps": ("--dep", L), "after": ("--after", S), "workdir": ("--workdir", S), "tier": ("--tier", S),
                           "harness": ("--harness", S), "model": ("--model", S), "manual": ("--manual", B),
                           "cmd": ("--cmd", S), "result": ("--result", S),
                           "max_attempts": ("--max-attempts", I), "timeout": ("--timeout", S), "data": ("--data", S),
                           "reply_to": ("--reply-to", S), "key": ("--key", S), "key_live": ("--key-live", B),
                           "plan": ("--plan", S)}, []),
    "ab_list": ("List tasks. Default open+claimed; ready=true for claimable now.", "ls", [],
                {"status": ("--status", S), "ready": ("--ready", B), "channel": ("--channel", S), "labels": ("--label", KV),
                 "limit": ("--limit", I), "all_boards": ("--all-boards", B), "tier": ("--tier", S), "grep": ("--grep", S)}, []),
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
    "ab_park": ("Park your claimed card on a long external job and exit (no attempt counted): exactly one of pid (wake when the "
                "process exits), reply (a thread id: wake when someone else posts in it; the reply is in the next brief) or file (wake when it exists and stops growing for grace, default 60s). note is required: what you "
                "wait for and what the next agent should do. The runner resumes the card with a fresh agent whose brief carries the note.",
                "park", ["id"], {"pid": ("--pid", I), "file": ("--file", S), "reply": ("--reply", S), "grace": ("--grace", S),
                                 "note": ("--note", S), "note_file": ("--note-file", S), "force": ("--force", B)}, []),
    "ab_cancel": ("Cancel a task (stops its run).", "cancel", ["id", "why"], {}, ["id"]),
    "ab_retry": ("Reopen a dead/cancelled/done task with fresh attempts.", "retry", ["id"], {}, ["id"]),
    "ab_edit": ("Change task fields. labels: {key: value} ('' removes).", "edit", ["id"],
                {"title": ("--title", S), "body": ("--body", S), "channel": ("--channel", S), "priority": ("--priority", I),
                 "tier": ("--tier", S), "harness": ("--harness", S), "model": ("--model", S), "workdir": ("--workdir", S),
                 "after": ("--after", S), "timeout": ("--timeout", S), "max_attempts": ("--max-attempts", I),
                 "labels": ("--label", KV), "add_deps": ("--dep", L), "rm_deps": ("--rm-dep", L),
                 "manual": ("--manual", B), "auto": ("--auto", B), "move_to": ("--move-to", S)}, ["id"]),
    "ab_kill": ("Stop one run (the task goes back to the queue, no attempt counted). ab_cancel stops the work for good.", "kill",
                ["run"], {}, ["run"]),
    "ab_decision": ("Decision cards (manual tasks waiting for an answer). action ls|answer; answer = id + text: notes it and closes the card.",
                    "decision", ["action", "id", "text"], {"owner": ("--owner", S), "all_boards": ("--all-boards", B)}, []),
    "ab_snapshot": ("Everything a UI needs in one read-only call: boards, tasks, runner, limits, plan %, usage, watchers, schedules, tiers, "
                    "recent events (events_after), a run's output tail (tail=RUN_ID:BYTES).", "snapshot", [],
                    {"closed": ("--closed", I), "events_after": ("--events-after", I), "events_limit": ("--events-limit", I),
                     "tail": ("--tail", S)}, []),
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
    "ab_post": ("Start a thread in a channel. Mention agents with @name.", "post", ["channel", "text"], {"data": ("--data", S)}, ["channel", "text"]),
    "ab_reply": ("Reply in a thread (task id or m<id>). sync=true mirrors it to the synced GitHub issue/PR.", "reply",
                 ["thread", "text"], {"data": ("--data", S), "sync": ("--sync", B)}, ["thread", "text"]),
    "ab_read": ("Read messages of a channel or thread.", "read", ["channel"],
                {"thread": ("--thread", S), "after": ("--after", I), "roots": ("--roots", B), "limit": ("--limit", I),
                 "all_boards": ("--all-boards", B)}, []),
    "ab_ask": ("Post a question (in channel, or in thread) and block until someone else replies.", "ask", ["channel", "text"],
               {"thread": ("--thread", S), "wait": ("--wait", S), "all": ("--all", B)}, ["text"]),
    "ab_wait": ("Block until a matching event arrives; returns events and a cursor for the next wait.", "wait", [],
                {"mentions": ("--mentions", B), "thread": ("--thread", S), "channel": ("--channel", S), "task": ("--task", S),
                 "kind": ("--kind", S), "from": ("--from", S), "text": ("--text", S), "label_change": ("--label-change", S),
                 "after": ("--after", I), "timeout": ("--timeout", S), "all": ("--all", B), "all_boards": ("--all-boards", B)}, []),
    "ab_tail": ("Recent events (no streaming over MCP); run=RUN_ID prints that run's output instead (last `bytes`).", "tail", [],
                {"kind": ("--kind", S), "channel": ("--channel", S), "task": ("--task", S), "thread": ("--thread", S),
                 "after": ("--after", I), "limit": ("--limit", I), "run": ("--run", I), "bytes": ("--bytes", I)}, []),
    "ab_inbox": ("Messages mentioning you or answering your threads/tasks.", "inbox", [], {"peek": ("--peek", B), "limit": ("--limit", I)}, []),
    "ab_followup": ("Schedule a later follow-up on a thread (e.g. check CI in 5m); its result posts back into the thread.", "followup",
                    ["thread", "prompt"], {"in": ("--in", S), "at": ("--at", S), "tier": ("--tier", S), "harness": ("--harness", S),
                                           "model": ("--model", S)}, ["thread", "prompt"]),
    "ab_watch": ("Watchers wake an agent on board events. action ls|add|rm|enable|disable.", "watch", ["action", "name"],
                 {"on": ("--on", S), "channel": ("--channel", S), "mention": ("--mention", S), "thread": ("--thread", S),
                  "from": ("--from", S), "text": ("--text", S), "label_change": ("--label-change", S), "brief": ("--brief", S),
                  "tier": ("--tier", S), "harness": ("--harness", S), "model": ("--model", S), "workdir": ("--workdir", S),
                  "to_channel": ("--to-channel", S), "cooldown": ("--cooldown", S), "no_reply": ("--no-reply", B),
                  "no_coalesce": ("--no-coalesce", B)}, []),
    "ab_sync": ("GitHub issue/PR sync. action ls|add|run|rm|enable|disable.", "sync", ["action", "name"],
                {"github": ("--github", S), "label": ("--label", S), "all": ("--all", B), "pulls": ("--pulls", B),
                 "channel": ("--channel", S), "tier": ("--tier", S), "workdir": ("--workdir", S), "manual": ("--manual", B),
                 "push": ("--push", B), "close_on_done": ("--close-on-done", B), "mirror_all": ("--mirror-all", B)}, []),
    "ab_grants": ("Access rules. action ls|add|rm|explain. add: who + can (+channel, grant_board).", "grants", ["action", "who", "perm"],
                  {"can": ("--can", S), "channel": ("--channel", S), "grant_board": ("--grant-board", S), "note": ("--note", S)}, []),
    "ab_endpoints": ("OpenAI-compatible servers harnesses can run on (spec HARNESS@ENDPOINT:MODEL). action ls|add|rm|check.",
                     "endpoints", ["action", "name"], {"base_url": ("--base-url", S), "api_key_env": ("--api-key-env", S),
                                                       "context": ("--context", I), "bucket": ("--bucket", S)}, []),
    "ab_boards": ("Boards. action ls|create|set|rm.", "boards", ["action", "name"],
                  {"description": ("--description", S), "max_slots": ("--max-slots", I), "tier": ("--tier", S),
                   "workdir": ("--workdir", S), "pause": ("--pause", B), "resume": ("--resume", B), "force": ("--force", B)}, []),
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
    props["board"] = {"type": "string", "description": "board name (default $AB_BOARD or main)"}
    return {"type": "object", "properties": props, "required": req}


def to_argv(tool, args):
    _, sub, pos, flags, _ = TOOLS[tool]
    argv = ["--json"]
    if args.get("as"):
        argv += ["--as", str(args["as"])]
    if args.get("board"):
        argv += ["--board", str(args["board"])]
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
                   "serverInfo": {"name": "agentboard", "version": "0.2.0"},
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
