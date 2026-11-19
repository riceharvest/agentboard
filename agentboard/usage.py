"""Usage: harvest every harness's local logs (board runs AND your own sessions share the same quota),
sum by bucket over windows, and enforce optional soft caps."""
import glob
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import store
from ._lazy import Lazy

H = Lazy("harness")

BACKFILL_S = 8 * 86400   # first harvest looks back 8 days (covers weekly windows)
HOMEP = Path.home()


def _cursor(conn, source, key):
    r = conn.execute("SELECT value FROM cursors WHERE source=? AND key=?", (source, key)).fetchone()
    return json.loads(r["value"]) if r else None


def _set_cursor(conn, source, key, value):
    conn.execute("INSERT INTO cursors(source,key,value) VALUES(?,?,?) ON CONFLICT(source,key) DO UPDATE SET value=excluded.value",
                 (source, key, json.dumps(value)))


def _put(conn, source, key, ts, harness, bucket, model, u, task_id=None, replace=False):
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    conn.execute(f"{verb} INTO usage(source,key,ts,harness,bucket,model,task_id,input,output,cache_read,cache_write,reasoning,cost_usd) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (source, key, ts, harness, bucket, model, task_id, int(u.get("input") or 0), int(u.get("output") or 0),
                  int(u.get("cache_read") or 0), int(u.get("cache_write") or 0), int(u.get("reasoning") or 0), u.get("cost_usd")))


def _iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- per-source harvesters
def harvest_jsonl(conn, source, pattern, handle_line, cutoff):
    """Incremental reader for append-only JSONL logs. Cursor = byte offset per file."""
    import glob
    n = 0
    for path in glob.glob(pattern, recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        cur = _cursor(conn, source, path) or {}
        off = cur.get("off", 0)
        if cur.get("ino") not in (None, st.st_ino) or st.st_size < off:
            off = 0
        if st.st_size == off:
            continue
        if not cur and st.st_mtime < cutoff:
            _set_cursor(conn, source, path, {"off": st.st_size, "ino": st.st_ino})  # old file: skip history
            continue
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            continue
        pos = off
        for raw in data[:end + 1].split(b"\n"):
            line_off = pos
            pos += len(raw) + 1
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            n += handle_line(path, line_off, obj) or 0
        _set_cursor(conn, source, path, {"off": off + end + 1, "ino": st.st_ino})
    return n


def harvest_claude(conn, cutoff):
    def line(path, off, o):
        m = o.get("message") if isinstance(o.get("message"), dict) else None
        if o.get("type") != "assistant" or not m or not m.get("usage"):
            return 0
        u = m["usage"]
        key = m.get("id") or f"{path}:{off}"   # streamed copies of one message share an id: count once
        model = m.get("model")
        if model == "<synthetic>":
            return 0
        bucket = "claude-sub" if (model or "").startswith("claude") else "local"  # claude pointed at an endpoint
        _put(conn, "claude", key, _iso(o.get("timestamp")) or time.time(), "claude", bucket, model, {
            "input": u.get("input_tokens"), "output": u.get("output_tokens"),
            "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
            "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens")})
        return 1
    return harvest_jsonl(conn, "claude", str(HOMEP / ".claude/projects/**/*.jsonl"), line, cutoff)


def harvest_pi(conn, cutoff):
    def line(path, off, o):
        m = o.get("message") if isinstance(o.get("message"), dict) else None
        if not m or m.get("role") != "assistant" or not m.get("usage"):
            return 0
        u = m["usage"]
        if not (u.get("input") or u.get("output")):
            return 0
        prov = m.get("provider") or ""
        bucket = "local" if prov in ("vllm-local", "llama-local", "local", "ollama") else f"pi-{prov or 'default'}"
        ts = (m.get("timestamp") or 0) / 1000 or _iso(o.get("timestamp")) or time.time()
        _put(conn, "pi", f"{path}:{off}", ts, "pi", bucket, f"{prov}/{m.get('model')}", {
            "input": u.get("input"), "output": u.get("output"), "cache_read": u.get("cacheRead"),
            "cache_write": u.get("cacheWrite"), "cost_usd": (u.get("cost") or {}).get("total")})
        return 1
    return harvest_jsonl(conn, "pi", str(HOMEP / ".pi/agent/sessions/**/*.jsonl"), line, cutoff)


def harvest_opencode(conn, cutoff):
    p = HOMEP / ".local/share/opencode/opencode.db"
    if not p.exists():
        return 0
    since = _cursor(conn, "opencode", "updated") or int(cutoff * 1000)
    try:
        oc = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        rows = oc.execute("SELECT id, time_created, time_updated, data FROM message WHERE time_updated > ? ORDER BY time_updated",
                          (since,)).fetchall()
        oc.close()
    except sqlite3.Error:
        return 0
    n, top = 0, since
    for mid, created, updated, data in rows:
        top = max(top, updated)
        try:
            d = json.loads(data)
        except ValueError:
            continue
        tk = d.get("tokens") or {}
        if d.get("role") != "assistant" or not (tk.get("input") or tk.get("output")):
            continue
        model = f"{d.get('providerID')}/{d.get('modelID')}"
        bucket = H.ADAPTERS["opencode"].bucket(model)
        c = tk.get("cache") or {}
        _put(conn, "opencode", mid, created / 1000, "opencode", bucket, model, {
            "input": tk.get("input"), "output": tk.get("output"), "reasoning": tk.get("reasoning"),
            "cache_read": c.get("read"), "cache_write": c.get("write"), "cost_usd": d.get("cost")}, replace=True)
        n += 1
    _set_cursor(conn, "opencode", "updated", top)
    return n


def harvest_hermes(conn, cutoff):
    """Hermes keeps running totals per session; re-read sessions active in the window and replace."""
    n = 0
    pinned = {r["session_id"]: r["bucket"] for r in conn.execute(
        "SELECT session_id, bucket FROM runs WHERE harness='hermes' AND provider IS NOT NULL AND session_id IS NOT NULL")}
    for prof, p in H.hermes_dbs():
        try:
            hc = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            hc.row_factory = sqlite3.Row
            cols = {r[1] for r in hc.execute("PRAGMA table_info(sessions)")}
            last = "COALESCE(ended_at, started_at)" if "ended_at" in cols else "started_at"
            rows = hc.execute(f"SELECT * FROM sessions WHERE {last} > ? OR ended_at IS NULL", (cutoff,)).fetchall() \
                if "ended_at" in cols else hc.execute("SELECT * FROM sessions WHERE started_at > ?", (cutoff,)).fetchall()
            hc.close()
        except sqlite3.Error:
            continue
        for r in rows:
            r = dict(r)
            if not ((r.get("input_tokens") or 0) or (r.get("output_tokens") or 0)):
                continue
            if (r.get("started_at") or 0) < cutoff - 86400:
                continue
            _put(conn, "hermes", f"{prof}:{r['id']}", r.get("started_at") or time.time(), "hermes",
                 pinned.get(r["id"]) or H.hermes_bucket(r.get("billing_provider")), r.get("model"), {
                     "input": r.get("input_tokens"), "output": r.get("output_tokens"),
                     "cache_read": r.get("cache_read_tokens"), "cache_write": r.get("cache_write_tokens"),
                     "reasoning": r.get("reasoning_tokens"),
                     "cost_usd": r.get("actual_cost_usd") or r.get("estimated_cost_usd")}, replace=True)
            n += 1
    return n


HARVESTERS = {"claude": harvest_claude, "opencode": harvest_opencode, "hermes": harvest_hermes, "pi": harvest_pi}


def harvest(conn, only=None):
    cutoff = time.time() - BACKFILL_S
    out = {}
    for name, fn in HARVESTERS.items():
        if only and name not in only:
            continue
        try:
            with store.tx(conn):
                out[name] = fn(conn, cutoff)
        except Exception as e:  # one broken source must not stop the rest
            out[name] = f"error: {e}"
    conn.execute("INSERT INTO control(key,value) VALUES('_last_harvest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(time.time()),))
    return out


def record_run_usage(conn, run_id, harness_name, bucket, model, task_id, res, force=False):
    """Usage of harnesses we can't harvest from disk (agy, gemini, codex, pi --no-session,
    or an isolated per-run Hermes home) comes from the run itself."""
    if not force and H.ADAPTERS[harness_name].harvested and harness_name != "pi":
        return
    if not any((res.input, res.output, res.cache_read, res.cache_write)):
        return  # failed before any model call: nothing was spent
    _put(conn, "run", str(run_id), time.time(), harness_name, bucket, model, res.usage(), task_id=task_id, replace=True)


# ---------------------------------------------------------------- reporting
FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


def summarize(conn, since_s=None, by="bucket"):
    col = {"bucket": "bucket", "harness": "harness", "model": "model", "source": "source"}[by]
    args, where = [], ""
    if since_s:
        where = "WHERE ts >= ?"
        args.append(time.time() - since_s)
    rows = conn.execute(f"SELECT {col} AS k, COUNT(*) AS calls, SUM(input) input, SUM(output) output, SUM(cache_read) cache_read, "
                        f"SUM(cache_write) cache_write, SUM(reasoning) reasoning, SUM(cost_usd) cost_usd "
                        f"FROM usage {where} GROUP BY {col} ORDER BY SUM(output) DESC", args).fetchall()
    return [dict(r) for r in rows]


def by_task(conn, since_s=None, limit=30):
    args, where = [], "WHERE r.ended_at IS NOT NULL"
    if since_s:
        where += " AND r.started_at >= ?"
        args.append(time.time() - since_s)
    rows = conn.execute(
        "SELECT r.task_id, t.title, COUNT(*) runs, GROUP_CONCAT(DISTINCT r.harness) harnesses, SUM(r.input) input, SUM(r.output) output, "
        "SUM(r.cache_read) cache_read, SUM(r.cache_write) cache_write, SUM(r.reasoning) reasoning, SUM(r.cost_usd) cost_usd, "
        "SUM(r.ended_at - r.started_at) seconds FROM runs r LEFT JOIN tasks t ON t.id=r.task_id "
        f"{where} GROUP BY r.task_id ORDER BY SUM(r.output) DESC LIMIT ?", args + [limit]).fetchall()
    return [dict(r) for r in rows]


def window_totals(conn, bucket, window_s):
    r = conn.execute("SELECT COUNT(*) calls, COALESCE(SUM(output),0) output, COALESCE(SUM(input+cache_write),0) fresh_in, "
                     "COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE bucket=? AND ts>=?",
                     (bucket, time.time() - window_s)).fetchone()
    runs = conn.execute("SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=?", (bucket, time.time() - window_s)).fetchone()[0]
    return dict(r) | {"runs": runs}


_PLAN = {"ts": 0.0, "data": None, "fetched": 0.0, "persisted": 0.0, "tried": 0.0, "persisted_data": None}
PLAN_TTL_S = 120        # re-read the provider's own numbers at most this often
PLAN_STALE_S = 1800     # a failed read falls back to the last good one for this long, then fails open


def claude_plan_pct():
    """Claude subscription utilisation as the provider reports it (what /usage shows):
    {"five_hour": pct, "seven_day": pct, "five_hour_resets": iso, ...} or None when unreadable.
    Uses the Claude Code OAuth login on this machine; the token is never printed or stored."""
    now = time.time()
    if _PLAN["data"] is not None and now - _PLAN["ts"] < PLAN_TTL_S:
        return _PLAN["data"]
    cred_path = Path(os.environ.get("HOME") or "~").expanduser() / ".claude" / ".credentials.json"
    if not cred_path.exists() or (_PLAN["data"] is None and now - _PLAN.get("tried", 0.0) < 30):
        return _PLAN["data"]
    _PLAN["tried"] = now
    try:
        import urllib.request
        cred = json.loads(cred_path.read_text())
        tok = cred["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "agentboard"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        _PLAN["data"] = {"five_hour": float(j["five_hour"]["utilization"]), "seven_day": float(j["seven_day"]["utilization"]),
                         "five_hour_resets": j["five_hour"].get("resets_at"), "seven_day_resets": j["seven_day"].get("resets_at")}
        _PLAN["ts"] = _PLAN["fetched"] = now
    except Exception:
        if _PLAN["data"] is not None and now - _PLAN["ts"] > PLAN_STALE_S:
            _PLAN["data"] = None
        elif _PLAN["data"] is not None:
            _PLAN["ts"] = now - PLAN_TTL_S + 30   # retry in 30 s, keep serving the last good value
    return _PLAN["data"]


_DEFAULT_CLAUDE_PLAN_PCT = claude_plan_pct


def uses_plan(cap):
    return any(cap.get(k) is not None for k in ("max_5h_pct", "max_7d_pct", "pace_7d_lead_h", "pace_lead_h"))


def persist_plan(conn, p=None):
    """Runner side: keep the last good provider reading in `control` so read-only readers (`ab snapshot`) can show it
    without calling the provider. Raw upsert, no config event (this changes every few minutes)."""
    data = p if p is not None else _PLAN["data"]
    if data is None:
        return
    t = _PLAN["fetched"] or time.time()
    if data == _PLAN.get("persisted_data") and t - _PLAN["persisted"] < PLAN_TTL_S:
        return
    conn.execute("INSERT INTO control(key,value) VALUES('_plan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps({"data": data, "ts": t}),))
    _PLAN["persisted"] = t
    _PLAN["persisted_data"] = dict(data)


def refresh_plan(conn):
    """Runner tick: read the provider's numbers (cached 2 min in-process) when a cap or pacer needs them, and persist them."""
    caps = store.cfg(conn, "caps") or {}
    if claude_plan_pct is not _DEFAULT_CLAUDE_PLAN_PCT or any(uses_plan(c) for c in caps.values()):
        p = claude_plan_pct()
        if p is not None:
            persist_plan(conn, p)
    if codex_rate_limits is not _DEFAULT_CODEX_RATE_LIMITS or any(c.get("pace_lead_h") is not None for c in caps.values()):
        cw = codex_rate_limits()
        if cw is not None:
            conn.execute("INSERT INTO control(key,value) VALUES('_codex',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps({"data": cw, "ts": time.time()}),))


def stored_plan(conn):
    """-> (plan dict or None, age in seconds) from the last runner reading. Never touches the network."""
    r = conn.execute("SELECT value FROM control WHERE key='_plan'").fetchone()
    if not r:
        return None, None
    j = json.loads(r["value"])
    return j.get("data"), max(0.0, time.time() - (j.get("ts") or 0))


def plan_status(cap):
    """Reason string if the provider-reported plan usage is at/over the cap's max_5h_pct / max_7d_pct."""
    if not uses_plan(cap):
        return None
    return plan_reason(cap, claude_plan_pct())


def plan_reason(cap, p):
    """plan_status() for an already-read plan dict `p` (no network)."""
    if not uses_plan(cap) or not p:
        return None
    for k, field, label in (("max_5h_pct", "five_hour", "5h"), ("max_7d_pct", "seven_day", "weekly")):
        if cap.get(k) is not None and p[field] >= float(cap[k]):
            return f"plan guard: {label} usage {p[field]:.0f}% >= {float(cap[k]):.0f}% (resets {p[field + '_resets']})"
    allowed = weekly_pace_pct(cap, p)
    if allowed is not None and p["seven_day"] >= allowed:
        return f"plan guard: weekly usage {p['seven_day']:.0f}% >= today's pace {allowed:.0f}% (resets {p['seven_day_resets']})"
    return None


def weekly_pace_pct(cap, p, now=None):
    """Weekly % allowed by now when the week is spread evenly so it is used up `pace_7d_lead_h` hours before the
    reset (a daily allowance; unused days carry over). None when not configured or the reset time is unknown."""
    if cap.get("pace_7d_lead_h") is None or not p.get("seven_day_resets"):
        return None
    try:
        reset = datetime.fromisoformat(p["seven_day_resets"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    span = 7 * 86400 - float(cap["pace_7d_lead_h"]) * 3600
    elapsed = (now if now is not None else time.time()) - (reset - 7 * 86400)
    return max(0.0, min(100.0, 100.0 * elapsed / span))


_CODEX = {"ts": 0.0, "data": None}


def codex_rate_limits():
    """Codex (ChatGPT login) quota as the CLI itself records it: the newest `rate_limits` event in ~/.codex/sessions.
    -> [{"name", "used", "window_s", "resets_at"}] (primary/secondary windows) or None. Cached 60 s; file read only."""
    now = time.time()
    if _CODEX["data"] is not None and now - _CODEX["ts"] < 60:
        return _CODEX["data"]
    out = None
    home = Path(os.environ.get("HOME") or "~").expanduser()
    files = sorted(glob.glob(str(home / ".codex" / "sessions" / "*" / "*" / "*" / "*.jsonl")), key=os.path.getmtime, reverse=True)
    for f in files[:5]:
        try:
            lines = Path(f).read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            rl = (j.get("payload") or {}).get("rate_limits") or j.get("rate_limits")
            if not isinstance(rl, dict):
                continue
            out = [{"name": k, "used": float(w["used_percent"]), "window_s": float(w["window_minutes"]) * 60,
                    "resets_at": float(w["resets_at"])}
                   for k in ("primary", "secondary") if isinstance(w := rl.get(k), dict) and w.get("resets_at")]
            break
        if out:
            break
    _CODEX.update(ts=now, data=out)
    return out


_DEFAULT_CODEX_RATE_LIMITS = codex_rate_limits


def stored_codex(conn):
    r = conn.execute("SELECT value FROM control WHERE key='_codex'").fetchone()
    if not r:
        return None, None
    j = json.loads(r["value"])
    return j.get("data"), max(0.0, time.time() - (j.get("ts") or 0))


def pace_reason(cap, windows, now=None):
    """Generic quota pacing: for every provider window, the share used may not run ahead of the share of the window
    that has passed (used up `pace_lead_h` hours before its reset; unused time carries over). Enabled by cap key
    `pace_lead_h`. Reason string or None."""
    if cap.get("pace_lead_h") is None or not windows:
        return None
    now = now if now is not None else time.time()
    for w in windows:
        span = max(1.0, w["window_s"] - float(cap["pace_lead_h"]) * 3600)
        allowed = max(0.0, min(100.0, 100.0 * (now - (w["resets_at"] - w["window_s"])) / span))
        if w["used"] >= allowed:
            return (f"pace guard: {w['name']} window ({w['window_s'] / 86400:.1f} d) used {w['used']:.0f}% >= "
                    f"pace {allowed:.1f}% (resets {datetime.fromtimestamp(w['resets_at']).isoformat(timespec='minutes')})")
    return None


_LIVE = object()   # cap_status(plan=_LIVE): read the provider now (runner); pass a dict/None to stay offline (snapshot)

# ---------------------------------------------------------------- launch pacer (pace / AIMD / cap)
PRIOR_RUN_COST = 25000.0   # ~25k effective tokens or $1.00 for a standard run
BASE_RUNS_PER_H = 18.0     # steady on-pace rate for standard runs (3 runs per 10m interval)


def pace_interval(conn):
    """-> (label, seconds). Scales with poll_s when poll_s is shortened for tests and pace_interval wasn't set explicitly."""
    row = conn.execute("SELECT value FROM control WHERE key='pace_interval'").fetchone()
    if row is not None:
        raw = json.loads(row["value"])
        s = max(0.5, float(store.parse_dur(raw) or 600.0))
        return (str(raw) if isinstance(raw, str) else store.fmt_dur(s)), s
    poll = float(store.cfg(conn, "poll_s") or 5)
    if poll < float(store.DEFAULTS["poll_s"]):
        s = max(1.0, poll * 3.0)
        return store.fmt_dur(s), s
    raw = store.DEFAULTS["pace_interval"]
    return str(raw), float(store.parse_dur(raw))


def recent_run_cost(conn, bucket, now=None):
    """Recent cost per run on `bucket` from the `usage` table, in effective-token units (PRIOR_RUN_COST = 1 standard run)."""
    since = (now or time.time()) - 7 * 86400
    rows = conn.execute(
        "SELECT SUM(output) o, SUM(input + cache_write) f, SUM(cost_usd) c "
        "FROM usage WHERE bucket=? AND ts>=? AND (task_id IS NOT NULL OR source='run') "
        "GROUP BY COALESCE(task_id, key) ORDER BY MAX(ts) DESC LIMIT 20",
        (bucket, since),
    ).fetchall()
    if not rows:
        tot = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(output),0) o, COALESCE(SUM(input+cache_write),0) f, COALESCE(SUM(cost_usd),0) c "
            "FROM usage WHERE bucket=? AND ts>=?",
            (bucket, since),
        ).fetchone()
        if not tot or not tot["n"]:
            return PRIOR_RUN_COST
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=? AND ended_at IS NOT NULL",
            (bucket, since),
        ).fetchone()[0]
        div = max(1, n_runs) if n_runs > 0 else tot["n"]
        cost = max(float(tot["c"] or 0) * PRIOR_RUN_COST, float(tot["o"] or 0) + 0.1 * float(tot["f"] or 0)) / div
        return cost if cost > 0 else PRIOR_RUN_COST
    costs = [max(float(r["c"] or 0) * PRIOR_RUN_COST, float(r["o"] or 0) + 0.1 * float(r["f"] or 0)) for r in rows]
    pos = [c for c in costs if c > 0]
    return (sum(pos) / len(pos)) if pos else PRIOR_RUN_COST


def _parse_reset_ts(val, default):
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    return _iso(str(val)) or default


def provider_windows(conn, bucket, cap=None, plan=_LIVE, now=None):
    """Provider-reported quota windows for `bucket`: [{"name", "used", "window_s", "resets_at", "max_pct", "lead_h", "hard_pace"}].
    Empty list when the provider reports nothing for this bucket."""
    now = now if now is not None else time.time()
    cap = cap if cap is not None else ((store.cfg(conn, "caps") or {}).get(bucket) or {})
    if bucket == "claude-sub":
        p = None
        if plan is _LIVE:
            if claude_plan_pct is not _DEFAULT_CLAUDE_PLAN_PCT or uses_plan(cap):
                p = claude_plan_pct()
            if p is None:
                raw, age = stored_plan(conn)
                if raw and (age is None or age <= PLAN_STALE_S):
                    p = raw
        else:
            p = plan
        if not p:
            return []
        out = []
        if p.get("five_hour") is not None:
            w_s = 5 * 3600.0
            out.append({
                "name": "5h",
                "used": float(p["five_hour"]),
                "window_s": w_s,
                "resets_at": _parse_reset_ts(p.get("five_hour_resets"), now + w_s / 2.0),
                "max_pct": float(cap["max_5h_pct"]) if cap.get("max_5h_pct") is not None else 100.0,
                "lead_h": float(cap["pace_lead_h"]) if cap.get("pace_lead_h") is not None else 0.0,
                "hard_pace": cap.get("pace_lead_h") is not None,
            })
        if p.get("seven_day") is not None:
            w_s = 7 * 86400.0
            lead_h = (float(cap["pace_7d_lead_h"]) if cap.get("pace_7d_lead_h") is not None
                      else float(cap["pace_lead_h"]) if cap.get("pace_lead_h") is not None else 0.0)
            out.append({
                "name": "7d",
                "used": float(p["seven_day"]),
                "window_s": w_s,
                "resets_at": _parse_reset_ts(p.get("seven_day_resets"), now + w_s / 2.0),
                "max_pct": float(cap["max_7d_pct"]) if cap.get("max_7d_pct") is not None else 100.0,
                "lead_h": lead_h,
                "hard_pace": cap.get("pace_7d_lead_h") is not None or cap.get("pace_lead_h") is not None,
            })
        return out
    if bucket == "openai-codex":
        wins = None
        if plan is _LIVE and (codex_rate_limits is not _DEFAULT_CODEX_RATE_LIMITS or cap.get("pace_lead_h") is not None):
            wins = codex_rate_limits()
        if wins is None:
            raw, age = stored_codex(conn)
            if raw and (age is None or age <= PLAN_STALE_S):
                wins = raw
        if not wins:
            return []
        lead_h = float(cap["pace_lead_h"]) if cap.get("pace_lead_h") is not None else 0.0
        return [{
            "name": w["name"],
            "used": float(w["used"]),
            "window_s": float(w["window_s"]),
            "resets_at": _parse_reset_ts(w.get("resets_at"), now + float(w["window_s"]) / 2.0),
            "max_pct": 100.0,
            "lead_h": lead_h,
            "hard_pace": cap.get("pace_lead_h") is not None,
        } for w in wins]
    return []


def pace_budget_for_windows(conn, bucket, windows, interval_s, now=None):
    """Derive allowed launches per interval from (remaining % until the pace line) / (time to the pace target)
    and the bucket's recent cost per run (from the usage table)."""
    now = now if now is not None else time.time()
    run_cost = recent_run_cost(conn, bucket, now)
    cost_ratio = max(0.05, run_cost / PRIOR_RUN_COST)
    evals = []
    for w in windows:
        used = float(w["used"])
        window_s = max(1.0, float(w["window_s"]))
        max_pct = float(w.get("max_pct") or 100.0)
        lead_s = float(w.get("lead_h") or 0.0) * 3600.0
        span = max(1.0, window_s - lead_s)
        resets_at = float(w.get("resets_at") or (now + window_s / 2.0))
        start = resets_at - window_s
        end_at = start + span
        if resets_at <= now:
            elapsed, rem_time = span, max(60.0, float(interval_s))
        else:
            elapsed = max(0.0, min(span, now - start))
            rem_time = max(60.0, end_at - now)
        pace_now = max_pct * (elapsed / span)
        if used >= max_pct or (w.get("hard_pace") and used >= pace_now):
            det = f"pace {w['name']}: {used:.0f}% >= {pace_now:.0f}% pace" if used < max_pct else f"pace {w['name']}: {used:.0f}% >= {max_pct:.0f}%"
            return 0, det
        eff_int_s = max(float(interval_s), 600.0) if window_s >= 3600.0 else float(interval_s)
        target_dt = min(rem_time, max(eff_int_s, span / 14.0))
        pace_target = min(max_pct, pace_now + max_pct * (target_dt / span))
        rem_pct = pace_target - used
        if rem_pct > 0:
            pct_per_s = rem_pct / target_dt
        else:
            pct_per_s = (max(0.0, max_pct - used) / rem_time) * (pace_now / max(used, 1.0))
        allowed_pct = pct_per_s * eff_int_s
        window_std_runs = max(1.0, (window_s / 3600.0) * BASE_RUNS_PER_H)
        pct_per_run = (100.0 / window_std_runs) * cost_ratio
        raw_runs = allowed_pct / max(1e-6, pct_per_run)
        weight = 0.7 if window_s >= 7 * 86400 else 0.3
        evals.append({"raw": raw_runs, "above": used > pace_now + 1e-6, "weight": weight,
                      "det": f"pace {w['name']}: {used:.0f}% vs {pace_now:.0f}% pace"})
    if not evals:
        return 1, "pace"
    if any(e["above"] for e in evals):
        chosen = min(evals, key=lambda e: e["raw"])
        return max(1, int(round(chosen["raw"]))), chosen["det"]
    tot_w = sum(e["weight"] for e in evals)
    combined = sum(e["raw"] * e["weight"] for e in evals) / (tot_w or 1.0)
    chosen = max(evals, key=lambda e: e["weight"])
    return max(1, int(round(combined))), chosen["det"]


def _load_pacer(conn):
    r = conn.execute("SELECT value FROM control WHERE key='_pacer'").fetchone()
    return json.loads(r["value"]) if r else {}


def _save_pacer(conn, state):
    conn.execute("INSERT INTO control(key,value) VALUES('_pacer',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(state),))


def pacer_tick(conn, now=None):
    """Runner tick: advance completed AIMD intervals (+1 budget per clean interval with activity)."""
    now = now if now is not None else time.time()
    _, interval_s = pace_interval(conn)
    state = _load_pacer(conn)
    if not state:
        return
    lim = store.active_limits(conn)
    caps = store.cfg(conn, "caps") or {}
    changed = False
    for bucket, st in list(state.items()):
        ws = float(st.get("window_start") or 0.0)
        if ws <= 0 or now - ws < interval_s:
            continue
        runs_in_win = conn.execute("SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=?", (bucket, ws)).fetchone()[0]
        had_limit = float(st.get("halved_at") or 0.0) >= ws or (lim.get(f"bucket:{bucket}") or {}).get("kind") == "limit"
        if runs_in_win > 0 and not had_limit:
            b = max(1, int(st.get("budget") or 1)) + 1
            max_r = (caps.get(bucket) or {}).get("max_runs")
            if max_r is not None:
                b = min(b, max(1, int(float(max_r))))
            st["budget"] = b
        st["window_start"] = now
        changed = True
    if changed:
        with store.tx(conn):
            _save_pacer(conn, state)


def pacer_on_spawn(conn, bucket, now=None):
    """Called inside Runner.spawn's transaction when a run starts on `bucket`."""
    now = now if now is not None else time.time()
    state = _load_pacer(conn)
    st = state.setdefault(bucket, {"budget": 1, "window_start": now})
    if not st.get("window_start"):
        st["window_start"] = now
    _save_pacer(conn, state)


def pacer_on_limit(conn, bucket, now=None):
    """Called inside Runner.finalize's transaction when a run on `bucket` hits a limit/quota error: halve its budget."""
    now = now if now is not None else time.time()
    state = _load_pacer(conn)
    st = state.setdefault(bucket, {"budget": 1, "window_start": now})
    old = max(1, int(st.get("budget") or 1))
    st["budget"] = max(1, old // 2)
    st["window_start"] = now
    st["halved_at"] = now
    _save_pacer(conn, state)
    return st["budget"]


def launch_budget(conn, bucket, plan=_LIVE, now=None, pacer_state=None):
    """Current launch budget for `bucket` over `pace_interval` and why ('pace' | 'aimd' | 'cap'). Never writes to the db."""
    now = now if now is not None else time.time()
    interval_str, interval_s = pace_interval(conn)
    cap = (store.cfg(conn, "caps") or {}).get(bucket) or {}
    pstate = pacer_state if pacer_state is not None else _load_pacer(conn)
    st = pstate.get(bucket) or {}
    windows = provider_windows(conn, bucket, cap, plan=plan, now=now)
    if windows:
        budget, detail = pace_budget_for_windows(conn, bucket, windows, interval_s, now=now)
        why = "pace"
    else:
        b = max(1, int(st.get("budget") or 1))
        ws = float(st.get("window_start") or 0.0)
        if ws > 0 and now - ws >= interval_s:
            runs_prev = conn.execute("SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=? AND started_at<?",
                                     (bucket, ws, ws + interval_s)).fetchone()[0]
            had_limit = float(st.get("halved_at") or 0.0) >= ws
            if runs_prev > 0 and not had_limit:
                b += 1
        budget = b
        why = "aimd"
        detail = f"AIMD {budget}/{interval_str}"
    if cap.get("max_runs") is not None:
        max_r = max(0, int(float(cap["max_runs"])))
        cap_win_s = store.parse_dur(cap.get("window") or "5h") or 18000.0
        runs_cap = conn.execute("SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=?", (bucket, now - cap_win_s)).fetchone()[0]
        if budget >= max_r or runs_cap >= max_r:
            budget = min(budget, max_r)
            why = "cap"
            detail = f"cap max_runs={max_r}/{cap.get('window', '5h')} ({runs_cap}/{max_r})"
    else:
        for k, field in (("max_output", "output"), ("max_cost", "cost_usd"), ("max_calls", "calls")):
            if cap.get(k) is not None:
                cap_win_s = store.parse_dur(cap.get("window") or "5h") or 18000.0
                t = window_totals(conn, bucket, cap_win_s)
                if t[field] >= float(cap[k]):
                    budget = 0
                    why = "cap"
                    detail = f"cap {k}={cap[k]} reached"
                    break
    ws = float(st.get("window_start") or 0.0)
    if ws <= 0:
        since_ts = now - interval_s
    elif now - ws < interval_s:
        since_ts = ws
    else:
        since_ts = ws + int((now - ws) // interval_s) * interval_s
    used_in_interval = conn.execute("SELECT COUNT(*) FROM runs WHERE bucket=? AND started_at>=?", (bucket, since_ts)).fetchone()[0]
    return {
        "budget": budget,
        "why": why,
        "interval": interval_str,
        "interval_s": interval_s,
        "used_in_interval": used_in_interval,
        "remaining": max(0, budget - used_in_interval),
        "detail": detail,
    }


def pacer_status(conn, bucket, plan=_LIVE, now=None):
    """None if `bucket` has remaining launch budget in the current interval, else a reason string."""
    if store.cfg(conn, "pacer") is False:
        return None
    info = launch_budget(conn, bucket, plan=plan, now=now)
    if info["used_in_interval"] >= info["budget"]:
        return f"pacer: {info['used_in_interval']}/{info['budget']} runs in {info['interval']} ({info['why']})"
    return None


def all_window_totals(conn, now=None):
    """{bucket: {"5h"|"24h"|"7d": window_totals-shaped dict}} in two queries (for UIs)."""
    now = now or time.time()
    wins = (("5h", 5 * 3600), ("24h", 86400), ("7d", 7 * 86400))
    cols = ", ".join(f"SUM(ts>={now - w}) c{i}, COALESCE(SUM(CASE WHEN ts>={now - w} THEN output END),0) o{i}, "
                     f"COALESCE(SUM(CASE WHEN ts>={now - w} THEN input+cache_write END),0) f{i}, "
                     f"COALESCE(SUM(CASE WHEN ts>={now - w} THEN cost_usd END),0) d{i}" for i, (_, w) in enumerate(wins))
    out = {}
    for r in conn.execute(f"SELECT bucket, {cols} FROM usage WHERE ts>=? AND bucket IS NOT NULL GROUP BY bucket", (now - 7 * 86400,)):
        out[r["bucket"]] = {lab: {"calls": r[f"c{i}"] or 0, "output": r[f"o{i}"], "fresh_in": r[f"f{i}"], "cost_usd": r[f"d{i}"], "runs": 0}
                            for i, (lab, _) in enumerate(wins)}
    runs_cols = ", ".join(f"SUM(started_at>={now - w}) r{i}" for i, (_, w) in enumerate(wins))
    for r in conn.execute(f"SELECT bucket, {runs_cols} FROM runs WHERE started_at>=? AND bucket IS NOT NULL GROUP BY bucket", (now - 7 * 86400,)):
        b = out.setdefault(r["bucket"], {lab: {"calls": 0, "output": 0, "fresh_in": 0, "cost_usd": 0, "runs": 0} for lab, _ in wins})
        for i, (lab, _) in enumerate(wins):
            b[lab]["runs"] = r[f"r{i}"] or 0
    return out


def history(conn, now=None, hours=168):
    """-> (hourly, split) for UIs from ONE scan of the usage table.
    hourly: {bucket: [output tokens per hour, oldest first, `hours` entries]} (sparkline data);
    split: [{harness, model, bucket, calls, output, fresh_in, cost_usd}] over the same window, biggest output first, at most 40."""
    now = now or time.time()
    first = int(now // 3600) - hours + 1
    hourly, split = {}, {}
    for r in conn.execute("SELECT bucket, harness, model, CAST(ts/3600 AS INTEGER) h, COUNT(*) c, SUM(output) o, SUM(input+cache_write) f, SUM(cost_usd) d "
                          "FROM usage WHERE ts>=? GROUP BY bucket, harness, model, h", (first * 3600,)):
        i = r["h"] - first
        if not 0 <= i < hours:
            continue
        if r["bucket"] is not None:
            hourly.setdefault(r["bucket"], [0] * hours)[i] += r["o"] or 0
        s = split.setdefault((r["harness"], r["model"], r["bucket"]), {"calls": 0, "output": 0, "fresh_in": 0, "cost_usd": 0.0})
        s["calls"] += r["c"]
        s["output"] += r["o"] or 0
        s["fresh_in"] += r["f"] or 0
        s["cost_usd"] += r["d"] or 0
    rows = [{"harness": k[0], "model": k[1], "bucket": k[2], **v} for k, v in split.items()]
    rows.sort(key=lambda x: -x["output"])
    return hourly, rows[:40]


def cap_status(conn, bucket, plan=_LIVE):
    """None if the bucket is under its soft cap (or has none), else a reason string."""
    cap = (store.cfg(conn, "caps") or {}).get(bucket)
    if not cap:
        return None
    r = plan_status(cap) if plan is _LIVE else plan_reason(cap, plan)
    if r:
        return r
    if bucket == "openai-codex" and cap.get("pace_lead_h") is not None:
        cw = codex_rate_limits() if plan is _LIVE else stored_codex(conn)[0]
        r = pace_reason(cap, cw)
        if r:
            return r
    w = store.parse_dur(cap.get("window") or "5h")
    t = window_totals(conn, bucket, w)
    for k, field in (("max_output", "output"), ("max_cost", "cost_usd"), ("max_runs", "runs"), ("max_calls", "calls")):
        if cap.get(k) is not None and t[field] >= float(cap[k]):
            return f"soft cap {k}={cap[k]} reached in the last {cap.get('window', '5h')} ({field}={t[field]:.0f})"
    return None


def buckets_report(conn):
    """Per bucket: usage in 5h / 24h / 7d, active limit, cap state, and launch budget."""
    lim = store.active_limits(conn)
    caps = store.cfg(conn, "caps") or {}
    pstate = _load_pacer(conn)
    since = time.time() - 7 * 86400
    names = {r[0] for r in conn.execute("SELECT DISTINCT bucket FROM usage WHERE ts >= ?", (since,))}
    names |= {r[0] for r in conn.execute("SELECT DISTINCT bucket FROM runs WHERE started_at >= ?", (since,))}
    names |= set(caps) | set(pstate) | {s.split(":", 1)[1] for s in lim if s.startswith("bucket:")}
    if claude_plan_pct is not _DEFAULT_CLAUDE_PLAN_PCT or stored_plan(conn)[0]:
        names.add("claude-sub")
    out = []
    for b in sorted(n for n in names if n):
        row = {"bucket": b}
        for label, w in (("5h", 5 * 3600), ("24h", 86400), ("7d", 7 * 86400)):
            row[label] = window_totals(conn, b, w)
        l = lim.get(f"bucket:{b}")
        row["limit"] = {"kind": l["kind"], "until": l["until"], "reason": l["reason"]} if l else None
        row["cap"] = caps.get(b)
        row["cap_hit"] = cap_status(conn, b)
        pinfo = launch_budget(conn, b, pacer_state=pstate)
        row["launch_budget"] = pinfo["budget"]
        row["budget_why"] = pinfo["why"]
        row["pacer"] = pinfo
        out.append(row)
    return out
