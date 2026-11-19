"""Usage: harvest every harness's local logs (board runs AND your own sessions share the same quota),
sum by bucket over windows, and enforce optional soft caps."""
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


def record_run_usage(conn, run_id, harness_name, bucket, model, task_id, res):
    """Usage of harnesses we can't harvest from disk (agy, gemini, codex, pi --no-session) comes from the run itself."""
    if H.ADAPTERS[harness_name].harvested and harness_name != "pi":
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


_PLAN = {"ts": 0.0, "data": None, "fetched": 0.0, "persisted": 0.0}
PLAN_TTL_S = 120        # re-read the provider's own numbers at most this often
PLAN_STALE_S = 1800     # a failed read falls back to the last good one for this long, then fails open


def claude_plan_pct():
    """Claude subscription utilisation as the provider reports it (what /usage shows):
    {"five_hour": pct, "seven_day": pct, "five_hour_resets": iso, ...} or None when unreadable.
    Uses the Claude Code OAuth login on this machine; the token is never printed or stored."""
    now = time.time()
    if _PLAN["data"] is not None and now - _PLAN["ts"] < PLAN_TTL_S:
        return _PLAN["data"]
    try:
        import urllib.request
        cred = json.loads((HOMEP / ".claude" / ".credentials.json").read_text())
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


def uses_plan(cap):
    return any(cap.get(k) is not None for k in ("max_5h_pct", "max_7d_pct", "pace_7d_lead_h"))


def persist_plan(conn):
    """Runner side: keep the last good provider reading in `control` so read-only readers (`ab snapshot`) can show it
    without calling the provider. Raw upsert, no config event (this changes every few minutes)."""
    if _PLAN["data"] is None or _PLAN["fetched"] <= _PLAN["persisted"]:
        return
    conn.execute("INSERT INTO control(key,value) VALUES('_plan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps({"data": _PLAN["data"], "ts": _PLAN["fetched"]}),))
    _PLAN["persisted"] = _PLAN["fetched"]


def refresh_plan(conn):
    """Runner tick: read the provider's numbers (cached 2 min in-process) when a cap needs them, and persist them."""
    if any(uses_plan(c) for c in (store.cfg(conn, "caps") or {}).values()):
        claude_plan_pct()
        persist_plan(conn)


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


_LIVE = object()   # cap_status(plan=_LIVE): read the provider now (runner); pass a dict/None to stay offline (snapshot)


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
    w = store.parse_dur(cap.get("window") or "5h")
    t = window_totals(conn, bucket, w)
    for k, field in (("max_output", "output"), ("max_cost", "cost_usd"), ("max_runs", "runs"), ("max_calls", "calls")):
        if cap.get(k) is not None and t[field] >= float(cap[k]):
            return f"soft cap {k}={cap[k]} reached in the last {cap.get('window', '5h')} ({field}={t[field]:.0f})"
    return None


def buckets_report(conn):
    """Per bucket: usage in 5h / 24h / 7d, active limit, cap state."""
    lim = store.active_limits(conn)
    caps = store.cfg(conn, "caps") or {}
    names = {r[0] for r in conn.execute("SELECT DISTINCT bucket FROM usage WHERE ts >= ?", (time.time() - 7 * 86400,))}
    names |= set(caps) | {s.split(":", 1)[1] for s in lim if s.startswith("bucket:")}
    out = []
    for b in sorted(n for n in names if n):
        row = {"bucket": b}
        for label, w in (("5h", 5 * 3600), ("24h", 86400), ("7d", 7 * 86400)):
            row[label] = window_totals(conn, b, w)
        l = lim.get(f"bucket:{b}")
        row["limit"] = {"kind": l["kind"], "until": l["until"], "reason": l["reason"]} if l else None
        row["cap"] = caps.get(b)
        row["cap_hit"] = cap_status(conn, b)
        out.append(row)
    return out
