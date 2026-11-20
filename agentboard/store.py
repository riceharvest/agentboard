"""SQLite store: boards, tasks, deps, messages, events, grants, runs, usage, limits, control, schedules."""
import fnmatch
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("AB_HOME", "~/.agentboard")).expanduser()
DB_PATH = HOME / "board.db"
DEFAULT_BOARD = "main"

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards(name TEXT PRIMARY KEY, description TEXT DEFAULT '', paused INTEGER DEFAULT 0,
  max_slots INTEGER, default_tier TEXT, default_workdir TEXT, created_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, board TEXT DEFAULT 'main', title TEXT NOT NULL, body TEXT DEFAULT '', channel TEXT DEFAULT 'main',
  status TEXT NOT NULL DEFAULT 'open', priority INTEGER DEFAULT 0, labels TEXT DEFAULT '{}',
  tier TEXT DEFAULT 'medium', harness TEXT, model TEXT, workdir TEXT, runnable INTEGER DEFAULT 1,
  attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, timeout_s INTEGER,
  owner TEXT, lease_until REAL, not_before REAL, result TEXT, data TEXT, park TEXT, run_id INTEGER, cmd TEXT, key TEXT, verify TEXT,
  created_by TEXT, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority);
CREATE TABLE IF NOT EXISTS plans(board TEXT, key TEXT, ids TEXT, created_by TEXT, created_at REAL, PRIMARY KEY(board, key));
CREATE TABLE IF NOT EXISTS deps(task_id TEXT, dep_id TEXT, PRIMARY KEY(task_id, dep_id));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, board TEXT, channel TEXT, thread TEXT, author TEXT,
  body TEXT, data TEXT, mentions TEXT DEFAULT '[]', ts REAL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread, id);
CREATE INDEX IF NOT EXISTS messages_chan ON messages(board, channel, id);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, board TEXT, channel TEXT, task_id TEXT, actor TEXT,
  kind TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS grants(id INTEGER PRIMARY KEY, board TEXT DEFAULT '*', who TEXT, channel TEXT DEFAULT '*',
  perms TEXT, note TEXT, created_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS watchers(id INTEGER PRIMARY KEY, board TEXT, name TEXT, spec TEXT, action TEXT,
  enabled INTEGER DEFAULT 1, last_event_id INTEGER DEFAULT 0, last_fired REAL, fired INTEGER DEFAULT 0,
  created_by TEXT, created_at REAL, UNIQUE(board, name));
CREATE TABLE IF NOT EXISTS syncs(id INTEGER PRIMARY KEY, board TEXT, name TEXT, kind TEXT, spec TEXT,
  enabled INTEGER DEFAULT 1, state TEXT DEFAULT '{}', last_run REAL, last_error TEXT,
  created_by TEXT, created_at REAL, UNIQUE(board, name));
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
  started_at REAL, heartbeat REAL, state TEXT, active INTEGER, slots INTEGER, note TEXT, code_ver TEXT);
CREATE TABLE IF NOT EXISTS models(harness TEXT, model TEXT, label TEXT, seen_at REAL, PRIMARY KEY(harness, model));
"""

# Every runtime setting lives in `control`, so any agent with admin rights can change it with `ab config set`.
DEFAULTS = {
    "slots": 2,                 # parallel agents when no schedule window says otherwise
    "launch_per_tick": 2,       # smooth starts: at most K new runs started per runner tick overall
    "pacer": True,              # per-bucket launch budgets (pace / AIMD / cap); false = only slots, caps and launch_per_tick
    "pace_interval": "10m",     # interval for per-bucket launch budgets (pace / AIMD)
    "gating": "pace",           # "pace": pacer + pace guards; "quota": launch whenever every max_*_pct cap and provider limit allows (no pace lines, no per-interval budgets)
    "paused": False,
    "draining": False,
    "stop": "",                 # "", "graceful", "now"
    "poll_s": 5,
    "lease": "30m",             # lease for agent-held claims; the runner renews its own children
    "timeout": "2h",            # default per-run wall clock
    "min_free_gib": 10,         # don't start an agent below this much free RAM
    "harvest_every_s": 60,
    "sync_every_s": 120,
    "channels": "",             # comma list the runner serves; empty = all
    "share_workdir": False,     # allow two runs in the same workdir at once
    "auto_worktree": True,      # busy git workdir -> run the card in its own `git worktree` (card label worktree=on|off overrides)
    "lockfree_types": ["watch", "decision", "dive", "overseer"],  # label type=X cards that neither wait for nor hold a workdir (label lock=none|hold overrides)
    "tier_fallback": "any",     # none | up | down | any: when every candidate of a tier is limited
    "default_tier": "medium",
    "cooldown.limit": "30m",    # used when a limit error carries no reset time
    "cooldown.unavailable": "5m",
    "cooldown.auth": "6h",
    "stall_retry_after": "10m", # a run stuck in provider 429 retries this long -> LIMIT on its bucket, killed, no attempt counted (0/off = never)
    "stall_idle_after": "45m",  # a run showing no sign of life this long -> killed + retried once, then the overseer (0/off = never; card label stall=off|90m)
    "stall_cooldown": "30m",    # bucket cooldown after a retry stall (a later provider reset hint wins)
    "stall_retry_count": "off", # opt-in: kill as a limit once a run logged this many provider 429 retries since its last good model call (off = time rule only)
    "stall_idle_cooldown": "off",  # opt-in: an idle kill also cools the run's bucket for this long, like a limit (off = no cooldown)
    "stall_idle_blind": "off",  # opt-in: also apply the idle rule to harnesses with no log to read, using output growth (they may print only at exit)
    "limit_resume": "off",      # opt-in: workers checkpoint (commits + progress file); a retry after a limit death gets that state in its brief
    "limit_skip_after": "2",    # with limit_resume on: a card's route (bucket) with this many limit deaths is skipped for that card (0/off = never)
    "verify_rounds": "2",       # cards with --verify CMD: failed verifications that reopen the card before it is failed (0 = the first failure fails it)
    "release_loop_runs": "3",   # a card whose last N runs each ended released (no `ab done`) within release_loop_minutes gets a not_before backoff (0/off = never)
    "release_loop_minutes": "3",  # what counts as a short run for the rule above
    "release_loop_backoff": "10m",  # first backoff; doubles with every further short released run, capped at release_loop_max
    "release_loop_max": "6h",
    "verify_timeout": "10m",    # a verify command running longer than this counts as a failure
    "backoff": "2m",            # wait before retrying a failed task (times attempt number)
    "require_free.opencode": True,  # opencode only on :free / -free models
    "hermes_isolate": True,     # run Hermes in a per-run HERMES_HOME (strips bot tokens, stops ephemeral gateway on kill/exit)
    "tiers": {
        "max": ["claude:opus@high", "codex:@xhigh", "agy:Claude Opus 5.5 (High)", "agy:Gemini 4 Argon (High)"],
        "high": ["claude:sonnet@high", "codex:@high", "agy:Claude Sonnet 5.5 (High)", "agy:Gemini 4 Neon (High)"],
        "medium": ["claude:sonnet@medium", "codex:@medium", "agy:Claude Sonnet 5.5 (Medium)", "agy:Gemini 4 Neon (Medium)",
                   "opencode:opencode/deepseek-v4.1-flash-free"],
        "low": ["opencode:opencode/deepseek-v4.1-flash-free", "agy:Gemini 3.8 Flash (Medium)", "codex:@low", "claude:haiku"],
    },
    "tier_order": ["low", "medium", "high", "max"],
    "caps": {},                 # bucket -> {"window": "5h", "max_output": N, "max_cost": USD, "max_runs": N}
    "bucket_map": {},           # "harness/model-glob" -> bucket, overrides the built-in mapping
    "extra_args": {},           # harness -> [args] appended to every launch
    "extra_args_tier": {},      # tier -> {harness -> [args]} appended after extra_args for entries of that tier
    "endpoints": {},            # name -> {"base_url", "api_key_env", "context", "bucket"}: OpenAI-compatible servers
    "kill_runs": [],            # run ids `ab kill` asked the runner to stop (the runner clears them)
}

PERMS = ("read", "post", "create", "claim", "manage", "admin")
SYSTEM_ACTORS = ("runner", "board")
SYSTEM_PREFIXES = ("sched:", "watch:", "sync:")
# Runner-launched workers act as "<harness>#<run>". By default they can work but not steer the board.
DEFAULT_GRANTS = [("*", "*#*", "*", "read,post,create,claim", "runner workers: work, don't administer")]


def now():
    return time.time()


def connect(path=None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def connect_ro(path=None):
    """Read-only connection for UIs (`ab snapshot`): no schema script, no migration, no writes.
    None when the db is missing or still needs a migration (the caller falls back to connect())."""
    p = Path(path or DB_PATH)
    if not p.exists():
        return None
    try:
        # (Path.as_uri() costs ~40 ms: escape by hand what a SQLite URI needs escaped)
        path = os.path.abspath(p).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
        conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        ok = ("board" in _cols(conn, "tasks") and {"board", "channel"} <= _cols(conn, "events")
              and "endpoint" in _cols(conn, "runs") and "park" in _cols(conn, "tasks")
              and conn.execute("SELECT 1 FROM control WHERE key='_schema'").fetchone() is not None)
    except sqlite3.Error:
        return None
    if not ok:
        conn.close()
        return None
    return conn


def code_version():
    """Fingerprint of the installed package code: a hash of (name, size, mtime) of every .py file next to this one.
    The runner records it at start; a later difference means the runner still runs older code (`ab runner reload`)."""
    import hashlib
    h = hashlib.sha1()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        st = f.stat()
        h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:10]


STALE_MSG = "runner code older than installed code: ab runner reload"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    if "board" not in _cols(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN board TEXT DEFAULT 'main'")
    ev = _cols(conn, "events")
    if "board" not in ev:
        conn.execute("ALTER TABLE events ADD COLUMN board TEXT")
    if "channel" not in ev:
        conn.execute("ALTER TABLE events ADD COLUMN channel TEXT")
    if "endpoint" not in _cols(conn, "runs"):
        conn.execute("ALTER TABLE runs ADD COLUMN endpoint TEXT")
    if "provider" not in _cols(conn, "runs"):
        conn.execute("ALTER TABLE runs ADD COLUMN provider TEXT")
    if "park" not in _cols(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN park TEXT")
    if "cmd" not in _cols(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN cmd TEXT")
    if "key" not in _cols(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN key TEXT")
    if "verify" not in _cols(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN verify TEXT")
    if "code_ver" not in _cols(conn, "runner"):
        conn.execute("ALTER TABLE runner ADD COLUMN code_ver TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS tasks_key ON tasks(board, key)")
    conn.execute("CREATE INDEX IF NOT EXISTS tasks_board ON tasks(board, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS events_board ON events(board, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS tasks_status_upd ON tasks(status, updated_at)")
    if conn.execute("SELECT value FROM control WHERE key='_schema'").fetchone() is not None:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT value FROM control WHERE key='_schema'").fetchone() is None:
            # notes -> messages (thread = task id)
            for n in conn.execute("SELECT n.*, t.board, t.channel FROM notes n LEFT JOIN tasks t ON t.id=n.task_id").fetchall():
                conn.execute("INSERT INTO messages(board,channel,thread,author,body,data,mentions,ts) VALUES(?,?,?,?,?,?,'[]',?)",
                             (n["board"] or DEFAULT_BOARD, n["channel"], n["task_id"], n["author"], n["body"], n["data"], n["ts"]))
            conn.execute("DELETE FROM notes")
            conn.execute("INSERT OR IGNORE INTO boards(name,description,created_by,created_at) VALUES(?,?,?,?)",
                         (DEFAULT_BOARD, "default board", "board", now()))
            for b, who, ch, perms, note_ in DEFAULT_GRANTS:
                conn.execute("INSERT INTO grants(board,who,channel,perms,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                             (b, who, ch, perms, note_, "board", now()))
            conn.execute("INSERT INTO control(key,value) VALUES('_schema','2')")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


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
def parse_dur(s, default_unit="s"):
    """'90s' '30m' '2h' '1d' '1w' '1h30m' or a plain number of seconds -> seconds (float)."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    rest = str(s).strip()
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)", rest)
    if not parts or "".join(a + b for a, b in parts) != re.sub(r"\s+", "", rest):
        raise ValueError(f"bad duration: {s!r}")
    total = 0.0
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
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
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
    sec = max(0, int(sec))
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


def event(conn, task_id, actor, kind, payload=None, board=None, channel=None):
    if task_id and (board is None or channel is None):
        r = conn.execute("SELECT board, channel FROM tasks WHERE id=?", (task_id,)).fetchone()
        if r:
            board, channel = board or r["board"], channel or r["channel"]
    cur = conn.execute("INSERT INTO events(ts,board,channel,task_id,actor,kind,payload) VALUES(?,?,?,?,?,?,?)",
                       (now(), board, channel, task_id, actor, kind, json.dumps(payload) if payload is not None else None))
    return cur.lastrowid


# ---------------------------------------------------------------- boards
def board_row(conn, name):
    return conn.execute("SELECT * FROM boards WHERE name=?", (name,)).fetchone()


def require_board(conn, name):
    row = board_row(conn, name)
    if row is None:
        names = ", ".join(r["name"] for r in conn.execute("SELECT name FROM boards ORDER BY name"))
        raise LookupError(f"no board {name!r} (boards: {names}); create it with `ab boards create {name}`")
    return row


def board_create(conn, name, actor, description="", max_slots=None, default_tier=None, default_workdir=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name or ""):
        raise ValueError("board names: letters, digits, _ . - (max 64)")
    with tx(conn):
        if board_row(conn, name):
            raise ValueError(f"board {name!r} exists")
        conn.execute("INSERT INTO boards(name,description,max_slots,default_tier,default_workdir,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                     (name, description or "", max_slots, default_tier, default_workdir, actor, now()))
        event(conn, None, actor, "board_created", {"name": name}, board=name)


def board_update(conn, name, actor, **fields):
    allowed = {"description", "paused", "max_slots", "default_tier", "default_workdir"}
    with tx(conn):
        require_board(conn, name)
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot set {k}")
            conn.execute(f"UPDATE boards SET {k}=? WHERE name=?", (v, name))
        event(conn, None, actor, "board_updated", {"name": name, **fields}, board=name)


def board_delete(conn, name, actor, force=False):
    with tx(conn):
        require_board(conn, name)
        if name == DEFAULT_BOARD:
            raise ValueError("the main board can't be removed")
        live = conn.execute("SELECT COUNT(*) FROM tasks WHERE board=? AND status IN ('open','claimed')", (name,)).fetchone()[0]
        if live and not force:
            raise ValueError(f"board {name} has {live} open/claimed tasks (use --force to cancel them)")
        conn.execute("UPDATE tasks SET status='cancelled', owner=NULL, lease_until=NULL WHERE board=? AND status IN ('open','claimed')", (name,))
        for t in ("watchers", "syncs"):
            conn.execute(f"DELETE FROM {t} WHERE board=?", (name,))
        conn.execute("DELETE FROM grants WHERE board=?", (name,))
        conn.execute("DELETE FROM boards WHERE name=?", (name,))
        event(conn, None, actor, "board_deleted", {"name": name}, board=name)


# ---------------------------------------------------------------- access (guardrails, not security: actors are self-declared)
def parse_perms(s):
    s = (s or "").strip().lower()
    if s in ("all", "*"):
        return set(PERMS)
    if s in ("none", ""):
        return set()
    out = {p.strip() for p in s.split(",") if p.strip()}
    bad = out - set(PERMS)
    if bad:
        raise ValueError(f"unknown permissions {sorted(bad)}; use {', '.join(PERMS)}, all, none")
    return out


def is_system(actor):
    return actor in SYSTEM_ACTORS or any(actor.startswith(p) for p in SYSTEM_PREFIXES)


def _who_rank(who):
    if who == "*":
        return 0
    return 1 if any(ch in who for ch in "*?[") else 2


def explain(conn, actor, perm, board, channel=None, grants=None):
    """-> (allowed, deciding grants). Most specific channel scope wins, then board-specific, then exact actor.
    `grants` = the whole grants table, pre-loaded by callers that check many tasks (snapshot)."""
    if is_system(actor):
        return True, []
    best, chosen = None, []
    rows = grants if grants is not None else conn.execute("SELECT * FROM grants WHERE board IN (?, '*')", (board,)).fetchall()
    for g in rows:
        if g["board"] not in (board, "*"):
            continue
        if not fnmatch.fnmatchcase(actor, g["who"]):
            continue
        scope = (g["channel"] or "*").strip()
        if scope != "*":
            if perm == "admin":
                continue  # admin is board-wide
            s = scope.rstrip("/")
            if channel is None or not (channel == s or channel.startswith(s + "/")):
                continue
        spec = (0 if scope == "*" else len(scope.rstrip("/")), 0 if g["board"] == "*" else 1, _who_rank(g["who"]))
        if best is None or spec > best:
            best, chosen = spec, [g]
        elif spec == best:
            chosen.append(g)
    if best is None:
        return True, []  # no grant mentions this actor here: open by default
    perms = set()
    for g in chosen:
        perms |= parse_perms(g["perms"])
    return perm in perms, [dict(g) for g in chosen]


def can(conn, actor, perm, board, channel=None):
    return explain(conn, actor, perm, board, channel)[0]


def require(conn, actor, perm, board, channel=None):
    if not can(conn, actor, perm, board, channel):
        raise PermissionError(f"{actor} has no '{perm}' on {board}:{channel or '*'} (see `ab grants explain {actor} {perm}`)")


def _task_actor_ok(conn, row, actor, perm="manage"):
    """Owner and creator may act on their own task; everyone else needs `manage` on its channel."""
    if actor in (row["owner"], row["created_by"]) or is_system(actor):
        return
    require(conn, actor, perm, row["board"], row["channel"])


def _can_mark_negative(conn, row, actor):
    """Owner, creator, system, or any actor with write access (`post`, `claim`, or `manage`) on the card."""
    if actor in (row["owner"], row["created_by"]) or is_system(actor):
        return True
    return any(can(conn, actor, p, row["board"], row["channel"]) for p in ("post", "claim", "manage"))


def is_negative(task):
    """True when a task is marked with label outcome=negative (intended negative, not an operational failure)."""
    labels = task["labels"] if isinstance(task, dict) else task["labels"]
    if isinstance(labels, str):
        labels = json.loads(labels or "{}")
    return (labels or {}).get("outcome") == "negative"


# ---------------------------------------------------------------- tasks
def new_id(conn):
    while True:
        tid = "t_" + os.urandom(4).hex()
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            return tid


def msg_dict(r):
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d.get("data") else None
    d["mentions"] = json.loads(d.get("mentions") or "[]")
    return d


def thread_messages(conn, thread, limit=None):
    rows = conn.execute("SELECT * FROM messages WHERE thread=? ORDER BY id", (thread,)).fetchall()
    if limit:
        rows = rows[-limit:]
    return [msg_dict(r) for r in rows]


def task_dict(conn, row, full=False):
    if row is None:
        return None
    d = dict(row)
    d["labels"] = json.loads(d.get("labels") or "{}")
    d["data"] = json.loads(d["data"]) if d.get("data") else None
    d["park"] = json.loads(d["park"]) if d.get("park") else None
    d["kind"] = "cmd" if d.get("cmd") else "agent"
    d["deps"] = [r["dep_id"] for r in conn.execute("SELECT dep_id FROM deps WHERE task_id=?", (d["id"],))]
    d["state"] = derived_state(conn, d)
    if full:
        d["notes"] = thread_messages(conn, d["id"])
        d["children"] = [r["task_id"] for r in conn.execute("SELECT task_id FROM deps WHERE dep_id=?", (d["id"],))]
        d["runs"] = [dict(r) for r in conn.execute(
            "SELECT id,harness,endpoint,provider,model,effort,bucket,started_at,ended_at,outcome,exit_code,input,output,cache_read,"
            "cache_write,reasoning,cost_usd,detail FROM runs WHERE task_id=? ORDER BY id", (d["id"],))]
    return d


def park_active(park):
    """True when a park spec (JSON string, dict or None) is still waiting (not woken yet)."""
    if isinstance(park, str):
        park = json.loads(park) if park else None
    return bool(park) and not park.get("woken_at")


def park_cond(park):
    """Human text for what a parked card waits on."""
    if isinstance(park, str):
        park = json.loads(park) if park else None
    if not park:
        return ""
    if park.get("pid"):
        return f"pid {park['pid']} to exit"
    if park.get("reply_thread"):
        return f"a reply in thread {park['reply_thread']}"
    if park.get("file"):
        return f"file {park['file']} (stable for {fmt_dur(park.get('grace_s') or 0)})"
    return "a wake condition"


def derived_state(conn, d):
    """open tasks are 'ready', 'waiting' (deps/not_before/park) or 'stuck' (a dep is dead/cancelled)."""
    if d["status"] != "open":
        return d["status"]
    if park_active(d.get("park")):
        return "waiting"
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
    if not tid:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        rows = conn.execute("SELECT * FROM tasks WHERE id LIKE ?", (tid + "%",)).fetchall()
        if len(rows) == 1:
            row = rows[0]
    return row


def must_task(conn, tid):
    row = get_task(conn, tid)
    if row is None:
        raise LookupError(f"no task {tid}")
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


def find_key(conn, board, key, live=False, skip=None):
    """Id of the card holding idempotency key `key` on `board` (not cancelled; live=True: only open/claimed and never
    card `skip`, the card the caller runs as, so a card can re-queue its own successor), else None."""
    sts = "('open','claimed')" if live else "('open','claimed','done','dead')"
    r = conn.execute(f"SELECT id FROM tasks WHERE board=? AND key=? AND status IN {sts} AND id IS NOT ? ORDER BY created_at LIMIT 1",
                     (board, key, skip if live else None)).fetchone()
    return r["id"] if r else None


def add_task(conn, actor, title, body="", channel="main", labels=None, priority=0, deps=(), not_before=None,
             workdir=None, tier=None, harness=None, model=None, runnable=True, max_attempts=3, timeout_s=None, data=None,
             board=DEFAULT_BOARD, _in_tx=False, cmd=None, key=None, key_live=False, _info=None, verify=None):
    """_info (dict) gets existed=True when `key` already named a card (nothing is created; its id is returned)."""
    def go():
        b = require_board(conn, board)
        ch = channel or "main"
        require(conn, actor, "create", board, ch)
        if key:
            old = find_key(conn, board, key, key_live, os.environ.get("AB_TASK"))
            if old:
                if _info is not None:
                    _info["existed"] = True
                return old
        tid = new_id(conn)
        for d in deps:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise ValueError(f"dependency {d} does not exist")
        tr = tier or b["default_tier"] or cfg(conn, "default_tier")
        tiers = cfg(conn, "tiers")
        if tr not in tiers:
            raise ValueError(f"unknown tier {tr!r}; tiers: {', '.join(tiers)}")
        t = now()
        conn.execute(
            "INSERT INTO tasks(id,board,title,body,channel,status,priority,labels,tier,harness,model,workdir,runnable,"
            "max_attempts,timeout_s,not_before,data,created_by,created_at,updated_at,cmd,key,verify) VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, board, title, body or "", ch, int(priority or 0), json.dumps(labels or {}), tr, harness, model,
             workdir or b["default_workdir"], 1 if runnable else 0, int(max_attempts), timeout_s, not_before,
             json.dumps(data) if data is not None else None, actor, t, t, cmd or None, key or None, verify or None))
        for d in deps:
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (tid, d))
        event(conn, tid, actor, "created", {"title": title, "deps": list(deps), "tier": tr, "labels": labels or {},
                                            **({"cmd": cmd} if cmd else {}), **({"key": key} if key else {}),
                                            **({"verify": verify} if verify else {})})
        return tid
    if _in_tx:
        return go()
    with tx(conn):
        return go()


_ID_REF_RE = re.compile(r"\{\{id:([^{}\s]+)\}\}")


def _ref_text(x):
    kw = x["kw"]
    return (kw.get("body") or "") + "\n" + "\n".join((kw.get("labels") or {}).values())


def add_plan(conn, actor, specs, board=DEFAULT_BOARD, plan_key=None, key_live=False):
    """Create a whole plan of cards in ONE transaction (all or nothing). specs: [{"key": str|None, "deps": [{"key": K}|{"id": ID}],
    "kw": add_task kwargs}]. Deps by key resolve inside the plan first, then to a card already on the board with that key.
    A card whose key is already on the board is reused, not duplicated. With plan_key, a rerun is a no-op that returns the
    recorded {key: id} map. Returns {"ids": {key-or-#index: id}, "created": n, "existed": bool}."""
    keys = [x["key"] for x in specs if x.get("key")]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"duplicate card key in plan: {', '.join(dup)}")
    with tx(conn):
        require_board(conn, board)
        if plan_key:
            rec = conn.execute("SELECT ids FROM plans WHERE board=? AND key=?", (board, plan_key)).fetchone()
            if rec:
                ids = json.loads(rec["ids"])
                if any(conn.execute("SELECT 1 FROM tasks WHERE id=? AND status!='cancelled'", (i,)).fetchone() for i in ids.values()):
                    return {"ids": ids, "created": 0, "existed": True}
        label = {i: (x.get("key") or f"#{i}") for i, x in enumerate(specs)}
        bykey = {x["key"]: i for i, x in enumerate(specs) if x.get("key")}
        need = {}
        for i, x in enumerate(specs):
            need[i] = set()
            refs = [d["key"] for d in x.get("deps") or [] if "key" in d] + _ID_REF_RE.findall(_ref_text(x))
            for k in refs:
                if k in bykey:
                    if bykey[k] == i:
                        raise ValueError(f"card {label[i]} depends on itself")
                    need[i].add(bykey[k])
        order, left = [], set(need)
        while left:
            ready = sorted(i for i in left if not (need[i] & left))
            if not ready:
                raise ValueError("dependency cycle among: " + ", ".join(label[i] for i in sorted(left)))
            order += ready
            left -= set(ready)
        ids, created = {}, 0
        for i in order:
            x = specs[i]
            dep_ids = []
            for d in x.get("deps") or []:
                if "key" in d:
                    got = ids.get(d["key"]) or find_key(conn, board, d["key"])
                    if not got:
                        raise ValueError(f"card {label[i]}: dependency key {d['key']!r} is neither in the plan nor on the board")
                else:
                    got = d["id"]
                dep_ids.append(got)
            kw = dict(x["kw"])
            if "{{id:" in _ref_text(x):  # {{id:KEY}} in a body or label value becomes that card's id (that card is created first: an ordering edge)
                def sub(m):
                    got = ids.get(m.group(1)) or find_key(conn, board, m.group(1))
                    if not got:
                        raise ValueError(f"card {label[i]}: {{{{id:{m.group(1)}}}}} names no card in the plan or on the board")
                    return got
                kw["body"] = _ID_REF_RE.sub(sub, kw.get("body") or "")
                kw["labels"] = {k: _ID_REF_RE.sub(sub, v) for k, v in (kw.get("labels") or {}).items()}
            info = {}
            ids[label[i]] = add_task(conn, actor, board=board, deps=dep_ids, key=x.get("key"), key_live=key_live,
                                     _in_tx=True, _info=info, **kw)
            created += 0 if info.get("existed") else 1
        ids = {label[i]: ids[label[i]] for i in range(len(specs))}
        if plan_key:
            conn.execute("INSERT OR REPLACE INTO plans(board,key,ids,created_by,created_at) VALUES(?,?,?,?,?)",
                         (board, plan_key, json.dumps(ids), actor, now()))
        return {"ids": ids, "created": created, "existed": False}


GRANT_REFUSAL_RE = re.compile(r"no admin grant|has no '\w+' on [\w.-]+:", re.I)


def _fail_attempt(conn, row, actor, why, kind="failed"):
    """Count an attempt; dead after max_attempts, else back to open with backoff."""
    attempts = (row["attempts"] or 0) + 1
    if kind == "failed" and GRANT_REFUSAL_RE.search(why or ""):
        # ab itself refused the action for lack of a grant: another non-admin worker would hit the same wall.
        # Hold the card for the overseer (label needs_admin=overseer, not auto-run) instead of retrying.
        lb = json.loads(row["labels"] or "{}")
        lb["needs_admin"] = "overseer"
        conn.execute("UPDATE tasks SET status='open', attempts=?, owner=NULL, lease_until=NULL, park=NULL, runnable=0, labels=?, updated_at=? WHERE id=?",
                     (attempts, json.dumps(lb), now(), row["id"]))
        event(conn, row["id"], actor, "needs_admin", {"why": why, "attempts": attempts})
        post_message(conn, "runner", f"Refused by a grant ({(why or '')[:200]}). Not retried: held for the overseer (label needs_admin=overseer, "
                     f"not auto-run). Grant what is needed, then: ab edit {row['id']} --auto -l needs_admin=", thread=row["id"], check=False, _in_tx=True)
        return "open"
    if attempts >= (row["max_attempts"] or 3):
        conn.execute("UPDATE tasks SET status='dead', attempts=?, owner=NULL, lease_until=NULL, park=NULL, result=?, updated_at=? WHERE id=?",
                     (attempts, why, now(), row["id"]))
        event(conn, row["id"], actor, "dead", {"why": why, "attempts": attempts, "after": kind})
        _report_back(conn, row, actor, f"DEAD after {attempts} attempts: {why}")
        return "dead"
    nb = now() + (parse_dur(cfg(conn, "backoff")) or 0) * attempts
    conn.execute("UPDATE tasks SET status='open', attempts=?, owner=NULL, lease_until=NULL, park=NULL, not_before=?, updated_at=? WHERE id=?",
                 (attempts, nb, now(), row["id"]))
    event(conn, row["id"], actor, kind, {"why": why, "attempts": attempts, "retry_at": nb})
    return "open"


def reap(conn):
    """Expired leases go back to the pool (counted as an attempt). Runner-held claims are renewed by the runner."""
    t = now()
    rows = conn.execute("SELECT id FROM tasks WHERE status='claimed' AND lease_until IS NOT NULL AND lease_until < ?", (t,)).fetchall()
    if not rows:
        return 0
    with tx(conn):
        for r in rows:
            cur = conn.execute("SELECT * FROM tasks WHERE id=?", (r["id"],)).fetchone()
            if cur["status"] == "claimed" and cur["lease_until"] and cur["lease_until"] < t:
                _fail_attempt(conn, cur, "board", f"lease expired (owner {cur['owner']})", "lease_expired")
    return len(rows)


READY_SQL = """
SELECT t.* FROM tasks t JOIN boards b ON b.name=t.board
WHERE t.status='open' AND (t.not_before IS NULL OR t.not_before <= :now)
  AND (t.park IS NULL OR json_extract(t.park, '$.woken_at') IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM deps x LEFT JOIN tasks p ON p.id=x.dep_id
                  WHERE x.task_id=t.id AND (p.status IS NULL OR p.status!='done'))
"""


def ready_tasks(conn, channel=None, labels=None, runnable_only=False, exclude=(), limit=200, board=None, skip_paused=False):
    sql, args = READY_SQL, {"now": now()}
    if board:
        sql += " AND t.board=:board"
        args["board"] = board
    if skip_paused:
        sql += " AND b.paused=0"
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


def norm_dir(d):
    return os.path.realpath(os.path.expanduser(str(d))) if d else ""


def is_git_dir(d):
    """True when `d` is an existing directory inside a git checkout or worktree (does not cross filesystems)."""
    if not d:
        return False
    p = Path(os.path.expanduser(str(d)))
    if not p.is_dir():
        return False
    p = p.resolve()
    try:
        dev = p.stat().st_dev
    except OSError:
        return False
    for cur in (p, *p.parents):
        if cur == cur.parent:
            break
        try:
            if cur.stat().st_dev != dev:
                break
        except OSError:
            break
        if (cur / ".git").exists():
            return True
    return False


def lockfree(conn, labels):
    """Non-code cards (watch/decision/dive/overseer, or label lock=none) don't take the workdir lock."""
    lock = str((labels or {}).get("lock", "")).lower()
    if lock:
        return lock in ("none", "off", "no", "free")
    return str((labels or {}).get("type", "")) in (cfg(conn, "lockfree_types") or [])


def want_worktree(conn, labels):
    """Whether a busy git workdir should auto-create a per-task worktree for this card."""
    raw = str((labels or {}).get("worktree", "")).lower()
    if raw:
        return raw in ("on", "1", "yes", "true")
    return bool(cfg(conn, "auto_worktree"))


def busy_dirs(conn):
    """Workdirs held right now by ANY claimed card (any board, any harness, any owner) that took the lock."""
    held = set()
    for r in conn.execute("SELECT workdir, labels FROM tasks WHERE status='claimed' AND workdir IS NOT NULL AND workdir!=''"):
        lb = json.loads(r["labels"] or "{}")
        if not lb.get("worktree_dir") and not lockfree(conn, lb):
            held.add(norm_dir(r["workdir"]))
    return held


def workdir_wait_reason(conn, task, held=None):
    """If a ready runnable task will really wait on a busy workdir, return the reason text; else None."""
    if not task.get("runnable", True) or cfg(conn, "share_workdir"):
        return None
    lb = task.get("labels") or {}
    if isinstance(lb, str):
        lb = json.loads(lb or "{}")
    if lockfree(conn, lb):
        return None
    wd = task.get("workdir")
    if not wd:
        return None
    if held is None:
        held = busy_dirs(conn)
    if norm_dir(wd) not in held:
        return None
    if not is_git_dir(wd):
        return "workdir busy"
    if not want_worktree(conn, lb):
        return "workdir busy (auto worktree off)"
    return None


def claim(conn, actor, tid=None, channel=None, labels=None, lease_s=None, board=DEFAULT_BOARD):
    lease_s = lease_s or parse_dur(cfg(conn, "lease"))
    reap(conn)
    with tx(conn):
        if tid:
            row = must_task(conn, tid)
            if row["id"] not in {r["id"] for r in ready_tasks(conn, limit=100000)}:
                return None, f"task {row['id']} is {derived_state(conn, dict(row))}, not ready"
            require(conn, actor, "claim", row["board"], row["channel"])
        else:
            rows = [r for r in ready_tasks(conn, channel, labels, board=board, limit=500)
                    if can(conn, actor, "claim", r["board"], r["channel"])]
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
    row = must_task(conn, tid)
    if force:
        _task_actor_ok(conn, row, actor)
        return row
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


# ---------------------------------------------------------------- messages
MENTION_RE = re.compile(r"(?<![\w@/.])@([A-Za-z0-9_][A-Za-z0-9_.#:-]*[A-Za-z0-9_#*])")


def resolve_thread(conn, thread):
    """-> (board, channel, kind, thread id). Threads are task ids (t_...) or root message ids (m123)."""
    thread = (thread or "").strip()
    if re.fullmatch(r"m\d+", thread):
        r = conn.execute("SELECT board, channel, thread FROM messages WHERE id=?", (int(thread[1:]),)).fetchone()
        if r is None:
            raise LookupError(f"no thread {thread}")
        root = r["thread"]
        if root.startswith("t_"):
            return r["board"], r["channel"], "task", root
        return r["board"], r["channel"], "post", root
    row = get_task(conn, thread)
    if row is None:
        raise LookupError(f"no thread {thread} (use a task id or m<message id>)")
    return row["board"], row["channel"], "task", row["id"]


def post_message(conn, actor, body, board=None, channel=None, thread=None, data=None, check=True, _in_tx=False):
    """New thread (board+channel) or reply (thread). Returns (message id, thread id, event id)."""
    def go():
        nonlocal board, channel, thread
        if thread:
            board, channel, kind, thread = resolve_thread(conn, thread)
            if check:
                row = get_task(conn, thread) if kind == "task" else None
                if not (row is not None and actor in (row["owner"], row["created_by"])):
                    require(conn, actor, "post", board, channel)
        else:
            require_board(conn, board)
            channel = channel or "main"
            if check:
                require(conn, actor, "post", board, channel)
        mentions = sorted({m.rstrip(".:") for m in MENTION_RE.findall(body or "")})
        cur = conn.execute("INSERT INTO messages(board,channel,thread,author,body,data,mentions,ts) VALUES(?,?,?,?,?,?,?,?)",
                           (board, channel, thread, actor, body, json.dumps(data) if data is not None else None,
                            json.dumps(mentions), now()))
        mid = cur.lastrowid
        if not thread:
            thread = f"m{mid}"
            conn.execute("UPDATE messages SET thread=? WHERE id=?", (thread, mid))
        task_id = thread if thread.startswith("t_") else None
        if task_id:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), task_id))
        eid = event(conn, task_id, actor, "message", {"msg": mid, "thread": thread, "mentions": mentions,
                                                      "body": (body or "")[:400], "data": data}, board=board, channel=channel)
        return mid, thread, eid
    if _in_tx:
        return go()
    with tx(conn):
        return go()


def note(conn, tid, actor, body, data=None):
    row = must_task(conn, tid)
    post_message(conn, actor, body, thread=row["id"], data=data)
    return row["id"]


def _report_back(conn, row, actor, text):
    """Tasks made with --reply-to / `ab followup` post their outcome into that thread."""
    target = json.loads(row["labels"] or "{}").get("reply_to")
    if not target:
        return
    try:
        post_message(conn, actor, f"[{row['id']}] {text}", thread=target, check=False, _in_tx=True,
                     data={"from_task": row["id"]})
    except LookupError:
        pass


def _complete(conn, row, actor, result, data=None):
    conn.execute("UPDATE tasks SET status='done', result=?, data=COALESCE(?,data), owner=NULL, lease_until=NULL, park=NULL, updated_at=? WHERE id=?",
                 (result, json.dumps(data) if data is not None else None, now(), row["id"]))
    event(conn, row["id"], actor, "done", {"result": (result or "")[:500]})
    _report_back(conn, row, actor, result or "done")


def complete(conn, tid, actor, result="", data=None, force=False):
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        _complete(conn, row, actor, result, data)
    return row["id"]


def answer(conn, tid, actor, text):
    """A decision card (manual: the runner never starts it) gets its answer: a note in its thread, then done.
    Refuses cards that are not manual, are already closed, or are held by a run."""
    with tx(conn):
        row = must_task(conn, tid)
        _task_actor_ok(conn, row, actor)
        if row["runnable"]:
            raise ValueError(f"task {row['id']} is not a manual card (the runner works it); use `ab done --force` if you really mean it")
        if row["status"] not in ("open", "claimed"):
            raise ValueError(f"task {row['id']} is already {row['status']}")
        if row["run_id"] and conn.execute("SELECT 1 FROM runs WHERE id=? AND ended_at IS NULL", (row["run_id"],)).fetchone():
            raise PermissionError(f"task {row['id']} is held by a live run")
        post_message(conn, actor, text, thread=row["id"], check=False, _in_tx=True, data={"answer": True})
        _complete(conn, row, actor, text, {"answered_by": actor})
    return row["id"]


def fail(conn, tid, actor, why, force=False, negative=False):
    with tx(conn):
        if negative:
            row = must_task(conn, tid)
            if row["status"] == "dead":
                if not _can_mark_negative(conn, row, actor):
                    raise PermissionError(f"{actor} has no write access on {row['board']}:{row['channel']}")
                cur = json.loads(row["labels"] or "{}")
                changed = {"outcome": "negative"} if cur.get("outcome") != "negative" else {}
                cur["outcome"] = "negative"
                res = row["result"] or why
                conn.execute("UPDATE tasks SET labels=?, result=?, updated_at=? WHERE id=?",
                             (json.dumps(cur), res, now(), row["id"]))
                if why:
                    post_message(conn, actor, "NEGATIVE: " + why, thread=row["id"], check=False, _in_tx=True)
                if changed:
                    event(conn, row["id"], actor, "label", {"changed": changed})
                return row["id"], "dead"
            if row["status"] == "claimed":
                if row["owner"] != actor and not force:
                    raise PermissionError(f"task {row['id']} is held by {row['owner']}, you are {actor} (use --force)")
                if row["owner"] != actor:
                    _task_actor_ok(conn, row, actor)
            elif row["status"] == "open":
                if force:
                    _task_actor_ok(conn, row, actor)
                elif not _can_mark_negative(conn, row, actor):
                    raise PermissionError(f"{actor} has no write access on {row['board']}:{row['channel']}")
            else:
                raise ValueError(f"task {row['id']} is already {row['status']}")
            cur = json.loads(row["labels"] or "{}")
            changed = {"outcome": "negative"} if cur.get("outcome") != "negative" else {}
            cur["outcome"] = "negative"
            attempts = (row["attempts"] or 0) + 1
            conn.execute("UPDATE tasks SET status='dead', attempts=?, owner=NULL, lease_until=NULL, "
                         "labels=?, result=?, updated_at=? WHERE id=?",
                         (attempts, json.dumps(cur), why, now(), row["id"]))
            if why:
                post_message(conn, actor, "NEGATIVE: " + why, thread=row["id"], check=False, _in_tx=True)
            event(conn, row["id"], actor, "dead", {"why": why, "attempts": attempts, "after": "negative", "outcome": "negative"})
            if changed:
                event(conn, row["id"], actor, "label", {"changed": changed})
            _report_back(conn, row, actor, f"NEGATIVE: {why}" if why else "NEGATIVE")
            return row["id"], "dead"
        row = _owned(conn, tid, actor, force)
        if why:
            post_message(conn, actor, "FAILED: " + why, thread=row["id"], check=False, _in_tx=True)
        state = _fail_attempt(conn, row, actor, why)
    return row["id"], state


def release(conn, tid, actor, why="", force=False, count_attempt=False):
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        if count_attempt:
            return row["id"], _fail_attempt(conn, row, actor, why or "released")
        conn.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now(), row["id"]))
        if why:
            post_message(conn, actor, "RELEASED: " + why, thread=row["id"], check=False, _in_tx=True)
        event(conn, row["id"], actor, "released", {"why": why})
    return row["id"], "open"


def defer(conn, tid, actor, until=None, after=(), why="", force=False):
    with tx(conn):
        row = must_task(conn, tid)
        if row["status"] not in ("open", "claimed"):
            raise PermissionError(f"task {row['id']} is {row['status']}")
        if row["status"] == "claimed" and row["owner"] != actor and not force:
            raise PermissionError(f"task {row['id']} is held by {row['owner']} (use --force)")
        _task_actor_ok(conn, row, actor)
        for d in after:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise ValueError(f"dependency {d} does not exist")
            if would_cycle(conn, row["id"], d):
                raise ValueError(f"{d} would create a dependency cycle")
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (row["id"], d))
        conn.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, not_before=COALESCE(?,not_before), updated_at=? WHERE id=?",
                     (until, now(), row["id"]))
        if why:
            post_message(conn, actor, "DEFERRED: " + why, thread=row["id"], check=False, _in_tx=True)
        event(conn, row["id"], actor, "deferred", {"until": until, "after": list(after), "why": why})
    return row["id"]


def park(conn, tid, actor, note, pid=None, pid_start=None, file=None, grace_s=60.0, force=False, reply=None):
    """Park a claimed card on a long external job: release the claim WITHOUT counting an attempt and record
    the wake condition (process gone, or file present and stable for grace_s). The runner wakes it later."""
    if not note or not note.strip():
        raise ValueError("ab park needs --note: what you wait for and what the next agent should do with the result")
    if bool(pid) + bool(file) + bool(reply) != 1:
        raise ValueError("give exactly one of --pid PID, --file PATH or --reply THREAD")
    with tx(conn):
        row = _owned(conn, tid, actor, force)
        if row["status"] not in ("open", "claimed"):
            raise ValueError(f"task {row['id']} is {row['status']}, cannot park")
        spec = {"note": note.strip(), "at": now()}
        if reply:
            th = str(reply).strip()
            if conn.execute("SELECT 1 FROM messages WHERE thread=? LIMIT 1", (th,)).fetchone() is None:
                raise ValueError(f"no thread {th!r} (post the question first: ab ask CHANNEL \"...\" --wait 0s prints the thread id)")
            mine = conn.execute("SELECT MAX(id) m FROM messages WHERE thread=? AND author=?", (th, actor)).fetchone()["m"]
            last = conn.execute("SELECT MAX(id) m FROM messages WHERE thread=?", (th,)).fetchone()["m"]
            # replies newer than this agent's own last post in the thread wake the card (one that arrived
            # between the ask and the park still counts)
            spec.update(reply_thread=th, reply_after=mine or last, reply_by=actor)
        elif pid:
            spec.update(pid=int(pid), pid_start=pid_start)
        else:
            path = os.path.abspath(os.path.expanduser(file or ""))
            spec.update(file=path, grace_s=float(grace_s or 0))
            try:
                st = os.stat(path)
                spec.update(file_size=st.st_size, file_mtime=st.st_mtime, stable_since=now())
            except OSError:
                pass  # not there yet: wake when it appears and stops growing
        live = conn.execute("SELECT id FROM runs WHERE task_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                            (row["id"],)).fetchone()
        spec["run"] = live["id"] if live else None
        conn.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, park=?, updated_at=? WHERE id=?",
                     (json.dumps(spec), now(), row["id"]))
        post_message(conn, actor, f"PARKED on {park_cond(spec)}: {spec['note']}", thread=row["id"], check=False, _in_tx=True)
        event(conn, row["id"], actor, "parked", spec)
    return row["id"], spec


def park_wake(conn, tid, why, actor="runner"):
    """Mark a park woken (inside a tx): the card becomes ready again; the note and wake reason stay in the
    park spec so the next agent's brief can carry them, until a new run starts."""
    row = must_task(conn, tid)
    spec = json.loads(row["park"]) if row["park"] else None
    if not spec or spec.get("woken_at"):
        return None
    spec["woken_at"], spec["woke"] = now(), why
    conn.execute("UPDATE tasks SET park=?, updated_at=? WHERE id=?", (json.dumps(spec), now(), tid))
    post_message(conn, actor, f"PARK WOKE ({why}): {spec.get('note', '')}", thread=tid, check=False, _in_tx=True)
    event(conn, tid, actor, "park_woken", {"why": why, "run": spec.get("run")})
    return spec


def cancel(conn, tid, actor, why=""):
    with tx(conn):
        row = must_task(conn, tid)
        _task_actor_ok(conn, row, actor)
        conn.execute("UPDATE tasks SET status='cancelled', owner=NULL, lease_until=NULL, park=NULL, result=COALESCE(NULLIF(?,''),result), updated_at=? WHERE id=?",
                     (why, now(), row["id"]))
        event(conn, row["id"], actor, "cancelled", {"why": why})
    return row["id"]


def retry(conn, tid, actor):
    with tx(conn):
        row = must_task(conn, tid)
        _task_actor_ok(conn, row, actor)
        if row["status"] == "claimed":
            raise PermissionError(f"task {row['id']} is claimed by {row['owner']}")
        cur = json.loads(row["labels"] or "{}")
        if cur.get("outcome") == "negative":
            cur.pop("outcome", None)
            conn.execute("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(cur), row["id"]))
        conn.execute("UPDATE tasks SET status='open', attempts=0, owner=NULL, lease_until=NULL, not_before=NULL, park=NULL, updated_at=? WHERE id=?",
                     (now(), row["id"]))
        event(conn, row["id"], actor, "retry", {"was": row["status"]})
    return row["id"]


EDITABLE = {"cmd", "verify", "title", "body", "channel", "priority", "tier", "harness", "model", "workdir", "runnable",
            "max_attempts", "timeout_s", "not_before", "board"}


def edit(conn, tid, actor, fields, labels=None, add_deps=(), rm_deps=()):
    with tx(conn):
        row = must_task(conn, tid)
        if not fields and not add_deps and not rm_deps and row["status"] == "dead" and labels == {"outcome": "negative"}:
            if not _can_mark_negative(conn, row, actor):
                raise PermissionError(f"{actor} has no write access on {row['board']}:{row['channel']}")
        else:
            _task_actor_ok(conn, row, actor)
        if "tier" in fields and fields["tier"] not in cfg(conn, "tiers"):
            raise ValueError(f"unknown tier {fields['tier']!r}")
        if "board" in fields or "channel" in fields:
            nb = fields.get("board") or row["board"]
            require_board(conn, nb)
            require(conn, actor, "create", nb, fields.get("channel") or row["channel"])
        for k, v in fields.items():
            if k not in EDITABLE:
                raise ValueError(f"cannot edit {k}")
            conn.execute(f"UPDATE tasks SET {k}=? WHERE id=?", (v, row["id"]))
        changed = {}
        if labels:
            cur = json.loads(row["labels"] or "{}")
            for k, v in labels.items():
                if v in (None, ""):
                    if k in cur:
                        changed[k] = None
                    cur.pop(k, None)
                else:
                    if cur.get(k) != v:
                        changed[k] = v
                    cur[k] = v
            conn.execute("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(cur), row["id"]))
        for d in add_deps:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise ValueError(f"dependency {d} does not exist")
            if would_cycle(conn, row["id"], d):
                raise ValueError(f"{d} would create a dependency cycle")
            conn.execute("INSERT OR IGNORE INTO deps(task_id,dep_id) VALUES(?,?)", (row["id"], d))
        for d in rm_deps:
            conn.execute("DELETE FROM deps WHERE task_id=? AND dep_id=?", (row["id"], d))
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), row["id"]))
        event(conn, row["id"], actor, "edited", {"fields": list(fields), "add_deps": list(add_deps), "rm_deps": list(rm_deps)})
        if changed:
            event(conn, row["id"], actor, "label", {"changed": changed})
    return row["id"]


def anomalies(conn, actor="agent", board=None, since=None):
    """Scan visible tasks for anomalies: dead cards (skipping outcome=negative) and open cards stuck on dead/cancelled deps."""
    sql, args = "SELECT * FROM tasks", []
    if board:
        require_board(conn, board)
        sql += " WHERE board=?"
        args.append(board)
    rows = [r for r in conn.execute(sql, args).fetchall() if can(conn, actor, "read", r["board"], r["channel"])]
    byid = {r["id"]: r for r in rows}
    out = []
    for r in sorted(rows, key=lambda x: x["updated_at"] or 0):
        if r["status"] == "dead":
            if is_negative(r):
                continue
            if since is not None and (r["updated_at"] or 0) < since:
                continue
            out.append({"kind": "dead", "task_id": r["id"], "board": r["board"], "title": r["title"],
                        "updated_at": r["updated_at"], "message": f"dead card {r['id']} {r['title'][:60]}"})
    for r in sorted(rows, key=lambda x: x["created_at"] or 0):
        if r["status"] == "open":
            deps = [d["dep_id"] for d in conn.execute("SELECT dep_id FROM deps WHERE task_id=?", (r["id"],))]
            if not deps:
                continue
            bad = []
            for d in deps:
                p = byid.get(d) or conn.execute("SELECT status FROM tasks WHERE id=?", (d,)).fetchone()
                if p and p["status"] in ("dead", "cancelled"):
                    bad.append(d)
            if bad:
                out.append({"kind": "blocked", "task_id": r["id"], "board": r["board"], "deps": bad,
                            "title": r["title"], "updated_at": r["updated_at"],
                            "message": f"blocked: {r['id']} <{r['board']}> waits on dead/cancelled {','.join(bad)}: {r['title'][:60]}"})
    for r in conn.execute("SELECT * FROM tasks WHERE status='open' AND json_extract(labels,'$.needs_admin')='overseer'"):
        if board and r["board"] != board or not can(conn, actor, "read", r["board"], r["channel"]):
            continue
        out.append({"kind": "needs-admin", "task_id": r["id"], "board": r["board"], "title": r["title"], "updated_at": r["updated_at"],
                    "message": f"needs-admin: {r['id']} <{r['board']}> was refused by a grant, held for the overseer: {r['title'][:60]}"})
    for r in conn.execute("SELECT * FROM tasks WHERE status='open' AND json_extract(labels,'$.stalled')='overseer'"):
        if board and r["board"] != board or not can(conn, actor, "read", r["board"], r["channel"]):
            continue
        out.append({"kind": "stalled", "task_id": r["id"], "board": r["board"], "title": r["title"], "updated_at": r["updated_at"],
                    "message": f"stalled: {r['id']} <{r['board']}> went idle twice, held for the overseer: {r['title'][:60]}"})
    return out


# ---------------------------------------------------------------- limits (cooldowns)
def set_limit(conn, scope, kind, until, reason, actor="runner"):
    conn.execute("INSERT INTO limits(scope,kind,until,reason,set_at) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(scope) DO UPDATE SET kind=excluded.kind, until=MAX(excluded.until, limits.until), "
                 "reason=excluded.reason, set_at=excluded.set_at", (scope, kind, until, reason, now()))
    event(conn, None, actor, "limit", {"scope": scope, "kind": kind, "until": until, "reason": (reason or "")[:300]})


def active_limits(conn):
    return {r["scope"]: dict(r) for r in conn.execute("SELECT * FROM limits WHERE until > ?", (now(),))}


def clear_limit(conn, scope, actor="?"):
    conn.execute("DELETE FROM limits WHERE scope=?", (scope,))
    event(conn, None, actor, "limit_cleared", {"scope": scope})


def park_replies(conn, spec):
    """Messages that answer a reply-park: newer than the parker's last post in the thread, from anyone else
    (not the parking agent itself, not the runner)."""
    return conn.execute("SELECT * FROM messages WHERE thread=? AND id>? AND author NOT IN (?, 'runner') ORDER BY id",
                        (spec["reply_thread"], spec.get("reply_after") or 0, spec.get("reply_by") or "")).fetchall()
