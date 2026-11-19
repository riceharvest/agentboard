"""SQLite store: tasks, deps, notes, events, runs, usage, limits, control, schedules."""
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("AB_HOME", "~/.agentboard")).expanduser()
DB_PATH = HOME / "board.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '', channel TEXT DEFAULT 'main',
  status TEXT NOT NULL DEFAULT 'open', priority INTEGER DEFAULT 0, labels TEXT DEFAULT '{}',
  tier TEXT DEFAULT 'medium', harness TEXT, model TEXT, workdir TEXT, runnable INTEGER DEFAULT 1,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, timeout_s INTEGER,
  owner TEXT, lease_until REAL, not_before REAL, result TEXT, data TEXT, run_id INTEGER,
  created_by TEXT, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority);
CREATE TABLE IF NOT EXISTS deps(task_id TEXT, dep_id TEXT, PRIMARY KEY(task_id, dep_id));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, data TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS notes_task ON notes(task_id, id);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, task_id TEXT, actor TEXT, kind TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, task_id TEXT, harness TEXT, model TEXT, effort TEXT, tier TEXT, bucket TEXT,
  pid INTEGER, pid_start TEXT, started_at REAL, ended_at REAL, exit_code INTEGER, outcome TEXT,
  session_id TEXT, input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0,
  cache_write INTEGER DEFAULT 0, reasoning INTEGER DEFAULT 0, cost_usd REAL, detail TEXT, out_path TEXT, err_path TEXT);
CREATE INDEX IF NOT EXISTS runs_open ON runs(ended_at);
CREATE TABLE IF NOT EXISTS usage(
  source TEXT, key TEXT, ts REAL, harness TEXT, bucket TEXT, model TEXT, task_id TEXT,
  input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
  reasoning INTEGER DEFAULT 0, cost_usd REAL, PRIMARY KEY(source, key));
CREATE INDEX IF NOT EXISTS usage_bucket_ts ON usage(bucket, ts);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS cursors(source TEXT, key TEXT, value TEXT, PRIMARY KEY(source, key));
CREATE TABLE IF NOT EXISTS limits(scope TEXT PRIMARY KEY, kind TEXT, until REAL, reason TEXT, set_at REAL);
CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, spec TEXT, action TEXT, enabled INTEGER DEFAULT 1,
  last_fired REAL, created_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS runner(id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER, host TEXT,
  started_at REAL, heartbeat REAL, state TEXT, active INTEGER, slots INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS models(harness TEXT, model TEXT, label TEXT, seen_at REAL, PRIMARY KEY(harness, model));
"""

# Every runtime setting lives in `control`, so any agent can change it with `ab config set`.
DEFAULTS = {
    "slots": 2,                 # parallel agents when no schedule window says otherwise
    "paused": False,
    "draining": False,
    "stop": "",                 # "", "graceful", "now"
    "poll_s": 5,
    "lease": "30m",             # lease for agent-held claims; the runner renews its own children
    "timeout": "2h",            # default per-run wall clock
    "min_free_gib": 10,         # don't start an agent below this much free RAM
    "harvest_every_s": 60,
    "channels": "",             # comma list the runner serves; empty = all
    "share_workdir": False,     # allow two runs in the same workdir at once
    "tier_fallback": "none",    # none | up | down | any: when every candidate of a tier is limited
    "default_tier": "medium",
    "cooldown.limit": "30m",    # used when a limit error carries no reset time
    "cooldown.unavailable": "5m",
    "cooldown.auth": "6h",
    "backoff": "2m",            # wait before retrying a failed task (times attempt number)
    "require_free.opencode": True,  # opencode only on :free / -free models
    "tiers": {
        "max": ["claude:opus@high", "agy:Claude Opus 5.5 (High)", "agy:Gemini 4 Argon (High)"],
        "high": ["claude:sonnet@high", "agy:Claude Sonnet 5.5 (High)", "agy:Gemini 4 Neon (High)"],
        "medium": ["claude:sonnet@medium", "agy:Claude Sonnet 5.5 (Medium)", "agy:Gemini 4 Neon (Medium)",
                   "opencode:opencode/deepseek-v4.1-flash-free"],
        "low": ["opencode:opencode/deepseek-v4.1-flash-free", "agy:Gemini 3.8 Flash (Medium)", "claude:haiku"],
    },
    "tier_order": ["low", "medium", "high", "max"],
    "caps": {},                 # bucket -> {"window": "5h", "max_output": N, "max_cost": USD, "max_runs": N}
    "bucket_map": {},           # "harness/model-glob" -> bucket, overrides the built-in mapping
    "extra_args": {},           # harness -> [args] appended to every launch
}


def now():
    return time.time()


def connect(path=None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- parsing helpers
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)?\s*$", re.I)


def parse_dur(s, default_unit="s"):
    """'90s' '30m' '2h' '1d' '1w' or a plain number of seconds -> seconds (float)."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    total, rest = 0.0, str(s).strip()
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)", rest)
    if not parts or "".join(a + b for a, b in parts) != re.sub(r"\s+", "", rest):
        raise ValueError(f"bad duration: {s!r}")
    for num, unit in parts:
        u = (unit or default_unit).lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}.get(u[0])
        if u.startswith("mo") or mult is None:
            raise ValueError(f"bad duration unit: {s!r}")
        total += float(num) * mult
    return total


def parse_time(s, base=None):
    """'+2h', 'HH:MM' (next occurrence), 'YYYY-MM-DD HH:MM', ISO, or epoch -> epoch seconds."""
    if s is None or s == "":
        return None
    s = str(s).strip()
    base = base or now()
    if s.startswith("+"):
        return base + parse_dur(s[1:])
    if re.fullmatch(r"\d{9,11}(\.\d+)?", s):
        return float(s)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        b = datetime.fromtimestamp(base)
        t = b.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        if t.timestamp() <= base:
            t += timedelta(days=1)
        return t.timestamp()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        pass
    try:
        return base + parse_dur(s)
    except ValueError:
        raise ValueError(f"bad time: {s!r} (use +2h, 14:30, 2026-11-20 03:00, or epoch)")


def fmt_ts(t):
    if not t:
        return "-"
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 0:
        sec = 0
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


# ---------------------------------------------------------------- control / config
def cfg(conn, key):
    row = conn.execute("SELECT value FROM control WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
    return json.loads(row["value"])


def cfg_all(conn):
    out = dict(DEFAULTS)
    for r in conn.execute("SELECT key, value FROM control"):
        out[r["key"]] = json.loads(r["value"])
    return out


def cfg_set(conn, key, value, actor="?"):
    conn.execute("INSERT INTO control(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    event(conn, None, actor, "config", {"key": key, "value": value})


def cfg_unset(conn, key, actor="?"):
    conn.execute("DELETE FROM control WHERE key=?", (key,))
    event(conn, None, actor, "config", {"key": key, "value": None, "reset": True})


def coerce(value):
    """CLI string -> JSON-ish value."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    try:
        return json.loads(v)
    except ValueError:
        pass
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    return v


def event(conn, task_id, actor, kind, payload=None):
    conn.execute("INSERT INTO events(ts,task_id,actor,kind,payload) VALUES(?,?,?,?,?)",
                 (now(), task_id, actor, kind, json.dumps(payload) if payload is not None else None))


# ---------------------------------------------------------------- tasks
def new_id(conn):
    while True:
        tid = "t_" + secrets.token_hex(4)
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            return tid


def task_dict(conn, row, full=False):
    if row is None:
        return None
    d = dict(row)
    d["labels"] = json.loads(d.get("labels") or "{}")
    d["data"] = json.loads(d["data"]) if d.get("data") else None
    d["deps"] = [r["dep_id"] for r in conn.execute("SELECT dep_id FROM deps WHERE task_id=?", (d["id"],))]
    d["state"] = derived_state(conn, d)
    if full:
        d["notes"] = [dict(r) | {"data": json.loads(r["data"]) if r["data"] else None}
                      for r in conn.execute("SELECT author,body,data,ts FROM notes WHERE task_id=? ORDER BY id", (d["id"],))]
        d["children"] = [r["task_id"] for r in conn.execute("SELECT task_id FROM deps WHERE dep_id=?", (d["id"],))]
        d["runs"] = [dict(r) for r in conn.execute(
            "SELECT id,harness,model,effort,bucket,started_at,ended_at,outcome,exit_code,input,output,cache_read,"
            "cache_write,reasoning,cost_usd,detail FROM runs WHERE task_id=? ORDER BY id", (d["id"],))]
    return d


def derived_state(conn, d):
    """open tasks are 'ready', 'waiting' (deps/not_before) or 'stuck' (a dep is dead/cancelled)."""
    if d["status"] != "open":
        return d["status"]
    bad = conn.execute("SELECT 1 FROM deps x JOIN tasks p ON p.id=x.dep_id WHERE x.task_id=? AND p.status IN ('dead','cancelled')",
                       (d["id"],)).fetchone()
    if bad:
        return "stuck"
    pending = conn.execute("SELECT 1 FROM deps x LEFT JOIN tasks p ON p.id=x.dep_id WHERE x.task_id=? AND (p.status IS NULL OR p.status!='done')",
                           (d["id"],)).fetchone()
    if pending or (d.get("not_before") and d["not_before"] > now()):
        return "waiting"
    return "ready"


def get_task(conn, tid):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        # allow unique prefix
        rows = conn.execute("SELECT * FROM tasks WHERE id LIKE ?", (tid + "%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
    return row


def would_cycle(conn, tid, dep):
    seen, stack = set(), [dep]
    while stack:
        cur = stack.pop()
        if cur == tid:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack += [r["dep_id"] for r in conn.execute("SELECT dep_id FROM deps WHERE task_id=?", (cur,))]
    return False


def add_task(conn, actor, title, body="", channel="main", labels=None, priority=0, deps=(), not_before=None,
             workdir=None, tier=None, harness=None, model=None, runnable=True, max_attempts=3, timeout_s=None, data=None):
    with tx(conn):
        tid = new_id(conn)
        for d in deps:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise ValueError(f"dependency {d} does not exist")
        tier = tier or cfg(conn, "default_tier")
        tiers = cfg(conn, "tiers")
        if tier not in tiers:
            raise ValueError(f"unknown tier {tier!r}; tiers: {', '.join(tiers)}")
        t = now()
        conn.execute(
            "INSERT INTO tasks(id,title,body,channel,status,priority,labels,tier,harness,model,workdir,runnable,"
            "max_attempts,timeout_s,not_before,data,created_by,created_at,updated_at) VALUES(?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, body or "", channel or "main", int(priority or 0), json.dumps(labels or {}), tier, harness, model,
             workdir, 1 if runnable else 0, int(max_attempts), timeout_s, not_before,
             json.dumps(data) if data is not None else None, actor, t, t))
        for d in deps:
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (tid, d))
        event(conn, tid, actor, "created", {"title": title, "deps": list(deps), "tier": tier})
    return tid


def _fail_attempt(conn, row, actor, why, kind="failed"):
    """Count an attempt; dead after max_attempts, else back to open with backoff."""
    attempts = (row["attempts"] or 0) + 1
    if attempts >= (row["max_attempts"] or 3):
        conn.execute("UPDATE tasks SET status='dead', attempts=?, owner=NULL, lease_until=NULL, result=?, updated_at=? WHERE id=?",
                     (attempts, why, now(), row["id"]))
        event(conn, row["id"], actor, "dead", {"why": why, "attempts": attempts, "after": kind})
        return "dead"
    nb = now() + (parse_dur(cfg(conn, "backoff")) or 0) * attempts
    conn.execute("UPDATE tasks SET status='open', attempts=?, owner=NULL, lease_until=NULL, not_before=?, updated_at=? WHERE id=?",
                 (attempts, nb, now(), row["id"]))
    event(conn, row["id"], actor, kind, {"why": why, "attempts": attempts, "retry_at": nb})
    return "open"


def reap(conn):
    """Expired leases go back to the pool (counted as an attempt). Runner-held claims are renewed by the runner."""
    t = now()
    rows = conn.execute("SELECT * FROM tasks WHERE status='claimed' AND lease_until IS NOT NULL AND lease_until < ?", (t,)).fetchall()
    if not rows:
        return 0
    with tx(conn):
        for r in rows:
            cur = conn.execute("SELECT * FROM tasks WHERE id=?", (r["id"],)).fetchone()
            if cur["status"] == "claimed" and cur["lease_until"] and cur["lease_until"] < t:
                _fail_attempt(conn, cur, "board", f"lease expired (owner {cur['owner']})", "lease_expired")
    return len(rows)


READY_SQL = """
SELECT t.* FROM tasks t
WHERE t.status='open' AND (t.not_before IS NULL OR t.not_before <= :now)
  AND NOT EXISTS (SELECT 1 FROM deps x LEFT JOIN tasks p ON p.id=x.dep_id
                  WHERE x.task_id=t.id AND (p.status IS NULL OR p.status!='done'))
"""


def ready_tasks(conn, channel=None, labels=None, runnable_only=False, exclude=(), limit=200):
    sql, args = READY_SQL, {"now": now()}
    if channel:
        chans = [c.strip() for c in channel.split(",") if c.strip()]
        ors = []
        for i, c in enumerate(chans):
            args[f"c{i}"], args[f"cp{i}"] = c, c.rstrip("/") + "/%"
            ors.append(f"(t.channel=:c{i} OR t.channel LIKE :cp{i})")
        sql += " AND (" + " OR ".join(ors) + ")"
    for i, (k, v) in enumerate((labels or {}).items()):
        args[f"lk{i}"], args[f"lv{i}"] = f"$.{json.dumps(k)}", v
        sql += f" AND json_extract(t.labels, :lk{i}) = :lv{i}"
    if runnable_only:
        sql += " AND t.runnable=1"
    sql += " ORDER BY t.priority DESC, t.created_at ASC LIMIT :lim"
    args["lim"] = limit
    return [r for r in conn.execute(sql, args).fetchall() if r["id"] not in exclude]


def claim(conn, actor, tid=None, channel=None, labels=None, lease_s=None):
    lease_s = lease_s or parse_dur(cfg(conn, "lease"))
    reap(conn)
    with tx(conn):
        if tid:
            row = get_task(conn, tid)
            if row is None:
                raise LookupError(f"no task {tid}")
            ready = {r["id"] for r in ready_tasks(conn)}
            if row["id"] not in ready:
                return None, f"task {row['id']} is {derived_state(conn, dict(row))}, not ready"
        else:
            rows = ready_tasks(conn, channel, labels, limit=1)
            if not rows:
                return None, "nothing ready"
            row = rows[0]
        conn.execute("UPDATE tasks SET status='claimed', owner=?, lease_until=?, updated_at=? WHERE id=? AND status='open'",
                     (actor, now() + lease_s, now(), row["id"]))
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            return None, "lost the race, try again"
        event(conn, row["id"], actor, "claimed", {"lease_s": lease_s})
    return row["id"], None


def _owned(conn, tid, actor, force):
    row = get_task(conn, tid)
    if row is None:
        raise LookupError(f"no task {tid}")
    if not force:
        if row["status"] != "claimed":
            raise PermissionError(f"task {row['id']} is {row['status']}, not claimed (use --force)")
        if row["owner"] != actor:
            raise PermissionError(f"task {row['id']} is held by {row['owner']}, you are {actor} (use --force)")
    return row


def renew(conn, tid, actor, lease_s=None, force=False):
    lease_s = lease_s or parse_dur(cfg(conn, "lease"))
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        conn.execute("UPDATE tasks SET lease_until=?, updated_at=? WHERE id=?", (now() + lease_s, now(), row["id"]))
        event(conn, row["id"], actor, "renewed", {"lease_s": lease_s})
    return row["id"]


def note(conn, tid, actor, body, data=None):
    row = get_task(conn, tid)
    if row is None:
        raise LookupError(f"no task {tid}")
    with tx(conn):
        conn.execute("INSERT INTO notes(task_id,author,body,data,ts) VALUES(?,?,?,?,?)",
                     (row["id"], actor, body, json.dumps(data) if data is not None else None, now()))
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), row["id"]))
        event(conn, row["id"], actor, "note", {"chars": len(body or "")})
    return row["id"]


def complete(conn, tid, actor, result="", data=None, force=False):
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        conn.execute("UPDATE tasks SET status='done', result=?, data=COALESCE(?,data), owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                     (result, json.dumps(data) if data is not None else None, now(), row["id"]))
        event(conn, row["id"], actor, "done", {"result": (result or "")[:500]})
    return row["id"]


def fail(conn, tid, actor, why, force=False):
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        if why:
            conn.execute("INSERT INTO notes(task_id,author,body,ts) VALUES(?,?,?,?)", (row["id"], actor, "FAILED: " + why, now()))
        state = _fail_attempt(conn, row, actor, why)
    return row["id"], state


def release(conn, tid, actor, why="", force=False, count_attempt=False):
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        if count_attempt:
            return row["id"], _fail_attempt(conn, row, actor, why or "released")
        conn.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now(), row["id"]))
        if why:
            conn.execute("INSERT INTO notes(task_id,author,body,ts) VALUES(?,?,?,?)", (row["id"], actor, "RELEASED: " + why, now()))
        event(conn, row["id"], actor, "released", {"why": why})
    return row["id"], "open"


def defer(conn, tid, actor, until=None, after=(), why="", force=False):
    with tx(conn):
        row = get_task(conn, tid)
        if row is None:
            raise LookupError(f"no task {tid}")
        if row["status"] == "claimed" and row["owner"] != actor and not force:
            raise PermissionError(f"task {row['id']} is held by {row['owner']} (use --force)")
        if row["status"] not in ("open", "claimed"):
            raise PermissionError(f"task {row['id']} is {row['status']}")
        for d in after:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise ValueError(f"dependency {d} does not exist")
            if would_cycle(conn, row["id"], d):
                raise ValueError(f"{d} would create a dependency cycle")
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (row["id"], d))
        conn.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, not_before=COALESCE(?,not_before), updated_at=? WHERE id=?",
                     (until, now(), row["id"]))
        if why:
            conn.execute("INSERT INTO notes(task_id,author,body,ts) VALUES(?,?,?,?)", (row["id"], actor, "DEFERRED: " + why, now()))
        event(conn, row["id"], actor, "deferred", {"until": until, "after": list(after), "why": why})
    return row["id"]


def cancel(conn, tid, actor, why=""):
    with tx(conn):
        row = get_task(conn, tid)
        if row is None:
            raise LookupError(f"no task {tid}")
        conn.execute("UPDATE tasks SET status='cancelled', owner=NULL, lease_until=NULL, result=COALESCE(NULLIF(?,''),result), updated_at=? WHERE id=?",
                     (why, now(), row["id"]))
        event(conn, row["id"], actor, "cancelled", {"why": why})
    return row["id"]


def retry(conn, tid, actor):
    with tx(conn):
        row = get_task(conn, tid)
        if row is None:
            raise LookupError(f"no task {tid}")
        if row["status"] == "claimed":
            raise PermissionError(f"task {row['id']} is claimed by {row['owner']}")
        conn.execute("UPDATE tasks SET status='open', attempts=0, owner=NULL, lease_until=NULL, not_before=NULL, updated_at=? WHERE id=?",
                     (now(), row["id"]))
        event(conn, row["id"], actor, "retry", {"was": row["status"]})
    return row["id"]


EDITABLE = {"title", "body", "channel", "priority", "tier", "harness", "model", "workdir", "runnable",
            "max_attempts", "timeout_s", "not_before"}


def edit(conn, tid, actor, fields, labels=None, add_deps=(), rm_deps=()):
    with tx(conn):
        row = get_task(conn, tid)
        if row is None:
            raise LookupError(f"no task {tid}")
        if "tier" in fields and fields["tier"] not in cfg(conn, "tiers"):
            raise ValueError(f"unknown tier {fields['tier']!r}")
        for k, v in fields.items():
            if k not in EDITABLE:
                raise ValueError(f"cannot edit {k}")
            conn.execute(f"UPDATE tasks SET {k}=? WHERE id=?", (v, row["id"]))
        if labels:
            cur = json.loads(row["labels"] or "{}")
            for k, v in labels.items():
                if v in (None, ""):
                    cur.pop(k, None)
                else:
                    cur[k] = v
            conn.execute("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(cur), row["id"]))
        for d in add_deps:
            if would_cycle(conn, row["id"], d):
                raise ValueError(f"{d} would create a dependency cycle")
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (row["id"], d))
        for d in rm_deps:
            conn.execute("DELETE FROM deps WHERE task_id=? AND dep_id=?", (row["id"], d))
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), row["id"]))
        event(conn, row["id"], actor, "edited", {"fields": list(fields), "labels": labels, "add_deps": list(add_deps), "rm_deps": list(rm_deps)})
    return row["id"]


# ---------------------------------------------------------------- limits (cooldowns)
def set_limit(conn, scope, kind, until, reason, actor="runner"):
    conn.execute("INSERT INTO limits(scope,kind,until,reason,set_at) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(scope) DO UPDATE SET kind=excluded.kind, until=MAX(excluded.until, limits.until), "
                 "reason=excluded.reason, set_at=excluded.set_at", (scope, kind, until, reason, now()))
    event(conn, None, actor, "limit", {"scope": scope, "kind": kind, "until": until, "reason": (reason or "")[:300]})


def active_limits(conn):
    t = now()
    return {r["scope"]: dict(r) for r in conn.execute("SELECT * FROM limits WHERE until > ?", (t,))}


def clear_limit(conn, scope, actor="?"):
    conn.execute("DELETE FROM limits WHERE scope=?", (scope,))
    event(conn, None, actor, "limit_cleared", {"scope": scope})
