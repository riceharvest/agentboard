"""Harness adapters: how to launch each agent CLI headless, read its result and usage, and classify failures."""
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import store

# ---------------------------------------------------------------- failure classification
# Order matters: auth beats limit beats unavailable. These run on harness error text only,
# never on a successful result, so a task *about* rate limiting doesn't trip them.
AUTH_RE = re.compile(
    r"api[ _-]?key (?:has )?expired|invalid[ _-]api[ _-]key|authentication_error|unauthori[sz]ed|\b401\b|"
    r"not (?:logged|signed) in|please (?:log ?in|login|sign in|run .{0,30}login)|GOOGLE_CLOUD_PROJECT|"
    r"oauth token (?:has )?expired|invalid_grant|credentials? (?:missing|not found|expired)|api_key_expired", re.I)
LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota|RESOURCE_EXHAUSTED|\b429\b|limit reached|hit your (?:usage )?limit|"
    r"too many requests|insufficient_quota|credits? (?:exhausted|depleted|balance)|out of credits|"
    r"weekly limit|5[- ]hour limit|session limit", re.I)
UNAVAIL_RE = re.compile(
    r"\b50[234]\b|\b529\b|overloaded|service unavailable|ECONNREFUSED|connection refused|in use by another job|"
    r"server_error|bad gateway|gateway timeout|ETIMEDOUT|ENOTFOUND|network error|socket hang up", re.I)


def classify(text):
    if not text:
        return None
    if AUTH_RE.search(text):
        return "auth"
    if LIMIT_RE.search(text):
        return "limit"
    if UNAVAIL_RE.search(text):
        return "unavailable"
    return None


def parse_reset(text, base=None):
    """Best effort: find when a limit resets. Returns epoch or None."""
    if not text:
        return None
    base = base or time.time()
    m = re.search(r"\|(\d{10})\b", text)  # "Claude AI usage limit reached|1700000000"
    if m:
        return float(m[1])
    m = re.search(r"reset\w*\W{1,4}(?:at\W+)?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", text, re.I)
    if m:
        try:
            return datetime.fromisoformat(m[1].replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    m = re.search(r"(?:try again|retry|resets?|available again)\s+(?:in|after)\s+(?:about\s+)?((?:\d+\s*(?:d|h|m|s)\w*\s*)+)", text, re.I)
    if m:
        try:
            return base + store.parse_dur(re.sub(r"(\d)\s*([a-z])\w*", r"\1\2", m[1].strip(), flags=re.I))
        except ValueError:
            pass
    m = re.search(r"retry[-_ ]after\W{1,4}(\d+)", text, re.I)
    if m:
        return base + int(m[1])
    m = re.search(r"resets?\s+(?:at\s+)?(?:(mon|tue|wed|thu|fri|sat|sun)\w*\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if m and (m[3] or m[4]):
        h, mi = int(m[2]), int(m[3] or 0)
        if m[4]:
            h = h % 12 + (12 if m[4].lower() == "pm" else 0)
        b = datetime.fromtimestamp(base)
        t = b.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if m[1]:
            wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m[1][:3].lower())
            t += timedelta(days=(wd - t.weekday()) % 7)
        if t.timestamp() <= base:
            t += timedelta(days=7 if m[1] else 1)
        return t.timestamp()
    return None


# ---------------------------------------------------------------- results
@dataclass
class Result:
    ok: bool = False
    text: str = ""
    session_id: str = None
    model: str = None
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost_usd: float = None
    error_kind: str = None     # auth | limit | unavailable | failed | None
    error: str = ""
    reset_at: float = None

    def usage(self):
        return {k: getattr(self, k) for k in ("input", "output", "cache_read", "cache_write", "reasoning", "cost_usd")}


def _json_objects(text):
    """Yield JSON objects from a stream that may hold one document or JSON lines mixed with noise."""
    text = text or ""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            yield json.loads(s)
            return
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _tail(s, n=1500):
    s = (s or "").strip()
    return s[-n:]


def _matched_lines(blob, kind):
    """The line(s) that triggered the classification, not whatever happened to be last on stderr."""
    rx = {"auth": AUTH_RE, "limit": LIMIT_RE, "unavailable": UNAVAIL_RE}.get(kind)
    if not rx:
        return None
    lines = blob.splitlines()
    for i, line in enumerate(lines):
        if rx.search(line):
            return " ".join(l.strip() for l in lines[i:i + 2] if l.strip())[:600]
    return None


def _finish(r, rc, out, err):
    """Shared post-processing: classify failures, find reset times."""
    if not r.ok:
        blob = "\n".join(x for x in (r.error, r.text if not r.ok else "", _tail(err, 4000)) if x)
        if not r.error_kind:
            r.error_kind = classify(blob) or "failed"
        if r.error_kind in ("auth", "limit", "unavailable"):
            r.error = _matched_lines(r.error or "", r.error_kind) or _matched_lines(blob, r.error_kind) or r.error
        if not r.error:
            r.error = _tail(blob, 1500) or f"exit code {rc} with no output"
        if r.error_kind == "limit":
            r.reset_at = parse_reset(blob)
    return r


# ---------------------------------------------------------------- adapters
class Adapter:
    name = "?"
    bin = None
    harvested = False      # True when the usage harvester already counts this harness's sessions
    default_bucket = None

    def available(self):
        return bool(self.bin and shutil.which(self.bin))

    def command(self, brief, model=None, effort=None, extra=()):
        raise NotImplementedError

    def parse(self, rc, out, err):
        raise NotImplementedError

    def bucket(self, model):
        return self.default_bucket or self.name

    def list_models(self):
        return []


class Claude(Adapter):
    name, bin, harvested = "claude", "claude", True

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["claude", "-p", brief, "--output-format", "json", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd + list(extra)

    def parse(self, rc, out, err):
        r = Result()
        doc = None
        for o in _json_objects(out):
            if o.get("type") == "result" or "total_cost_usd" in o or "session_id" in o:
                doc = o
        if doc is None:
            r.error = _tail(out) or _tail(err)
            return _finish(r, rc, out, err)
        u = doc.get("usage") or {}
        r.input = int(u.get("input_tokens") or 0)
        r.output = int(u.get("output_tokens") or 0)
        r.cache_read = int(u.get("cache_read_input_tokens") or 0)
        r.cache_write = int(u.get("cache_creation_input_tokens") or 0)
        r.reasoning = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        r.cost_usd = doc.get("total_cost_usd")
        r.session_id = doc.get("session_id")
        mu = doc.get("modelUsage") or {}
        if mu:
            r.model = max(mu, key=lambda k: (mu[k] or {}).get("outputTokens") or 0)
        r.text = doc.get("result") or ""
        r.ok = rc == 0 and not doc.get("is_error") and doc.get("subtype", "success") == "success"
        if not r.ok:
            r.error = r.text or doc.get("subtype") or ""
        return _finish(r, rc, out, err)

    def bucket(self, model):
        return "claude-sub"

    def list_models(self):
        # Aliases always point at the newest model of each family, so tiers built on them never go stale.
        return [("opus", "newest Opus"), ("sonnet", "newest Sonnet"), ("haiku", "newest Haiku"),
                ("opusplan", "Opus plans, Sonnet executes")]


class Agy(Adapter):
    name, bin = "agy", "agy"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["agy", "-p", brief, "--output-format", "json", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        return cmd + list(extra)

    def parse(self, rc, out, err):
        r = Result()
        doc = None
        for o in _json_objects(out):
            if "status" in o or "response" in o:
                doc = o
        if doc is None:
            r.error = _tail(out) or _tail(err)
            return _finish(r, rc, out, err)
        u = doc.get("usage") or {}
        r.input = int(u.get("input_tokens") or 0)
        r.output = int(u.get("output_tokens") or 0)
        r.reasoning = int(u.get("thinking_tokens") or 0)
        r.cache_read = int(u.get("cache_read_tokens") or 0)
        r.session_id = doc.get("conversation_id")
        r.text = doc.get("response") or ""
        r.ok = rc == 0 and str(doc.get("status", "")).upper() == "SUCCESS"
        if not r.ok:
            r.error = json.dumps(doc.get("error")) if doc.get("error") else (r.text or str(doc.get("status")))
        return _finish(r, rc, out, err)

    def bucket(self, model):
        m = (model or self.configured_model() or "").lower()
        if any(k in m for k in ("claude", "gpt", "opus", "sonnet")):
            return "agy-claude-gpt"
        if "argon" in m:
            return "agy-argon"
        return "agy-gemini"

    def configured_model(self):
        try:
            return json.loads((Path.home() / ".gemini/antigravity-cli/settings.json").read_text()).get("model")
        except (OSError, ValueError):
            return None

    def list_models(self):
        p = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        out = []
        for line in p.stdout.splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                out.append((label.strip(), mid.strip()))  # --model takes the display name
        return out


class OpenCode(Adapter):
    name, bin, harvested = "opencode", "opencode", True

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["opencode", "run", "--format", "json"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--variant", effort]
        return cmd + list(extra) + [brief]

    def parse(self, rc, out, err):
        r = Result()
        texts, errors, saw = [], [], False
        for o in _json_objects(out):
            saw = True
            typ = o.get("type")
            part = o.get("part") or {}
            r.session_id = r.session_id or o.get("sessionID") or part.get("sessionID")
            if typ == "error" or o.get("error"):
                e = o.get("error") or {}
                errors.append(((e.get("data") or {}).get("message") or e.get("name") or json.dumps(e))[:800])
            if typ in ("text",) and part.get("text"):
                texts.append(part["text"])
            if typ in ("step_finish", "step-finish") or part.get("type") == "step-finish":
                tk = part.get("tokens") or {}
                r.input += int(tk.get("input") or 0)
                r.output += int(tk.get("output") or 0)
                r.reasoning += int(tk.get("reasoning") or 0)
                c = tk.get("cache") or {}
                r.cache_read += int(c.get("read") or 0)
                r.cache_write += int(c.get("write") or 0)
                if part.get("cost") is not None:
                    r.cost_usd = (r.cost_usd or 0) + float(part["cost"])
        r.text = "\n".join(texts).strip()
        r.ok = rc == 0 and saw and not errors
        if errors:
            r.error = " | ".join(errors)
        elif not saw:
            r.error = _tail(out) or _tail(err)
        return _finish(r, rc, out, err)

    def bucket(self, model):
        m = model or ""
        if m.endswith("-free") or m.endswith(":free"):
            return "opencode-free"
        return "opencode-" + (m.split("/")[0] if "/" in m else "default")

    def list_models(self):
        p = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return [(l.strip(), "") for l in p.stdout.splitlines() if "/" in l]


class Gemini(Adapter):
    name, bin, default_bucket = "gemini", "gemini", "gemini-cli"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["gemini", "-o", "json", "--approval-mode", "yolo"]
        if model:
            cmd += ["-m", model]
        return cmd + list(extra) + [brief]

    def parse(self, rc, out, err):
        r = Result()
        doc = None
        for o in _json_objects(out):
            doc = o
        if doc is None:
            r.error = _tail(err) or _tail(out)
            return _finish(r, rc, out, err)
        models = ((doc.get("stats") or {}).get("models") or {})
        for name, m in models.items():
            tk = (m or {}).get("tokens") or {}
            r.input += int(tk.get("prompt") or tk.get("input") or 0)
            r.output += int(tk.get("candidates") or tk.get("output") or 0)
            r.cache_read += int(tk.get("cached") or 0)
            r.reasoning += int(tk.get("thoughts") or 0)
            r.model = name
        r.text = doc.get("response") or ""
        if doc.get("error"):
            e = doc["error"]
            r.error = e.get("message") if isinstance(e, dict) else str(e)
        r.ok = rc == 0 and not doc.get("error")
        return _finish(r, rc, out, err)


class Pi(Adapter):
    name, bin = "pi", "pi"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["pi", "-p", "--mode", "json", "--no-session"]
        if model:
            cmd += ["--model", model + (f":{effort}" if effort else "")]
        return cmd + list(extra) + [brief]

    def parse(self, rc, out, err):
        r = Result()
        last_err, final_text, saw = None, "", False
        for o in _json_objects(out):
            saw = True
            if o.get("type") == "message_end":
                m = o.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                u = m.get("usage") or {}
                r.input += int(u.get("input") or 0)
                r.output += int(u.get("output") or 0)
                r.cache_read += int(u.get("cacheRead") or 0)
                r.cache_write += int(u.get("cacheWrite") or 0)
                cost = (u.get("cost") or {}).get("total")
                if cost:
                    r.cost_usd = (r.cost_usd or 0) + float(cost)
                r.model = f"{m.get('provider')}/{m.get('model')}" if m.get("provider") else m.get("model")
                if m.get("stopReason") == "error":
                    last_err = m.get("errorMessage") or "error"
                else:
                    last_err = None
                    txt = "".join(c.get("text", "") for c in (m.get("content") or []) if isinstance(c, dict) and c.get("type") == "text")
                    if txt.strip():
                        final_text = txt
            if o.get("type") == "auto_retry_end" and not o.get("success"):
                last_err = o.get("finalError") or last_err
        r.text = final_text
        r.ok = rc == 0 and saw and not last_err
        if last_err:
            r.error = last_err
        elif not saw:
            r.error = _tail(out) or _tail(err)
        return _finish(r, rc, out, err)

    def bucket(self, model):
        prov = (model or "").split("/")[0] if model and "/" in model else self.default_provider()
        return "local" if prov in ("vllm-local", "llama-local", "local", "ollama") else f"pi-{prov or 'default'}"

    def default_provider(self):
        try:
            return json.loads((Path.home() / ".pi/agent/settings.json").read_text()).get("defaultProvider")
        except (OSError, ValueError):
            return None

    def list_models(self):
        p = subprocess.run(["pi", "--list-models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        out = []
        for line in p.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out.append((f"{parts[0]}/{parts[1]}", ""))
        return out


class Hermes(Adapter):
    name, bin, harvested = "hermes", "hermes", True

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["hermes", "chat", "-q", brief, "--oneshot", "-Q", "--pass-session-id", "--yolo"]
        if model:
            cmd += ["-m", model]
        return cmd + list(extra)

    def parse(self, rc, out, err):
        r = Result()
        m = re.search(r"session_id:\s*(\S+)", (err or "") + "\n" + (out or ""))
        r.session_id = m[1] if m else None
        r.text = re.sub(r"(?m)^session_id:.*$", "", out or "").strip()
        if r.session_id:
            row = hermes_session(r.session_id)
            if row:
                r.input, r.output = row["input_tokens"] or 0, row["output_tokens"] or 0
                r.cache_read, r.cache_write = row["cache_read_tokens"] or 0, row["cache_write_tokens"] or 0
                r.reasoning = row["reasoning_tokens"] or 0
                r.model = row["model"]
        r.ok = rc == 0 and bool(r.text)
        if not r.ok:
            r.error = _tail(err) or r.text or f"exit {rc}"
        return _finish(r, rc, out, err)

    def bucket(self, model):
        prov = hermes_default_provider() or ""
        return hermes_bucket(prov)


class Codex(Adapter):
    name, bin, default_bucket = "codex", "codex", "openai-codex"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        return cmd + list(extra) + [brief]

    def parse(self, rc, out, err):
        r = Result()
        texts, errors, saw = [], [], False
        for o in _json_objects(out):
            saw = True
            typ = o.get("type", "")
            if typ == "thread.started":
                r.session_id = o.get("thread_id")
            if typ in ("error", "turn.failed"):
                errors.append(str((o.get("error") or {}).get("message") or o.get("message") or o)[:800])
            item = o.get("item") or {}
            if typ == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
                texts.append(item.get("text") or "")
            if typ == "turn.completed":
                u = o.get("usage") or {}
                r.input += int(u.get("input_tokens") or 0)
                r.cache_read += int(u.get("cached_input_tokens") or 0)
                r.output += int(u.get("output_tokens") or 0)
                r.reasoning += int(u.get("reasoning_output_tokens") or 0)
        r.text = "\n".join(texts).strip()
        r.ok = rc == 0 and saw and not errors
        if errors:
            r.error = " | ".join(errors)
        elif not saw:
            r.error = _tail(err) or _tail(out)
        return _finish(r, rc, out, err)


ADAPTERS = {a.name: a for a in (Claude(), Agy(), OpenCode(), Gemini(), Pi(), Hermes(), Codex())}


# ---------------------------------------------------------------- hermes helpers
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def hermes_dbs():
    dbs = [("default", HERMES_HOME / "state.db")]
    for p in sorted((HERMES_HOME / "profiles").glob("*/state.db")):
        dbs.append((p.parent.name, p))
    return [(n, p) for n, p in dbs if p.exists()]


def hermes_session(sid):
    for _, p in hermes_dbs():
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            c.close()
            if row:
                return row
        except sqlite3.Error:
            continue
    return None


def hermes_default_provider():
    try:
        txt = (HERMES_HOME / "config.yaml").read_text()
    except OSError:
        return None
    m = re.search(r"(?m)^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+provider:\s*['\"]?([\w.-]+)", txt)
    return m[1] if m else None


def hermes_bucket(provider):
    p = (provider or "").lower()
    if p.startswith("claude-subscription") or p == "anthropic-oauth":
        return "claude-sub"
    if p in ("custom", "local", "", "none", "unknown") or "local" in p:
        return "local"
    if p in ("openai-codex",):
        return "openai-codex"
    return f"hermes-{p}"


# ---------------------------------------------------------------- tiers and buckets
@dataclass
class Candidate:
    harness: str
    model: str = None
    effort: str = None
    spec: str = ""

    def label(self):
        return self.spec or f"{self.harness}:{self.model or ''}{'@' + self.effort if self.effort else ''}"


def parse_spec(spec):
    """'claude:sonnet@high' | 'agy:Claude Sonnet 5.5 (High)' | 'opencode:opencode/x-free' | 'hermes'"""
    spec = spec.strip()
    harness, _, rest = spec.partition(":")
    harness = harness.strip()
    model, effort = rest.strip() or None, None
    if model and "@" in model:
        model, effort = model.rsplit("@", 1)
        model, effort = model.strip() or None, effort.strip() or None
    if harness not in ADAPTERS:
        raise ValueError(f"unknown harness {harness!r} in {spec!r}; known: {', '.join(ADAPTERS)}")
    return Candidate(harness, model, effort, spec)


def bucket_for(conn, harness, model):
    bmap = store.cfg(conn, "bucket_map") or {}
    key = f"{harness}/{model or ''}"
    for pat, b in bmap.items():
        if fnmatch.fnmatch(key, pat) or fnmatch.fnmatch(harness, pat):
            return b
    return ADAPTERS[harness].bucket(model)


def candidates_for(conn, task):
    """Ordered candidates for a task: its pin if any, else its tier, then fallback tiers per tier_fallback."""
    if task["harness"]:
        return [Candidate(task["harness"], task["model"], None, f"{task['harness']}:{task['model'] or ''} (pinned)")], []
    tiers = store.cfg(conn, "tiers")
    order = store.cfg(conn, "tier_order") or list(tiers)
    tier = task["tier"] or store.cfg(conn, "default_tier")
    primary = [parse_spec(s) for s in tiers.get(tier, [])]
    fb = store.cfg(conn, "tier_fallback") or "none"
    extra = []
    if fb != "none" and tier in order:
        i = order.index(tier)
        ups, downs = order[i + 1:], list(reversed(order[:i]))
        seq = {"up": ups, "down": downs, "any": ups + downs}.get(fb, [])
        for t in seq:
            extra += [parse_spec(s) for s in tiers.get(t, [])]
    return primary, extra
