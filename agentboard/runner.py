"""The runner: the only thing that starts agents. Plain subprocesses, no admission layer.
Every setting is read from the board each tick, so agents steer it with `ab runner ...` / `ab config set ...` / `ab sched ...`."""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import store
from ._lazy import Lazy
from .store import cfg, event, now, parse_dur, tx

automation, H, usage = Lazy("automation"), Lazy("harness"), Lazy("usage")
# stdlib modules only the runner process itself needs: not worth their import time for `ab snapshot`
fcntl, signal, socket, subprocess = (Lazy(n, absolute=True) for n in ("fcntl", "signal", "socket", "subprocess"))

PKG_ROOT = Path(__file__).resolve().parent.parent     # dir that contains the agentboard package


def ab_path_dirs():
    """Dirs to put on a worker's PATH so `ab` resolves: the installed entry point and/or a git checkout."""
    import shutil
    dirs = []
    w = shutil.which("ab")
    if w:
        dirs.append(str(Path(w).resolve().parent))
    if (PKG_ROOT / "ab").exists():
        dirs.append(str(PKG_ROOT))
    if not dirs:
        dirs.append(str(Path(sys.executable).parent))
    return list(dict.fromkeys(dirs))


def _pythonpath(env):
    return f"{PKG_ROOT}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(PKG_ROOT)
RUNNER_LEASE = 600          # the runner renews its children's claims every tick; this only matters if it dies
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "AI_AGENT", "HERMES_AGENT",
             "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID",
             "AB_TASK", "AB_ACTOR", "AB_RUN", "AB_JSON", "AB_BOARD")

BRIEF = """You are an autonomous worker on agentboard task {id}. No human is watching: do not ask questions of a human, decide and act.
Your name on the board is {actor} (board {board}). Other agents can mention you as @{actor}.

TASK: {title}
board={board} channel={channel} tier={tier} attempt={attempt}/{max_attempts}{labels}
workdir: {workdir}

{body}
{context}{deps}{notes}
BOARD (the `ab` CLI is on PATH; AB_TASK={id}, AB_ACTOR and AB_BOARD are already set for you):
  ab note $AB_TASK "progress, findings, decisions"     keep a trail for whoever picks this up next
  ab add "title" --body "..." [--dep ID] [--tier low|medium|high|max]   hand sub-work to other agents
  ab ask CHANNEL "question for @someone" --wait 10m   ask another agent and block for the answer
  ab reply THREAD "text" ; ab read CHANNEL ; ab inbox   talk in threads (THREAD = task id or m<id>)
  ab followup $AB_TASK "check CI for the PR" --in 5m  schedule a later check that reports back here
  ab defer $AB_TASK --after ID [--why "..."]           park this task until ID is done, then stop
  ab edit $AB_TASK --tier high && ab release $AB_TASK "needs a stronger model"   escalate, then stop
  ab done $AB_TASK "short result: what changed, where, how it was verified"
  ab fail $AB_TASK "why it cannot be done"
  ab show ID | ab ls --ready | ab guide                 read the board
Do the work, verify it for real, then call exactly ONE of done / fail / defer / release and stop.
"""


def harness_scope(harness, endpoint=None):
    """Limits on a harness's own account don't block it on an endpoint, and vice versa."""
    return f"harness:{harness}@{endpoint}" if endpoint else f"harness:{harness}"


def free_gib():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1048576
    except OSError:
        pass
    return 999.0


def pid_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def alive(pid, start):
    if not pid:
        return False
    st = pid_start(pid)
    if st is None:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        return False
    return start is None or st == start


def runner_status(conn):
    r = conn.execute("SELECT * FROM runner WHERE id=1").fetchone()
    d = dict(r) if r else {"running": False}
    if r:
        poll = float(cfg(conn, "poll_s") or 5)
        d["running"] = bool(d["pid"] and alive(d["pid"], None) and d["heartbeat"] and now() - d["heartbeat"] < poll * 3 + 30)
    d["children"] = [dict(x) for x in conn.execute(
        "SELECT r.id run, r.task_id, r.harness, r.endpoint, r.model, r.effort, r.bucket, r.pid, r.started_at, r.out_path, r.err_path, "
        "t.title, t.board, t.timeout_s FROM runs r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.ended_at IS NULL ORDER BY r.id")]
    default_to = parse_dur(cfg(conn, "timeout"))
    for ch in d["children"]:
        ch["timeout_s"] = ch["timeout_s"] or default_to
    return d


# ---------------------------------------------------------------- schedules
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def days_ok(spec_days, dt):
    if not spec_days:
        return True
    allowed = set()
    for part in str(spec_days).lower().split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAYS.index(x[:3]) for x in part.split("-"))
            i = a
            while True:
                allowed.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif part:
            allowed.add(DAYS.index(part[:3]))
    return dt.weekday() in allowed


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def window_active(spec, t=None):
    dt = datetime.fromtimestamp(t or now())
    cur, a, b = dt.hour * 60 + dt.minute, _hm(spec["from"]), _hm(spec["to"])
    if a <= b:
        return a <= cur < b and days_ok(spec.get("days"), dt)
    # overnight window: the day check applies to the day it started
    if cur >= a:
        return days_ok(spec.get("days"), dt)
    if cur < b:
        return days_ok(spec.get("days"), dt - timedelta(days=1))
    return False


def apply_action(conn, action, actor):
    do = action.get("do")
    if do == "pause":
        store.cfg_set(conn, "paused", True, actor)
    elif do == "resume":
        store.cfg_set(conn, "paused", False, actor)
        store.cfg_set(conn, "draining", False, actor)
    elif do == "drain":
        store.cfg_set(conn, "draining", True, actor)
    elif do == "stop":
        store.cfg_set(conn, "stop", "graceful", actor)
    elif do == "reload":
        store.cfg_set(conn, "stop", "reload", actor)
    elif do == "slots":
        store.cfg_set(conn, "slots", int(action["value"]), actor)
    elif do == "set":
        store.cfg_set(conn, action["key"], action["value"], actor)
    else:
        raise ValueError(f"unknown action {do!r}")


def fire_schedules(conn):
    """Fire due one-shot / daily actions. Returns the slot override from active windows (or None)."""
    t = now()
    override = None
    for s in conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall():
        spec, action = json.loads(s["spec"]), json.loads(s["action"])
        actor = f"sched:{s['name']}"
        if s["kind"] == "window":
            if window_active(spec, t):
                override = max(override or 0, int(action.get("slots", 0)))
        elif s["kind"] == "at":
            if t >= spec["at"] and not s["last_fired"]:
                with tx(conn):
                    apply_action(conn, action, actor)
                    conn.execute("UPDATE schedules SET last_fired=?, enabled=0 WHERE id=?", (t, s["id"]))
                    event(conn, None, actor, "schedule_fired", action)
        elif s["kind"] == "daily":
            dt = datetime.fromtimestamp(t)
            hm = _hm(spec["time"])
            due = dt.replace(hour=hm // 60, minute=hm % 60, second=0, microsecond=0).timestamp()
            if t >= due and (s["last_fired"] or 0) < due and days_ok(spec.get("days"), dt) and t - due < 6 * 3600:
                with tx(conn):
                    apply_action(conn, action, actor)
                    conn.execute("UPDATE schedules SET last_fired=? WHERE id=?", (t, s["id"]))
                    event(conn, None, actor, "schedule_fired", action)
    return override


def effective_slots(conn):
    override = fire_schedules(conn)
    return override if override is not None else int(cfg(conn, "slots") or 0), override is not None


# ---------------------------------------------------------------- brief
def build_brief(conn, task, workdir, attempt, actor="?"):
    deps = conn.execute("SELECT p.id, p.title, p.result FROM deps x JOIN tasks p ON p.id=x.dep_id WHERE x.task_id=?",
                        (task["id"],)).fetchall()
    dtxt = ""
    if deps:
        dtxt = "\nRESULTS OF THE TASKS THIS ONE DEPENDS ON:\n" + "\n".join(
            f"- {d['id']} {d['title']}: {(d['result'] or '').strip()[:1500]}" for d in deps) + "\n"
    notes = conn.execute("SELECT author, body, ts FROM notes WHERE task_id=? ORDER BY id DESC LIMIT 12", (task["id"],)).fetchall()
    ntxt = ""
    if notes:
        ntxt = "\nNOTES SO FAR (earlier attempts and other agents, oldest first):\n" + "\n".join(
            f"- [{store.fmt_ts(n['ts'])} {n['author']}] {(n['body'] or '')[:1200]}" for n in reversed(notes)) + "\n"
    labels = json.loads(task["labels"] or "{}")
    ctx = ""
    if labels.get("reply_to"):
        try:
            ctx = ("\nYOU ARE ANSWERING / FOLLOWING UP ON THIS THREAD (your `ab done` result is posted back into it):\n"
                   + automation.thread_context(conn, labels["reply_to"]) + "\n")
        except LookupError:
            pass
    return BRIEF.format(id=task["id"], title=task["title"], channel=task["channel"], tier=task["tier"],
                        actor=actor, board=task["board"], context=ctx,
                        attempt=attempt, max_attempts=task["max_attempts"],
                        labels=(" labels=" + ",".join(f"{k}={v}" for k, v in labels.items())) if labels else "",
                        workdir=workdir, body=(task["body"] or "(no further description)").strip(), deps=dtxt, notes=ntxt)


# ---------------------------------------------------------------- runner
class Child:
    def __init__(self, run_id, task_id, owner, harness, model, bucket, pid, start, proc, timeout_s, workdir, out_path, err_path, started):
        self.__dict__.update(locals())
        del self.__dict__["self"]
        self.kill_reason = None
        self.kill_at = None


class Runner:
    def __init__(self, conn, log=None):
        self.conn = conn
        self.children = {}
        self.host = socket.gethostname()
        self.waiting = {}
        self.last_harvest = 0
        self.log_f = log or sys.stderr
        self.lock_f = None

    def log(self, msg):
        print(f"{datetime.now():%H:%M:%S} {msg}", file=self.log_f, flush=True)

    # -- lifecycle
    def acquire(self):
        store.HOME.mkdir(parents=True, exist_ok=True)
        self.lock_f = open(store.HOME / "runner.lock", "w")
        try:
            fcntl.flock(self.lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another runner is already running")
        self.lock_f.write(str(os.getpid()))
        self.lock_f.flush()

    def heartbeat(self, state, slots, note=None):
        self.conn.execute(
            "INSERT INTO runner(id,pid,host,started_at,heartbeat,state,active,slots,note) VALUES(1,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, host=excluded.host, heartbeat=excluded.heartbeat, state=excluded.state, "
            "active=excluded.active, slots=excluded.slots, note=excluded.note, "
            "started_at=CASE WHEN runner.pid=excluded.pid THEN runner.started_at ELSE excluded.started_at END",
            (os.getpid(), self.host, now(), now(), state, len(self.children), slots, note))

    def adopt(self):
        """Runs left by a previous runner: keep watching the live ones, settle the finished ones."""
        for r in self.conn.execute("SELECT r.*, t.owner FROM runs r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.ended_at IS NULL").fetchall():
            t = self.conn.execute("SELECT * FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
            timeout_s = (t["timeout_s"] if t else None) or parse_dur(cfg(self.conn, "timeout"))
            c = Child(r["id"], r["task_id"], f"{r['harness']}#{r['id']}", r["harness"], r["model"], r["bucket"], r["pid"],
                      r["pid_start"], None, timeout_s, None, r["out_path"], r["err_path"], r["started_at"])
            c.board = t["board"] if t else store.DEFAULT_BOARD
            c.endpoint = r["endpoint"]
            c.provider = r["provider"]
            if alive(r["pid"], r["pid_start"]):
                self.children[r["id"]] = c
                self.log(f"adopted run {r['id']} ({r['task_id']}, pid {r['pid']})")
            else:
                self.finalize(c, None)

    def run(self):
        self.acquire()
        self.log(f"runner up, pid {os.getpid()}, board {store.DB_PATH}")
        with tx(self.conn):
            store.cfg_set(self.conn, "stop", "", "runner")
            event(self.conn, None, "runner", "runner_start", {"pid": os.getpid()})
        self.adopt()
        while True:
            try:
                if self.tick():
                    break
            except Exception as e:  # never die on one bad tick
                self.log(f"tick error: {e!r}")
                time.sleep(5)
            time.sleep(float(cfg(self.conn, "poll_s") or 5))
        self.heartbeat("stopped", 0)
        with tx(self.conn):
            store.cfg_set(self.conn, "stop", "", "runner")
            event(self.conn, None, "runner", "runner_stop", {"pid": os.getpid()})
        self.log("runner stopped")

    def reload(self):
        """Re-exec this runner with the code on disk. Running harnesses live in their own sessions and are
        re-adopted from the db by the new process (same pid, so they are still our children)."""
        # Pre-flight: a broken edit on disk must not take the runner (and its agents) down.
        chk = subprocess.run([sys.executable, "-c", "import agentboard.cli, agentboard.runner, agentboard.harness, "
                              "agentboard.usage, agentboard.store, agentboard.automation"],
                             capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent))
        if chk.returncode:
            self.log(f"reload refused, new code does not import: {(chk.stderr or chk.stdout).strip()[-400:]}")
            with tx(self.conn):
                store.cfg_set(self.conn, "stop", "", "runner")
                event(self.conn, None, "runner", "runner_reload_refused", {"error": (chk.stderr or "")[-400:]})
            return
        self.log(f"reloading with {len(self.children)} run(s) in flight")
        with tx(self.conn):
            store.cfg_set(self.conn, "stop", "", "runner")
            event(self.conn, None, "runner", "runner_reload", {"pid": os.getpid(), "runs": len(self.children)})
        self.conn.close()
        try:
            self.lock_f.close()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, "-m", "agentboard", "runner", "run"])

    def tick(self):
        c = self.conn
        store.reap(c)
        self.poll_children()
        stop = cfg(c, "stop") or ""
        if stop == "reload":
            self.reload()
        if stop == "now":
            for ch in list(self.children.values()):
                self.kill(ch, "stopped")
            deadline = now() + 20
            while self.children and now() < deadline:
                time.sleep(1)
                self.poll_children()
            return True
        slots, windowed = effective_slots(c)
        if now() - self.last_harvest >= float(cfg(c, "harvest_every_s") or 60):
            self.last_harvest = now()
            usage.harvest(c)
        try:
            usage.refresh_plan(c)
        except Exception as e:
            self.log(f"plan refresh error: {e!r}")
        try:
            for name, tid, n in automation.run_watchers(c):
                self.log(f"watcher {name} fired on {n} event(s) -> {tid}")
        except Exception as e:
            self.log(f"watchers error: {e!r}")
        for name, res in automation.run_syncs(c).items():
            if res.get("error") or any(res.values()):
                self.log(f"sync {name}: {res}")
        paused, draining = bool(cfg(c, "paused")), bool(cfg(c, "draining"))
        if stop == "graceful":
            if not self.children:
                return True
            self.heartbeat("stopping", slots, f"waiting for {len(self.children)} run(s)")
            return False
        if draining and not self.children:
            with tx(c):
                store.cfg_set(c, "draining", False, "runner")
                store.cfg_set(c, "paused", True, "runner")
                event(c, None, "runner", "drained", None)
            paused, draining = True, False
        state = "paused" if paused else "draining" if draining else "running"
        note = None
        if not paused and not draining:
            note = self.fill(slots)
        self.heartbeat(state, slots, note if note else ("schedule window" if windowed else None))
        return False

    # -- picking work
    def pick(self, task, limits):
        primary, extra = H.candidates_for(self.conn, task)
        reasons = []
        for cand in primary + extra:
            a = H.ADAPTERS.get(cand.harness)
            if a is None or not a.available():
                reasons.append(f"{cand.label()}: not installed")
                continue
            if cand.endpoint:
                if cand.endpoint not in (cfg(self.conn, "endpoints") or {}):
                    reasons.append(f"{cand.label()}: no endpoint {cand.endpoint!r} (ab endpoints add)")
                    continue
                if type(a).endpoint_command is H.Adapter.endpoint_command:
                    reasons.append(f"{cand.label()}: {cand.harness} can't run on endpoints")
                    continue
            elif cand.harness == "opencode" and cfg(self.conn, "require_free.opencode") and not (
                    (cand.model or "").endswith("-free") or (cand.model or "").endswith(":free")):
                reasons.append(f"{cand.label()}: opencode is limited to free models")
                continue
            hl = limits.get(harness_scope(cand.harness, cand.endpoint))
            if hl:
                reasons.append(f"{cand.label()}: {hl['kind']} until {store.fmt_ts(hl['until'])}")
                continue
            b = H.bucket_for(self.conn, cand.harness, cand.model, cand.endpoint, cand.provider)
            bl = limits.get(f"bucket:{b}")
            if bl:
                reasons.append(f"{cand.label()}: {b} {bl['kind']} until {store.fmt_ts(bl['until'])}")
                continue
            cap = usage.cap_status(self.conn, b)
            if cap:
                reasons.append(f"{cand.label()}: {b} {cap}")
                continue
            return cand, b, None
        return None, None, "; ".join(reasons) or "tier has no candidates"

    def fill(self, slots):
        c = self.conn
        if len(self.children) >= slots:
            return None
        chans = cfg(c, "channels") or None
        busy_dirs = {ch.workdir for ch in self.children.values() if ch.workdir}
        limits = store.active_limits(c)
        caps = {r["name"]: r["max_slots"] for r in c.execute("SELECT name, max_slots FROM boards")}
        note = None
        for t in store.ready_tasks(c, channel=chans, runnable_only=True, skip_paused=True):
            if len(self.children) >= slots:
                break
            cap = caps.get(t["board"])
            if cap is not None and sum(1 for ch in self.children.values() if ch.board == t["board"]) >= cap:
                continue
            gib = free_gib()
            if gib < float(cfg(c, "min_free_gib") or 0):
                note = f"waiting for RAM: {gib:.1f} GiB free < min_free_gib"
                break
            workdir = t["workdir"] or str(store.HOME / "work" / t["id"])
            if not cfg(c, "share_workdir") and workdir in busy_dirs:
                continue
            cand, bucket, why = self.pick(t, limits)
            if cand is None:
                if self.waiting.get(t["id"]) != why:
                    self.waiting[t["id"]] = why
                    with tx(c):
                        event(c, t["id"], "runner", "waiting", {"why": why})
                    self.log(f"{t['id']} waiting: {why}")
                note = f"{t['id']} waiting: {why}"
                continue
            self.waiting.pop(t["id"], None)
            if self.spawn(t, cand, bucket, workdir):
                busy_dirs.add(workdir)
        return note

    def spawn(self, task, cand, bucket, workdir):
        c = self.conn
        a = H.ADAPTERS[cand.harness]
        with tx(c):
            cur = c.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if cur["status"] != "open" or task["id"] not in {r["id"] for r in store.ready_tasks(c, runnable_only=True, limit=100000)}:
                return False
            c.execute("INSERT INTO runs(task_id,harness,model,effort,tier,bucket,started_at,endpoint,provider) VALUES(?,?,?,?,?,?,?,?,?)",
                      (task["id"], cand.harness, cand.model, cand.effort, task["tier"], bucket, now(), cand.endpoint, cand.provider))
            run_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            owner = f"{cand.harness}#{run_id}"
            c.execute("UPDATE tasks SET status='claimed', owner=?, lease_until=?, run_id=?, updated_at=? WHERE id=?",
                      (owner, now() + RUNNER_LEASE, run_id, now(), task["id"]))
            event(c, task["id"], "runner", "started", {"run": run_id, "harness": cand.harness, "model": cand.model,
                                                       "effort": cand.effort, "bucket": bucket, "candidate": cand.label(),
                                                       **({"provider": cand.provider} if cand.provider else {})})
        Path(workdir).mkdir(parents=True, exist_ok=True)
        runs_dir = store.HOME / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        out_path, err_path = str(runs_dir / f"{run_id}.out"), str(runs_dir / f"{run_id}.err")
        brief = build_brief(c, cur, workdir, (cur["attempts"] or 0) + 1, owner)
        extra = (cfg(c, "extra_args") or {}).get(cand.harness, [])
        try:
            ep = H.get_endpoint(c, cand.endpoint) if cand.endpoint else None
            cmd, xenv = a.build(brief, cand, ep, extra)
        except Exception as e:  # bad endpoint, harness setup step failed: park this route, not the task
            with tx(c):
                c.execute("UPDATE runs SET ended_at=?, outcome='spawn_failed', detail=? WHERE id=?", (now(), str(e)[:1000], run_id))
                c.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL WHERE id=? AND owner=?", (task["id"], owner))
                store.set_limit(c, harness_scope(cand.harness, cand.endpoint), "setup", now() + 1800, f"setup failed: {e}")
            self.log(f"setup failed for {cand.label()}: {e}")
            return False
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update(xenv)
        env.update({"AB_TASK": task["id"], "AB_ACTOR": owner, "AB_RUN": str(run_id), "AB_HOME": str(store.HOME),
                    "AB_BOARD": cur["board"],
                    "PATH": ":".join(ab_path_dirs() + [env.get("PATH", "")])})
        try:
            with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
                proc = subprocess.Popen(cmd, cwd=workdir, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                                        start_new_session=True, env=env)
        except OSError as e:
            with tx(c):
                c.execute("UPDATE runs SET ended_at=?, outcome='spawn_failed', detail=? WHERE id=?", (now(), str(e), run_id))
                c.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL WHERE id=? AND owner=?", (task["id"], owner))
                store.set_limit(c, harness_scope(cand.harness, cand.endpoint), "missing", now() + 3600, f"spawn failed: {e}")
            self.log(f"spawn failed for {cand.label()}: {e}")
            return False
        start = pid_start(proc.pid)
        with tx(c):
            c.execute("UPDATE runs SET pid=?, pid_start=?, out_path=?, err_path=? WHERE id=?", (proc.pid, start, out_path, err_path, run_id))
        timeout_s = cur["timeout_s"] or parse_dur(cfg(c, "timeout"))
        self.children[run_id] = Child(run_id, task["id"], owner, cand.harness, cand.model, bucket, proc.pid, start, proc,
                                      timeout_s, workdir, out_path, err_path, now())
        self.children[run_id].board = cur["board"]
        self.children[run_id].endpoint = cand.endpoint
        self.children[run_id].provider = cand.provider
        self.log(f"run {run_id}: {task['id']} -> {cand.label()} [{bucket}] pid {proc.pid}")
        return True

    # -- watching
    def kill(self, ch, reason):
        if ch.kill_reason is None:
            ch.kill_reason, ch.kill_at = reason, now()
            try:
                os.killpg(ch.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        elif now() - ch.kill_at > 15:
            try:
                os.killpg(ch.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def take_kill_requests(self):
        """`ab kill RUN` leaves run ids in the `kill_runs` setting; stop those runs and drop the handled ids."""
        ids = cfg(self.conn, "kill_runs") or []
        if not ids:
            return set()
        mine = {int(i) for i in ids} & set(self.children)
        with tx(self.conn):
            rest = [i for i in (cfg(self.conn, "kill_runs") or []) if int(i) not in mine and int(i) in self.children]
            self.conn.execute("INSERT INTO control(key,value) VALUES('kill_runs',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (json.dumps(rest),))
        return mine

    def poll_children(self):
        c = self.conn
        killed = self.take_kill_requests()
        for rid in killed:
            ch = self.children[rid]
            self.log(f"run {rid} ({ch.task_id}): kill requested")
            self.kill(ch, "killed")
        for rid, ch in list(self.children.items()):
            if ch.proc is not None:
                rc = ch.proc.poll()
                finished = rc is not None
            else:
                rc, finished = None, not alive(ch.pid, ch.start)
                if finished:  # adopted after a reload: it is our child again, reap it
                    try:
                        wp, st = os.waitpid(ch.pid, os.WNOHANG)
                        rc = os.waitstatus_to_exitcode(st) if wp else None
                    except OSError:
                        pass
            if finished:
                del self.children[rid]
                self.finalize(ch, rc)
                continue
            c.execute("UPDATE tasks SET lease_until=? WHERE id=? AND owner=? AND status='claimed'",
                      (now() + RUNNER_LEASE, ch.task_id, ch.owner))
            if ch.kill_reason or (ch.timeout_s and now() - ch.started > ch.timeout_s):
                self.kill(ch, ch.kill_reason or "timeout")
            else:
                t = c.execute("SELECT status, owner FROM tasks WHERE id=?", (ch.task_id,)).fetchone()
                if t and t["status"] == "cancelled":
                    self.kill(ch, "cancelled")

    def finalize(self, ch, rc):
        c = self.conn
        a = H.ADAPTERS[ch.harness]

        def rd(p):
            try:
                return Path(p).read_text(errors="replace") if p else ""
            except OSError:
                return ""
        out, err = rd(ch.out_path), rd(ch.err_path)
        res = a.parse(0 if rc is None else rc, out, err)
        if ch.kill_reason:
            res.ok, res.error_kind, res.error = False, ch.kill_reason, f"killed by runner: {ch.kill_reason}"
        model = res.model or ch.model
        with tx(c):
            t = c.execute("SELECT * FROM tasks WHERE id=?", (ch.task_id,)).fetchone()
            mine = t is not None and t["status"] == "claimed" and t["owner"] == ch.owner
            if res.error_kind in ("auth", "limit", "unavailable"):
                kind = res.error_kind
                if kind == "auth" and not getattr(ch, "provider", None):
                    scope = harness_scope(ch.harness, getattr(ch, "endpoint", None))
                else:  # a provider-pinned run lost one provider's login, not the whole harness
                    scope = f"bucket:{ch.bucket}"
                until = res.reset_at or (now() + parse_dur(cfg(c, f"cooldown.{kind}") or "30m"))
                store.set_limit(c, scope, kind, until, res.error[:500])
                if mine:
                    c.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now(), ch.task_id))
                    event(c, ch.task_id, "runner", "rerouted", {"run": ch.run_id, "kind": kind, "scope": scope, "until": until})
                outcome = kind
            elif ch.kill_reason in ("stopped", "cancelled", "killed"):
                if mine:
                    c.execute("UPDATE tasks SET status='open', owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (now(), ch.task_id))
                    event(c, ch.task_id, "runner", "released", {"run": ch.run_id, "why": "run killed" if ch.kill_reason == "killed" else "runner stopped"})
                outcome = ch.kill_reason
            elif mine:
                if res.ok:
                    store.post_message(c, "runner", "Agent exited without `ab done`; its final answer is kept as the result.",
                                       thread=ch.task_id, check=False, _in_tx=True)
                    result = (res.text or "finished, no summary")[:8000]
                    c.execute("UPDATE tasks SET status='done', result=?, owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                              (result, now(), ch.task_id))
                    event(c, ch.task_id, "runner", "done", {"run": ch.run_id, "auto": True})
                    store._report_back(c, t, ch.owner, result)
                    outcome = "done_auto"
                else:
                    why = (res.error or "agent failed")[:1500]
                    store.post_message(c, "runner", f"Run {ch.run_id} on {ch.harness} failed ({res.error_kind}): {why}",
                                       thread=ch.task_id, check=False, _in_tx=True)
                    outcome = "timeout" if ch.kill_reason == "timeout" else "failed"
                    store._fail_attempt(c, t, "runner", why, outcome)
            else:
                outcome = f"agent_{t['status']}" if t else "task_gone"
            c.execute("UPDATE runs SET ended_at=?, exit_code=?, outcome=?, session_id=?, model=?, input=?, output=?, cache_read=?, "
                      "cache_write=?, reasoning=?, cost_usd=?, detail=? WHERE id=?",
                      (now(), rc, outcome, res.session_id, model, res.input, res.output, res.cache_read, res.cache_write,
                       res.reasoning, res.cost_usd, (res.error if not res.ok else res.text)[:2000], ch.run_id))
            usage.record_run_usage(c, ch.run_id, ch.harness, ch.bucket, model, ch.task_id, res)
            event(c, ch.task_id, "runner", "run_end", {"run": ch.run_id, "outcome": outcome, "rc": rc,
                                                       "out": res.output, "in": res.input, "cost": res.cost_usd})
        self.log(f"run {ch.run_id} ({ch.task_id}, {ch.harness}) -> {outcome}" + (f": {res.error[:160]}" if not res.ok else ""))


def start_detached():
    store.HOME.mkdir(parents=True, exist_ok=True)
    logf = open(store.HOME / "runner.log", "ab")
    env = {**os.environ, "AB_HOME": str(store.HOME)}
    env["PYTHONPATH"] = _pythonpath(env)
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return p.pid


SERVICE = """[Unit]
Description=agentboard runner
After=network-online.target

[Service]
Type=simple
Environment="AB_HOME={home}"
Environment="PATH={path}"
Environment="PYTHONPATH={pythonpath}"
ExecStart={py} -m agentboard runner run
Restart=on-failure
RestartSec=10
KillMode=process

[Install]
WantedBy=default.target
"""


def service_text():
    return SERVICE.format(home=store.HOME, path=":".join(ab_path_dirs() + [os.environ.get("PATH", "")]),
                          pythonpath=_pythonpath(os.environ), py=sys.executable)
