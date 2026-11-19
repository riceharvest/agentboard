"""Event wake-ups (wait / tail / inbox / watchers), follow-ups, and GitHub two-way sync."""
import fnmatch
import json
import re
import subprocess
import time

from . import store
from .store import event, now, tx


# ---------------------------------------------------------------- reading the event log
def ev_dict(r):
    d = dict(r)
    d["payload"] = json.loads(d["payload"]) if d.get("payload") else None
    return d


def max_event_id(conn):
    return conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]


def mentions_me(mentions, me):
    return any(m == me or fnmatch.fnmatchcase(me, m) or (m.endswith("#") and me.startswith(m)) for m in (mentions or []))


def match_event(e, kinds=None, board=None, channel=None, thread=None, task=None, mention=None, actor=None,
                not_actor=None, text=None, label=None):
    p = e.get("payload") or {}
    if kinds and e["kind"] not in kinds:
        return False
    if board and e.get("board") != board:
        return False
    if channel:
        ch, s = e.get("channel") or "", channel.rstrip("/")
        if not (ch == s or ch.startswith(s + "/")):
            return False
    if thread and not (p.get("thread") == thread or e.get("task_id") == thread):
        return False
    if task and e.get("task_id") != task:
        return False
    if mention and not mentions_me(p.get("mentions"), mention):
        return False
    if actor and not fnmatch.fnmatchcase(e.get("actor") or "", actor):
        return False
    if not_actor and (e.get("actor") or "") == not_actor:
        return False
    if text and not re.search(text, json.dumps(p), re.I):
        return False
    if label:
        k, _, v = label.partition("=")
        changed = (p.get("changed") or {}) if e["kind"] == "label" else (p.get("labels") or {})
        if k not in changed or (v and str(changed.get(k)) != v):
            return False
    return True


def readable(conn, actor, e):
    if not e.get("board"):
        return True  # global events (config, limits, runner)
    return store.can(conn, actor, "read", e["board"], e.get("channel"))


def events_after(conn, after, limit=500, board=None):
    sql, args = "SELECT * FROM events WHERE id > ?", [after]
    if board:
        sql += " AND board=?"
        args.append(board)
    return [ev_dict(r) for r in conn.execute(sql + " ORDER BY id LIMIT ?", args + [limit])]


def wait_for(conn, me, timeout_s, after=None, poll=1.0, first_only=False, **flt):
    """Block until events matching flt arrive after `after` (default: now). -> (events, cursor)."""
    cursor = max_event_id(conn) if after is None else after
    deadline = now() + (timeout_s or 0)
    while True:
        evs = events_after(conn, cursor, board=flt.get("board"))
        if evs:
            cursor = evs[-1]["id"]
        hits = [e for e in evs if match_event(e, **flt) and readable(conn, me, e)]
        if hits:
            return (hits[:1] if first_only else hits), (hits[0]["id"] if first_only else cursor)
        if now() >= deadline:
            return [], cursor
        time.sleep(poll)


def describe_event(conn, e):
    p = e.get("payload") or {}
    head = f"event {e['id']} {e['kind']} by {e['actor']} in {e.get('board') or '-'}/{e.get('channel') or '-'}"
    if e.get("task_id"):
        t = store.get_task(conn, e["task_id"])
        head += f" task {e['task_id']}" + (f" ({t['title'][:80]})" if t else "")
    if e["kind"] == "message":
        m = conn.execute("SELECT body FROM messages WHERE id=?", (p.get("msg"),)).fetchone()
        body = (m["body"] if m else p.get("body")) or ""
        head += f" thread {p.get('thread')}:\n    " + body.strip().replace("\n", "\n    ")[:3000]
    elif p:
        head += " " + json.dumps(p)[:600]
    return head


# ---------------------------------------------------------------- inbox
def inbox(conn, actor, peek=False, limit=50):
    """Messages that mention you, or reply in threads you started / tasks you created or hold."""
    cur = conn.execute("SELECT value FROM cursors WHERE source='inbox' AND key=?", (actor,)).fetchone()
    after = json.loads(cur["value"]) if cur else 0
    rows = conn.execute("SELECT * FROM messages WHERE id > ? AND author != ? ORDER BY id LIMIT 2000", (after, actor)).fetchall()
    mine_threads = {r["thread"] for r in conn.execute("SELECT DISTINCT thread FROM messages WHERE author=?", (actor,))}
    mine_tasks = {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE created_by=? OR owner=?", (actor, actor))}
    out = []
    for r in rows:
        d = store.msg_dict(r)
        if mentions_me(d["mentions"], actor) or d["thread"] in mine_threads or d["thread"] in mine_tasks:
            if store.can(conn, actor, "read", d["board"], d["channel"]):
                out.append(d)
    out = out[:limit]
    if not peek and rows:
        top = out[-1]["id"] if len(out) == limit else rows[-1]["id"]
        conn.execute("INSERT INTO cursors(source,key,value) VALUES('inbox',?,?) ON CONFLICT(source,key) DO UPDATE SET value=excluded.value",
                     (actor, json.dumps(top)))
    return out


# ---------------------------------------------------------------- follow-ups
def thread_context(conn, thread, n=15):
    board, channel, kind, tid = store.resolve_thread(conn, thread)
    lines = []
    if kind == "task":
        t = store.get_task(conn, tid)
        lines.append(f"THREAD {tid} (task, {t['status']}): {t['title']}")
        if t["body"]:
            lines.append(t["body"].strip()[:3000])
        if t["result"]:
            lines.append("RESULT: " + t["result"].strip()[:2000])
    msgs = store.thread_messages(conn, tid)
    if kind == "post" and msgs:
        lines.append(f"THREAD {tid} in {board}/{channel}, started by {msgs[0]['author']}:")
        lines.append((msgs[0]["body"] or "").strip()[:3000])
        msgs = msgs[1:]
    for m in msgs[-n:]:
        lines.append(f"[{store.fmt_ts(m['ts'])} {m['author']}] {(m['body'] or '').strip()[:1500]}")
    return "\n".join(lines)


def followup(conn, actor, thread, prompt, at=None, tier=None, harness=None, model=None, priority=0):
    board, channel, kind, tid = store.resolve_thread(conn, thread)
    workdir = None
    if kind == "task":
        workdir = store.get_task(conn, tid)["workdir"]
    title = "follow-up: " + (prompt.strip().splitlines()[0][:80] if prompt.strip() else tid)
    return store.add_task(conn, actor, title, prompt, channel=channel, board=board, labels={"reply_to": tid, "followup": "1"},
                          not_before=at, workdir=workdir, tier=tier, harness=harness, model=model, priority=priority)


# ---------------------------------------------------------------- watchers
def _watch_owned_task_ids(conn, board, name):
    return {r["id"] for r in conn.execute("SELECT id FROM tasks WHERE board=? AND json_extract(labels,'$.watcher')=?", (board, name))}


def _worker_task(conn, actor):
    m = re.fullmatch(r"[\w-]+#(\d+)", actor or "")
    if not m:
        return None
    r = conn.execute("SELECT task_id FROM runs WHERE id=?", (int(m[1]),)).fetchone()
    return r["task_id"] if r else None


def run_watchers(conn):
    fired = []
    for w in conn.execute("SELECT * FROM watchers WHERE enabled=1").fetchall():
        spec, action = json.loads(w["spec"]), json.loads(w["action"])
        evs = events_after(conn, w["last_event_id"] or 0, board=None if spec.get("all_boards") else w["board"])
        if not evs:
            continue
        own = _watch_owned_task_ids(conn, w["board"], w["name"])
        me = f"watch:{w['name']}"
        hits = []
        for e in evs:
            if e["actor"] == me or e.get("task_id") in own or _worker_task(conn, e["actor"]) in own:
                continue  # never wake on our own echo
            if match_event(e, kinds=spec.get("kinds"), channel=spec.get("channel"), thread=spec.get("thread"),
                           mention=spec.get("mention"), actor=spec.get("actor"), text=spec.get("text"), label=spec.get("label")):
                hits.append(e)
        last = evs[-1]["id"]
        if not hits:
            conn.execute("UPDATE watchers SET last_event_id=? WHERE id=?", (last, w["id"]))
            continue
        cooldown = store.parse_dur(action.get("cooldown") or "0") or 0
        if w["last_fired"] and now() - w["last_fired"] < cooldown:
            continue  # keep the cursor: these events fire once the cooldown passes
        desc = "\n".join(describe_event(conn, e) for e in hits[:25])
        if len(hits) > 25:
            desc += f"\n... and {len(hits) - 25} more"
        with tx(conn):
            live = conn.execute("SELECT * FROM tasks WHERE board=? AND json_extract(labels,'$.watcher')=? AND status IN ('open','claimed') "
                                "ORDER BY created_at DESC LIMIT 1", (w["board"], w["name"])).fetchone()
            if live is not None and action.get("coalesce", True):
                store.post_message(conn, me, "More events arrived:\n" + desc, thread=live["id"], check=False, _in_tx=True)
                tid = live["id"]
            else:
                threads = {(e.get("payload") or {}).get("thread") or e.get("task_id") for e in hits} - {None}
                labels = {**(action.get("labels") or {}), "watcher": w["name"]}
                if action.get("reply", True) and len(threads) == 1:
                    labels["reply_to"] = threads.pop()
                body = (action.get("brief") or "React to these board events.").strip() + "\n\nEVENTS THAT WOKE YOU:\n" + desc
                if labels.get("reply_to"):
                    body += ("\n\nYour final `ab done` result is posted back into thread " + labels["reply_to"]
                             + " automatically. Use `ab reply " + labels["reply_to"] + " \"...\"` for anything you need to say before that.")
                tid = store.add_task(conn, me, f"[watch:{w['name']}] {hits[0]['kind']}" + (f" in {hits[0].get('channel')}" if hits[0].get("channel") else ""),
                                     body, channel=action.get("channel") or hits[0].get("channel") or "main", board=w["board"],
                                     labels=labels, tier=action.get("tier"), harness=action.get("harness"), model=action.get("model"),
                                     workdir=action.get("workdir"), priority=action.get("priority", 0), _in_tx=True)
            conn.execute("UPDATE watchers SET last_event_id=?, last_fired=?, fired=fired+1 WHERE id=?", (last, now(), w["id"]))
            event(conn, tid, me, "watch_fired", {"watcher": w["name"], "events": [e["id"] for e in hits][:50]})
        fired.append((w["name"], tid, len(hits)))
    return fired


# ---------------------------------------------------------------- GitHub sync (via the gh CLI)
MARK = "<!-- agentboard"


def gh(args, timeout=60):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:400]}")
    return p.stdout


def gh_json(args):
    out = gh(args)
    return json.loads(out) if out.strip() else None


def sync_github(conn, s):
    spec, state = json.loads(s["spec"]), json.loads(s["state"] or "{}")
    items = state.setdefault("items", {})
    repo, me = spec["repo"], f"sync:{s['name']}"
    channel = spec.get("channel") or "gh/" + repo.split("/")[-1]
    stats = {"new": 0, "comments_in": 0, "pushed": 0, "closed": 0}
    kinds = ["issue"] + (["pr"] if spec.get("pulls") else [])
    seen = set()
    for kind in kinds:
        args = [kind, "list", "-R", repo, "--state", "open", "--limit", str(spec.get("limit", 100)),
                "--json", "number,title,body,url,updatedAt,author"]
        if spec.get("label"):
            args += ["--label", spec["label"]]
        for it in gh_json(args) or []:
            key = f"{kind}#{it['number']}"
            seen.add(key)
            m = items.get(key)
            if m is None:
                tid = store.add_task(conn, me, f"{repo}#{it['number']}: {it['title']}",
                                     f"GitHub {kind}: {it['url']}\nopened by {((it.get('author') or {}).get('login'))}\n\n{(it.get('body') or '').strip()}",
                                     channel=channel, board=s["board"], tier=spec.get("tier"), workdir=spec.get("workdir"),
                                     labels={"gh": f"{repo}#{it['number']}", "gh_kind": kind, "gh_url": it["url"]},
                                     runnable=not spec.get("manual", False))
                m = items[key] = {"task": tid, "updated": None, "comments": 0, "pushed": [], "done_pushed": False}
                stats["new"] += 1
            if m.get("updated") != it["updatedAt"]:
                view = gh_json([kind, "view", str(it["number"]), "-R", repo, "--json", "comments"]) or {}
                comments = view.get("comments") or []
                for cmt in comments[m.get("comments", 0):]:
                    if MARK in (cmt.get("body") or ""):
                        continue  # our own push
                    who = (cmt.get("author") or {}).get("login") or "unknown"
                    with tx(conn):
                        store.post_message(conn, f"gh:{who}", (cmt.get("body") or "").strip(), thread=m["task"], check=False,
                                           _in_tx=True, data={"gh_comment": cmt.get("url") or cmt.get("id")})
                    stats["comments_in"] += 1
                m["comments"], m["updated"] = len(comments), it["updatedAt"]
    # closed or merged upstream -> cancel the open task
    for key, m in items.items():
        if key in seen or m.get("closed"):
            continue
        t = store.get_task(conn, m["task"])
        if t and t["status"] in ("open", "claimed"):
            kind, num = key.split("#")
            st = (gh_json([kind, "view", num, "-R", repo, "--json", "state"]) or {}).get("state", "")
            if st.upper() in ("CLOSED", "MERGED"):
                store.cancel(conn, t["id"], me, f"{kind} {st.lower()} on GitHub")
                m["closed"] = True
                stats["closed"] += 1
        elif t is None or t["status"] in ("done", "dead", "cancelled"):
            pass
    # push board -> GitHub (opt-in)
    if spec.get("push"):
        for key, m in items.items():
            t = store.get_task(conn, m["task"])
            if t is None:
                continue
            kind, num = key.split("#")
            out = []
            for msg in store.thread_messages(conn, t["id"]):
                if msg["id"] in m["pushed"] or msg["author"].startswith("gh:"):
                    continue
                if (msg.get("data") or {}).get("sync") or spec.get("mirror_all"):
                    out.append((msg["id"], f"**{msg['author']}** (agentboard):\n\n{msg['body']}"))
            if t["status"] == "done" and not m.get("done_pushed"):
                out.append(("done", f"**agentboard**: done.\n\n{(t['result'] or '').strip()}"))
            if t["status"] == "dead" and not m.get("dead_pushed"):
                out.append(("dead", f"**agentboard**: gave up after {t['attempts']} attempts.\n\n{(t['result'] or '').strip()}"))
            for mid, body in out:
                gh([kind, "comment", num, "-R", repo, "--body", f"{body}\n\n{MARK} {t['id']} -->"])
                stats["pushed"] += 1
                if mid == "done":
                    m["done_pushed"] = True
                    if spec.get("close_on_done") and kind == "issue":
                        gh(["issue", "close", num, "-R", repo])
                        m["closed"] = True
                elif mid == "dead":
                    m["dead_pushed"] = True
                else:
                    m["pushed"].append(mid)
    return state, stats


def run_syncs(conn, only=None, force=False):
    results = {}
    every = float(store.cfg(conn, "sync_every_s") or 120)
    for s in conn.execute("SELECT * FROM syncs WHERE enabled=1").fetchall():
        if only and s["name"] != only:
            continue
        if not force and s["last_run"] and now() - s["last_run"] < every:
            continue
        try:
            if s["kind"] != "github":
                raise RuntimeError(f"unknown sync kind {s['kind']}")
            state, stats = sync_github(conn, s)
            conn.execute("UPDATE syncs SET state=?, last_run=?, last_error=NULL WHERE id=?", (json.dumps(state), now(), s["id"]))
            results[s["name"]] = stats
        except Exception as e:
            conn.execute("UPDATE syncs SET last_run=?, last_error=? WHERE id=?", (now(), str(e)[:1000], s["id"]))
            results[s["name"]] = {"error": str(e)[:300]}
    return results
