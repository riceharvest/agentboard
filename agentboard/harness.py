"""Harness adapters: how to launch each agent CLI headless (on its own account or on an OpenAI-compatible endpoint),
read its result and usage, and classify failures."""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import store

# ---------------------------------------------------------------- failure classification
# Order matters: auth beats limit beats unavailable. These run on harness error text only,
# never on a successful result, so a task *about* rate limiting doesn't trip them.
AUTH_RE = re.compile(
    r"api[ _-]?key (?:has )?expired|invalid[ _-]api[ _-]key|authentication_?error|unauthori[sz]ed|\b401\b|"
    r"not (?:logged|signed) in|please (?:log ?in|login|sign in|run .{0,30}login)|GOOGLE_CLOUD_PROJECT|"
    r"oauth token (?:has )?expired|invalid_grant|credentials? (?:missing|not found|expired)|api_key_expired|"
    r"MISSING_CREDENTIAL|no API key for provider|authentication required|requires re-?authentication|"
    r"api_key client option must be set|no model configured|use /login|run [`'\"]?\w*\s*login|"
    r"no (?:access )?token found|re-?authenticate", re.I)
LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota|RESOURCE_EXHAUSTED|\b429\b|limit reached|hit your (?:usage )?limit|"
    r"too many requests|insufficient_quota|credits? (?:exhausted|depleted|balance)|out of credits|"
    r"weekly limit|5[- ]hour limit|session limit", re.I)
UNAVAIL_RE = re.compile(
    r"\b50[234]\b|\b529\b|overloaded|service unavailable|ECONNREFUSED|connection refused|in use by another job|"
    r"server_error|bad gateway|gateway timeout|ETIMEDOUT|ENOTFOUND|network error|socket hang up|APIConnectionError|"
    r"connection error|TRANSPORT:|ECONNRESET|fetch failed", re.I)


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
    error_kind: str = None     # auth | limit | unavailable | failed | timeout | None
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
    return (s or "").strip()[-n:]


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


_BOXED = re.compile(r"^\s*(?:[│┃┆║|╭╰┌└├]|[+\-<>]\s|\d+[:\t ]|@@|diff |index |---|\+\+\+)")
_HARNESS_OUT_LINE = re.compile(r"^\W*(?:error|api call failed|retrying api call|non-retryable|http \d{3}|\w*(?:error|exception):)", re.I)


def _provider_error_text(out, err):
    """Hermes: only what the harness itself reported. stderr minus boxed/diff-looking lines (tool output echoed there is
    agent content, e.g. code containing `in (429, 500)`), plus stdout lines that are log records or error lines. The
    transcript on stdout is never scanned."""
    keep = [l for l in (err or "").splitlines()[-200:] if l.strip() and not _BOXED.match(l)]
    keep += [l for l in (out or "").splitlines()[-200:]
             if l.strip() and not _BOXED.match(l) and (_HERMES_LINE.match(l) or _HARNESS_OUT_LINE.match(l))]
    return "\n".join(keep)[-4000:]


def _finish(r, rc, out, err, scan=None):
    """Shared post-processing: classify failures, find reset times. `scan`, when given, is the only text classified."""
    if not r.ok:
        blob = scan if scan is not None else "\n".join(x for x in (r.error, r.text if not r.ok else "", _tail(err, 4000)) if x)
        if not r.error_kind:
            r.error_kind = classify(blob) or "failed"
        if r.error_kind in ("auth", "limit", "unavailable"):
            hit = (_matched_lines(blob, r.error_kind) or _matched_lines(r.error or "", r.error_kind)) if scan is not None \
                else (_matched_lines(r.error or "", r.error_kind) or _matched_lines(blob, r.error_kind))
            r.error = hit or r.error
        if not r.error:
            r.error = _tail(blob, 1500) or f"exit code {rc} with no output"
        if r.error_kind == "limit":
            r.reset_at = parse_reset(blob)
    return r


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- endpoints
@dataclass
class Endpoint:
    """An OpenAI-compatible server (llama.cpp, vLLM, a gateway). Some harnesses also need /v1/responses
    (codex) or /v1/messages (claude); llama-server serves all three."""
    name: str
    base_url: str
    api_key_env: str = None
    context: int = 65536
    bucket: str = None

    @property
    def key(self):
        return (os.environ.get(self.api_key_env) if self.api_key_env else None) or "local"

    @property
    def root(self):
        return re.sub(r"/v1/?$", "", self.base_url.rstrip("/"))


def get_endpoint(conn, name):
    eps = store.cfg(conn, "endpoints") or {}
    if name not in eps:
        raise LookupError(f"no endpoint {name!r}; add it with `ab endpoints add {name} --base-url URL`")
    e = eps[name]
    return Endpoint(name, e["base_url"], e.get("api_key_env"), int(e.get("context") or 65536), e.get("bucket"))


def _cache_dir(ep, harness, model):
    h = hashlib.sha1(f"{ep.base_url}|{model}|{ep.context}".encode()).hexdigest()[:10]
    d = store.HOME / "endpoints" / ep.name / harness / h
    d.mkdir(parents=True, exist_ok=True)
    return d


class EndpointUnsupported(ValueError):
    pass


# ---------------------------------------------------------------- adapters
class Adapter:
    name = "?"
    bin = None
    harvested = False      # True when the usage harvester already counts this harness's sessions
    default_bucket = None
    provider_pin = False   # True when a spec may name the provider: 'hermes/nous:MODEL'
    # Idle watchdog: where a "still alive and working" signal comes from. "out" = the run's stdout/stderr stream
    # (a harness that streams events while it works); "probe" = probe()'s last_progress_at (its own log/transcript).
    # None = nothing to watch (most harnesses print one blob at exit), so a quiet run is never called idle.
    idle_signal = None

    def available(self):
        return bool(self.bin and shutil.which(self.bin))

    def command(self, brief, model=None, effort=None, extra=()):
        raise NotImplementedError

    def build(self, brief, cand, ep=None, extra=()):
        """-> (argv, env overrides). With an endpoint the harness is pointed at it through a per-run overlay;
        the user's own config files are never edited."""
        if ep is None:
            if cand.provider:
                return self.command(brief, cand.model, cand.effort, extra, provider=cand.provider), {}
            return self.command(brief, cand.model, cand.effort, extra), {}
        return self.endpoint_command(brief, cand.model, cand.effort, ep, extra)

    def endpoint_command(self, brief, model, effort, ep, extra):
        raise EndpointUnsupported(f"{self.name} can't run on a custom endpoint")

    def parse(self, rc, out, err):
        raise NotImplementedError

    def probe(self, run):
        """Optional: read the live run's OWN logs (never its stdout, never tool output) and report
        {"retry_count", "retry_since", "reset_at", "last_progress_at"}: how many provider 429 retries it has been
        stuck in since its last successful model call, since when, a reset hint, and when the last model call
        succeeded. `run` has .started, .workdir, .hermes_home, .out_path, .err_path. None = no such log."""
        return None

    def bucket(self, model):
        return self.default_bucket or self.name

    def list_models(self):
        return []


class Claude(Adapter):
    name, bin, harvested = "claude", "claude", True
    idle_signal = "probe"   # `-p --output-format json` prints once at exit: the session transcript shows life

    def probe(self, run):
        return probe_claude_transcript(run)

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["claude", "-p", brief, "--output-format", "json", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd + list(extra)

    def endpoint_command(self, brief, model, effort, ep, extra):
        # Needs Anthropic Messages at <root>/v1/messages (llama-server and most gateways serve it).
        env = {"ANTHROPIC_BASE_URL": ep.root, "ANTHROPIC_AUTH_TOKEN": ep.key, "ANTHROPIC_MODEL": model,
               "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        return self.command(brief, model, None, extra), env

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
        r.input, r.output = _int(u.get("input_tokens")), _int(u.get("output_tokens"))
        r.cache_read, r.cache_write = _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))
        r.reasoning = _int((u.get("output_tokens_details") or {}).get("thinking_tokens"))
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
        r.input, r.output = _int(u.get("input_tokens")), _int(u.get("output_tokens"))
        r.reasoning, r.cache_read = _int(u.get("thinking_tokens")), _int(u.get("cache_read_tokens"))
        r.session_id = doc.get("conversation_id")
        r.text = doc.get("response") or ""
        r.ok = rc == 0 and str(doc.get("status", "")).upper() == "SUCCESS"
        if not r.ok:
            e = doc.get("error")
            r.error = (e if isinstance(e, str) else json.dumps(e)) if e else (r.text or str(doc.get("status")))
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

    def endpoint_command(self, brief, model, effort, ep, extra):
        # Isolated config home: the user's global plugins/MCP servers/title model are not loaded.
        d = _cache_dir(ep, self.name, model)
        pid = f"ab-{ep.name}"
        cfg = {"$schema": "https://opencode.ai/config.json", "model": f"{pid}/{model}", "small_model": f"{pid}/{model}",
               "provider": {pid: {"npm": "@ai-sdk/openai-compatible", "name": f"agentboard {ep.name}",
                                  "options": {"baseURL": ep.base_url, "apiKey": "{env:AB_EP_KEY}"},
                                  "models": {model: {"name": model, "limit": {"context": ep.context, "output": 8192}}}}}}
        (d / "opencode").mkdir(exist_ok=True)
        (d / "opencode" / "opencode.json").write_text(json.dumps(cfg, indent=1))
        return self.command(brief, f"{pid}/{model}", effort, extra), {"XDG_CONFIG_HOME": str(d), "AB_EP_KEY": ep.key}

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
            if typ == "text" and part.get("text"):
                texts.append(part["text"])
            if typ in ("step_finish", "step-finish") or part.get("type") == "step-finish":
                tk = part.get("tokens") or {}
                r.input += _int(tk.get("input"))
                r.output += _int(tk.get("output"))
                r.reasoning += _int(tk.get("reasoning"))
                c = tk.get("cache") or {}
                r.cache_read += _int(c.get("read"))
                r.cache_write += _int(c.get("write"))
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
        if m.startswith("ab-"):
            return "local"  # an agentboard endpoint overlay
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
        for name, m in ((doc.get("stats") or {}).get("models") or {}).items():
            tk = (m or {}).get("tokens") or {}
            r.input += _int(tk.get("prompt") or tk.get("input"))
            r.output += _int(tk.get("candidates") or tk.get("output"))
            r.cache_read += _int(tk.get("cached"))
            r.reasoning += _int(tk.get("thoughts"))
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

    def endpoint_command(self, brief, model, effort, ep, extra):
        d = _cache_dir(ep, self.name, model)
        pid = f"ab-{ep.name}"
        (d / "models.json").write_text(json.dumps({"providers": {pid: {
            "name": f"agentboard {ep.name}", "baseUrl": ep.base_url, "api": "openai-completions", "apiKey": ep.key,
            "models": [{"id": model, "name": model, "contextWindow": ep.context}]}}}, indent=1))
        (d / "settings.json").write_text(json.dumps({"defaultProvider": pid, "defaultModel": model,
                                                     "defaultThinkingLevel": effort or "off"}))
        return self.command(brief, f"{pid}/{model}", effort, extra), {"PI_CODING_AGENT_DIR": str(d)}

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
                r.input += _int(u.get("input"))
                r.output += _int(u.get("output"))
                r.cache_read += _int(u.get("cacheRead"))
                r.cache_write += _int(u.get("cacheWrite"))
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
    provider_pin = True
    idle_signal = "probe"   # `hermes chat -Q` prints nothing until it exits: only the run's own agent.log shows life

    def probe(self, run):
        hh = getattr(run, "hermes_home", None)
        if not hh:  # shared gateway: its log is not this run's
            return None
        return probe_hermes_log(Path(hh) / "logs" / "agent.log")

    def command(self, brief, model=None, effort=None, extra=(), provider=None):
        cmd = ["hermes", "chat", "-q", brief, "--oneshot", "-Q", "--pass-session-id", "--yolo"]
        if provider:
            cmd += ["--provider", provider]
        if model:
            cmd += ["-m", model]
        return cmd + list(extra)

    def endpoint_command(self, brief, model, effort, ep, extra):
        # Hermes refuses models whose served context is under 64K.
        cmd = self.command(brief, model, effort, extra) + ["--provider", "custom", "--base-url", ep.base_url, "--api-key", ep.key]
        return cmd, {}

    def parse(self, rc, out, err, hermes_home=None):
        r = Result()
        combined = (err or "") + "\n" + (out or "")
        m = re.search(r"session_id:\s*(\S+)", combined) or re.search(r"(?m)^Session:\s*(\S+)", err or "")
        r.session_id = m[1] if m else None
        r.text = re.sub(r"(?m)^session_id:.*$", "", out or "").strip()
        row = hermes_session(r.session_id, home=hermes_home) if (r.session_id or hermes_home) else None
        if row:
            r.session_id = r.session_id or row.get("id")
            r.input, r.output = row.get("input_tokens") or 0, row.get("output_tokens") or 0
            r.cache_read, r.cache_write = row.get("cache_read_tokens") or 0, row.get("cache_write_tokens") or 0
            r.reasoning = row.get("reasoning_tokens") or 0
            r.model = row.get("model")
            cost = row.get("actual_cost_usd")
            if cost is None:
                cost = row.get("estimated_cost_usd")
            if cost is not None:
                r.cost_usd = float(cost)
        r.ok = rc == 0 and bool(r.text)
        if not r.ok:
            r.error = _tail(err) or r.text or f"exit {rc}"
        return _finish(r, rc, out, err, scan=_provider_error_text(out, err))

    def bucket(self, model, provider=None):
        return hermes_bucket(provider or hermes_default_provider() or "")


class Codex(Adapter):
    name, bin, default_bucket = "codex", "codex", "openai-codex"
    idle_signal = "out"     # `exec --json` streams one event per line while it works

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        return cmd + list(extra) + [brief]

    def endpoint_command(self, brief, model, effort, ep, extra):
        # Needs the Responses API at <base_url>/responses.
        pid = "ab_" + re.sub(r"\W", "_", ep.name)
        ov = ["-c", f'model_provider="{pid}"', "-c", f'model_providers.{pid}.name="{ep.name}"',
              "-c", f'model_providers.{pid}.base_url="{ep.base_url}"', "-c", f'model_providers.{pid}.wire_api="responses"',
              "-c", f'model_providers.{pid}.env_key="AB_EP_KEY"']
        return self.command(brief, model, effort, list(ov) + list(extra)), {"AB_EP_KEY": ep.key}

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
                r.input += _int(u.get("input_tokens"))
                r.cache_read += _int(u.get("cached_input_tokens"))
                r.output += _int(u.get("output_tokens"))
                r.reasoning += _int(u.get("reasoning_output_tokens"))
        r.text = "\n".join(texts).strip()
        r.ok = rc == 0 and saw and not errors and bool(texts)
        if errors:
            r.error = " | ".join(errors)
        elif not saw:
            r.error = _tail(err) or _tail(out)
        return _finish(r, rc, out, err)


class Kimi(Adapter):
    """Kimi Code CLI (npm @moonshot-ai/kimi-code)."""
    name, bin, default_bucket = "kimi", "kimi", "kimi-code"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["kimi", "-p", brief, "--output-format", "stream-json"]
        if model:
            cmd += ["-m", model]
        return cmd + list(extra)

    def endpoint_command(self, brief, model, effort, ep, extra):
        d = _cache_dir(ep, self.name, model)
        (d / "config.toml").write_text(
            'default_model = "ab"\n\n[models.ab]\nprovider = "ab"\n'
            f'model = {json.dumps(model)}\nmax_context_size = {ep.context}\n\n'
            f'[providers.ab]\ntype = "openai"\nbase_url = {json.dumps(ep.base_url)}\napi_key = {json.dumps(ep.key)}\n')
        return self.command(brief, None, effort, extra), {"KIMI_CODE_HOME": str(d)}

    def parse(self, rc, out, err):
        r = Result()
        texts, errors = [], []
        for o in _json_objects(out):
            if o.get("role") == "assistant":
                c = o.get("content")
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
                if c:
                    texts.append(c)
            if o.get("type") in ("error", "system.error") or (o.get("role") == "meta" and "error" in str(o.get("type"))):
                errors.append(str(o.get("content") or o.get("message") or o)[:800])
            if o.get("session_id"):
                r.session_id = o["session_id"]
            u = o.get("usage")
            if isinstance(u, dict):
                r.input += _int(u.get("input_tokens") or u.get("prompt_tokens"))
                r.output += _int(u.get("output_tokens") or u.get("completion_tokens"))
        r.text = (texts[-1] if texts else "").strip()
        r.ok = rc == 0 and not errors and bool(texts)
        if not r.ok:
            r.error = " | ".join(errors) or _tail(err) or _tail(out)
        return _finish(r, rc, out, err)


class Cursor(Adapter):
    """Cursor CLI (cursor-agent)."""
    name, bin, default_bucket = "cursor", "cursor-agent", "cursor"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["cursor-agent", "-p", "--output-format", "json", "--force", "--trust"]
        if model:
            cmd += ["--model", model]
        return cmd + list(extra) + [brief]

    def parse(self, rc, out, err):
        r = Result()
        doc = None
        for o in _json_objects(out):
            if o.get("type") == "result" or "result" in o:
                doc = o
        if doc is None:
            r.error = _tail(err) or _tail(out)
            return _finish(r, rc, out, err)
        u = doc.get("usage") or {}
        r.input = _int(u.get("inputTokens") or u.get("input_tokens"))
        r.output = _int(u.get("outputTokens") or u.get("output_tokens"))
        r.cache_read = _int(u.get("cacheReadTokens") or u.get("cache_read_input_tokens"))
        r.cache_write = _int(u.get("cacheWriteTokens") or u.get("cache_creation_input_tokens"))
        r.session_id = doc.get("session_id") or doc.get("sessionId")
        r.text = doc.get("result") or ""
        r.ok = rc == 0 and not doc.get("is_error") and doc.get("subtype", "success") == "success"
        if not r.ok:
            r.error = r.text or str(doc.get("subtype") or "")
        return _finish(r, rc, out, err)

    def list_models(self):
        p = subprocess.run(["cursor-agent", "--list-models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return [(l.split()[0], " ".join(l.split()[1:])) for l in p.stdout.splitlines() if l.strip() and not l.startswith(("Available", "-"))]


class Cline(Adapter):
    """Cline CLI."""
    name, bin, default_bucket = "cline", "cline", "cline"

    def command(self, brief, model=None, effort=None, extra=()):
        cmd = ["cline", "--json"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--thinking", effort]
        return cmd + list(extra) + [brief]

    def endpoint_command(self, brief, model, effort, ep, extra):
        d = _cache_dir(ep, self.name, model)
        if not (d / ".ab-configured").exists():
            p = subprocess.run(["cline", "auth", "--config", str(d), "-p", "openai", "-k", ep.key, "-m", model, "-b", ep.base_url],
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise RuntimeError(f"cline auth for endpoint failed: {(p.stderr or p.stdout).strip()[:300]}")
            (d / ".ab-configured").write_text("ok")
        return self.command(brief, None, effort, ["--config", str(d)] + list(extra)), {}

    def parse(self, rc, out, err):
        r = Result()
        res, errors = None, []
        for o in _json_objects(out):
            if o.get("type") == "run_result":
                res = o
            if o.get("type") == "error":
                errors.append(str(o.get("message"))[:800])
            if o.get("sessionId") and not r.session_id:
                r.session_id = o["sessionId"]
        for o in _json_objects(err):  # cline prints errors as JSON on stderr
            if o.get("type") == "error":
                errors.append(str(o.get("message"))[:800])
        if res:
            u = res.get("aggregateUsage") or res.get("usage") or {}
            r.input, r.output = _int(u.get("inputTokens")), _int(u.get("outputTokens"))
            r.cache_read, r.cache_write = _int(u.get("cacheReadTokens")), _int(u.get("cacheWriteTokens"))
            r.cost_usd = u.get("totalCost") or None
            r.text = res.get("text") or ""
            r.model = (res.get("model") or {}).get("id")
        r.ok = rc == 0 and res is not None and res.get("finishReason") == "completed"
        if not r.ok:
            r.error = " | ".join(errors) or (f"finished: {res.get('finishReason')}" if res else "") or _tail(err) or _tail(out)
        return _finish(r, rc, out, err)


class OpenHands(Adapter):
    """OpenHands CLI (headless)."""
    name, bin, default_bucket = "openhands", "openhands", "openhands"

    def command(self, brief, model=None, effort=None, extra=()):
        return ["openhands", "--headless", "--json", "-t", brief] + list(extra)

    def build(self, brief, cand, ep=None, extra=()):
        cmd, env = super().build(brief, cand, ep, extra)
        env.setdefault("COLUMNS", "100000")  # its JSON events go through a renderer that wraps at terminal width
        return cmd, env

    def endpoint_command(self, brief, model, effort, ep, extra):
        d = _cache_dir(ep, self.name, model)
        settings = d / ".openhands" / "agent_settings.json"
        if not settings.exists():
            settings.parent.mkdir(parents=True, exist_ok=True)
            py = Path(os.path.realpath(shutil.which("openhands"))).read_text(errors="replace").splitlines()[0][2:].strip()
            code = ("import sys\nfrom openhands.sdk import LLM\nfrom openhands_cli.utils import get_default_cli_agent\n"
                    f"a = get_default_cli_agent(LLM(model={json.dumps('openai/' + model)}, base_url={json.dumps(ep.base_url)}, "
                    "api_key='x', usage_id='agent'))\nopen(sys.argv[1], 'w').write(a.model_dump_json())\n")
            p = subprocess.run([py, "-c", code, str(settings)], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise RuntimeError(f"openhands settings for endpoint failed: {p.stderr.strip()[-300:]}")
        # The saved settings mask the key, so it travels in the environment.
        return self.command(brief, model, effort, extra), {"HOME": str(d), "OPENAI_API_KEY": ep.key}

    def parse(self, rc, out, err):
        r = Result()
        t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out or "")
        t = re.sub(r"(?m)^.?\s*Agent is working\r?\n?", "", t)
        dec = json.JSONDecoder(strict=False)
        texts, errors = [], []
        for blk in t.split("--JSON Event--")[1:]:
            s = blk.lstrip()
            try:
                o, _ = dec.raw_decode(s)
            except ValueError:
                continue
            kind = o.get("kind")
            if kind == "MessageEvent" and o.get("source") == "agent":
                txt = "".join(c.get("text", "") for c in ((o.get("llm_message") or {}).get("content") or []) if isinstance(c, dict))
                if txt.strip():
                    texts.append(txt)
            elif kind == "ActionEvent":
                a = o.get("action") or {}
                if a.get("kind") == "FinishAction" and a.get("message"):
                    texts.append(a["message"])
            elif kind in ("ConversationErrorEvent", "AgentErrorEvent"):
                errors.append(str(o.get("detail") or o.get("error") or o)[:800])
        m = re.search(r"Conversation ID:\s*(\S+)", t)
        r.session_id = m[1] if m else None
        r.text = (texts[-1] if texts else "").strip()
        r.ok = rc == 0 and not errors and bool(texts)
        if not r.ok:
            r.error = " | ".join(errors) or _tail(err) or "no agent message in the output"
        return _finish(r, rc, out, err)


class DeepSeek(Adapter):
    """DeepSeek Harness (dsh), headless profile."""
    name, bin, default_bucket = "deepseek", "dsh", "deepseek-api"

    def command(self, brief, model=None, effort=None, extra=(), pre=()):
        return ["dsh"] + list(pre) + ["--profile", "headless", "--json"] + list(extra) + [brief]

    def build(self, brief, cand, ep=None, extra=()):
        cmd, env = super().build(brief, cand, ep, extra)
        # Like every other harness here: no sandbox, no approval prompts. Its default workspace-write sandbox
        # also makes the board database read-only, so `ab done` would fail.
        env.setdefault("DSH_PERMISSION_MODE", "danger-full-access")
        return cmd, env

    def endpoint_command(self, brief, model, effort, ep, extra):
        d = _cache_dir(ep, self.name, model)
        pid = f"ab-{re.sub(r'[^a-z0-9-]', '-', ep.name.lower())}"
        (d / "patch.yml").write_text(
            "- id: llm-pi-ai\n  config:\n    providers:\n"
            f"      {pid}:\n        apiKeyEnv: AB_EP_KEY\n        api: openai-completions\n        baseURL: {json.dumps(ep.base_url)}\n"
            "        compat:\n          supportsDeveloperRole: false\n          maxTokensField: max_tokens\n"
            f"        models:\n          - id: {json.dumps(model)}\n            contextWindow: {ep.context}\n            maxTokens: 8192\n"
            f"- id: agent-default-model\n  config:\n    provider: {pid}\n    model: {json.dumps(model)}\n")
        return self.command(brief, None, effort, extra, pre=["--patch", str(d / "patch.yml")]), {"AB_EP_KEY": ep.key}

    def parse(self, rc, out, err):
        r = Result()
        final, errors = None, []
        for o in _json_objects(out):
            typ = o.get("type")
            if not r.session_id:
                r.session_id = o.get("sessionId") or o.get("session_id")
            if typ == "final":
                final = o.get("text") or ""
            if typ == "status" and o.get("phase") == "step_end":
                u = o.get("usage") or {}
                r.input += _int(u.get("inputTokens"))
                r.output += _int(u.get("outputTokens"))
                r.cache_read += _int(u.get("cacheReadTokens"))
            if typ == "status" and o.get("phase") == "turn_end":
                reason = o.get("reason") or {}
                if reason.get("kind") == "error":
                    e = reason.get("error") or {}
                    errors.append(f"{e.get('code', '')}: {e.get('message', '')}"[:800])
        r.text = (final or "").strip()
        r.ok = rc == 0 and not errors and bool(r.text)
        if not r.ok:
            r.error = " | ".join(errors) or _tail(err) or _tail(out)
        return _finish(r, rc, out, err)


ADAPTERS = {a.name: a for a in (Claude(), Agy(), OpenCode(), Gemini(), Pi(), Hermes(), Codex(),
                                Kimi(), Cursor(), Cline(), OpenHands(), DeepSeek())}


# ---------------------------------------------------------------- hermes helpers
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()

_HERMES_STRIP_ENV_PREFIXES = (
    "TELEGRAM_", "DISCORD_", "SLACK_", "WHATSAPP_", "MATRIX_", "SIGNAL_",
    "WEIXIN_", "WECHAT_", "IMESSAGE_", "SMS_", "MSTEAMS_", "TEAMS_",
    "BLUEBUBBLES_", "DINGTALK_", "FEISHU_", "LARK_", "LINE_", "MATTERMOST_",
    "IRC_", "ZULIP_", "HERMES_GATEWAY_", "HERMES_TUI_GATEWAY_",
    "HERMES_KANBAN_", "RELAY_", "GATEWAY_", "API_SERVER_",
)
_HERMES_STRIP_ENV_SUFFIXES = (
    "_BOT_TOKEN", "_APP_TOKEN", "_WEBHOOK_URL", "_WEBHOOK_SECRET",
)
_HERMES_STRIP_ENV_EXACT = frozenset({
    "SUDO_PASSWORD", "HERMES_PROFILE",
})
_HERMES_STRIP_YAML_KEYS = frozenset({
    "platforms", "discord", "telegram", "slack", "whatsapp", "signal", "matrix",
    "weixin", "wechat", "imessage", "sms", "msteams", "teams", "bluebubbles",
    "dingtalk", "feishu", "lark", "line", "mattermost", "irc", "zulip",
    "gateway", "kanban", "dashboard", "cron", "desktop", "updates", "onboarding",
    "platform_toolsets",
})


def _is_stripped_hermes_env_key(key):
    k = key.strip().upper()
    return (k in _HERMES_STRIP_ENV_EXACT
            or k.startswith(_HERMES_STRIP_ENV_PREFIXES)
            or k.endswith(_HERMES_STRIP_ENV_SUFFIXES))


def _sanitize_hermes_env(text):
    out = []
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        lhs = s.split("=", 1)[0].strip()
        if lhs.startswith("export "):
            lhs = lhs[7:].strip()
        if _is_stripped_hermes_env_key(lhs):
            continue
        out.append(line)
    return "".join(out)


def _sanitize_hermes_config(text):
    out = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line and not line[0].isspace() and not line.startswith("#"):
            m = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
            if m:
                skipping = m.group(1) in _HERMES_STRIP_YAML_KEYS
                if skipping:
                    continue
            else:
                skipping = False
        elif skipping:
            continue
        out.append(line)
    return "".join(out)


RUN_PROFILE_PREFIX = "ab-run-"


def hermes_run_home(run_id, root=None):
    """Per-run HERMES_HOME shaped as a named profile, <root>/profiles/ab-run-N. Hermes' protected-instruction gate then
    exempts the Hermes root as well as the home; a home outside <root>/profiles made it treat the real root's files as
    project-local .hermes config and block every write to them (AB_FRICTION hermes#191)."""
    return Path(root or HERMES_HOME) / "profiles" / f"{RUN_PROFILE_PREFIX}{run_id}"


def keep_hermes_agent_log(home, dest, cap=2 * 1024 * 1024):
    """Copy the last *cap* bytes of the run home's logs/agent.log to *dest* before the home is deleted, so a dead run's
    provider 429/retry lines stay readable. Returns the bytes kept, or None."""
    src = Path(home) / "logs" / "agent.log"
    try:
        with open(src, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - cap))
            data = f.read()
        Path(dest).write_bytes(data)
        return len(data)
    except OSError:
        return None


def prepare_hermes_home(dest, source=None):
    """Create an isolated per-run HERMES_HOME with provider auth/config but no bot tokens or gateway locks."""
    src = Path(source) if source else HERMES_HOME
    dst = Path(dest)
    dst.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dst, 0o700)
    except OSError:
        pass
    auth_file = src / "auth.json"
    if auth_file.is_file():
        shutil.copy2(auth_file, dst / "auth.json")
        try:
            os.chmod(dst / "auth.json", 0o600)
        except OSError:
            pass
    auth_dir = src / "auth"
    if auth_dir.is_dir():
        shutil.copytree(auth_dir, dst / "auth", dirs_exist_ok=True)
    env_file = src / ".env"
    if env_file.is_file():
        out_env = dst / ".env"
        out_env.write_text(_sanitize_hermes_env(env_file.read_text(errors="replace")))
        try:
            os.chmod(out_env, 0o600)
        except OSError:
            pass
    cfg_file = src / "config.yaml"
    if cfg_file.is_file():
        out_cfg = dst / "config.yaml"
        out_cfg.write_text(_sanitize_hermes_config(cfg_file.read_text(errors="replace")))
        try:
            os.chmod(out_cfg, 0o600)
        except OSError:
            pass
    return dst


def _pid_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _pid_alive(pid, start=None):
    if not pid or pid <= 1:
        return False
    st = _pid_start(pid)
    if st is None:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except OSError:
        return False
    return start is None or str(st) == str(start)


def read_hermes_gateway_pid(home):
    """Return (pid, start_time) from <home>/gateway.pid if it belongs to *home*, else (None, None)."""
    home_p = Path(home)
    pid_file = home_p / "gateway.pid"
    if not pid_file.is_file():
        return None, None
    try:
        raw = pid_file.read_text(errors="replace").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    pid, start = None, None
    if raw.startswith("{"):
        try:
            doc = json.loads(raw)
        except ValueError:
            return None, None
        recorded_home = doc.get("hermes_home")
        if recorded_home:
            try:
                if os.path.realpath(recorded_home) != os.path.realpath(home_p):
                    return None, None
            except OSError:
                return None, None
        try:
            pid = int(doc["pid"])
        except (KeyError, TypeError, ValueError):
            return None, None
        if doc.get("start_time") is not None:
            start = str(doc["start_time"])
    else:
        try:
            pid = int(raw.splitlines()[0].strip())
        except ValueError:
            return None, None
    if not pid or pid <= 1:
        return None, None
    return pid, start


def _signal_pid_or_pgid(pid, sig):
    try:
        pgid = os.getpgid(pid)
        my_pgid = os.getpgid(0)
        if pgid == pid and pgid != my_pgid:
            os.killpg(pgid, sig)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop_hermes_gateway(home, force=False, wait_s=0.0):
    """Stop the ephemeral Hermes gateway owned by an isolated per-run *home*.
    Refuses to target the user's shared HERMES_HOME."""
    if not home:
        return False
    home_p = Path(home)
    if not home_p.exists():
        return False
    try:
        if os.path.realpath(home_p) == os.path.realpath(HERMES_HOME):
            return False
    except OSError:
        pass
    pid, start = read_hermes_gateway_pid(home_p)
    if pid is not None and not _pid_alive(pid, start):
        return False
    has_live = pid is not None or (home_p / "gateway.lock").exists()
    if not has_live:
        return False
    if not force:
        if shutil.which("hermes"):
            env = {k: v for k, v in os.environ.items() if not _is_stripped_hermes_env_key(k)}
            env["HERMES_HOME"] = str(home_p)
            try:
                subprocess.run(["hermes", "gateway", "stop", "--yes"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        pid, start = read_hermes_gateway_pid(home_p)
        if pid and _pid_alive(pid, start):
            _signal_pid_or_pgid(pid, signal.SIGTERM)
        if wait_s > 0 and pid and _pid_alive(pid, start):
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and _pid_alive(pid, start):
                time.sleep(0.05)
            if _pid_alive(pid, start):
                _signal_pid_or_pgid(pid, signal.SIGKILL)
        return True
    if pid and _pid_alive(pid, start):
        _signal_pid_or_pgid(pid, signal.SIGKILL)
        return True
    return False


def hermes_session_id_from_log(text):
    """The session id a running `hermes chat` printed early on stderr ('Session: <id>').
    Only complete lines count: the run's stderr is read while the client may still be writing it."""
    m = (re.search(r"(?m)^Session:[ \t]*(\S+)[ \t]*$", text or "")
         or re.search(r"(?m)^session_id:[ \t]*(\S+)[ \t]*$", text or ""))
    return m[1] if m else None


def hermes_run_session_id(workdir, started_at, home=None):
    """The live session a run's `hermes chat` created, from the gateway's session store.

    The classic `chat -q` client prints no session id until it exits, but the gateway records the
    session row at creation with the client's cwd. Match the newest unterminated session in the
    run's workdir started at/after the run: the workdir lock keeps concurrent runs out of the same
    directory (with `share_workdir` two overlapping runs could match; the newest wins)."""
    if not workdir or not started_at:
        return None
    try:
        wd = os.path.realpath(workdir)
    except OSError:
        return None
    for _, p in hermes_dbs(home):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            rows = c.execute("SELECT id, cwd FROM sessions WHERE ended_at IS NULL AND started_at >= ? "
                             "ORDER BY started_at DESC LIMIT 20", (started_at - 10,)).fetchall()
            c.close()
            for sid, cwd in rows:
                if cwd and os.path.realpath(cwd) == wd:
                    return sid
        except (sqlite3.Error, OSError):
            continue
    return None


class _HermesRpcError(Exception):
    """A gateway JSON-RPC error answer (its bounded reason text)."""


class _WebsocketsRpc:
    """One synchronous JSON-RPC connection over the gateway WebSocket, using `websockets` when installed."""

    def __init__(self, url, protocols, timeout_s):
        self._url = url
        self._protocols = list(protocols)
        self._timeout_s = timeout_s
        self._conn = None
        self._ws = None
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self._ws.send(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}))
        deadline = time.monotonic() + self._timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"gateway rpc timed out: {method}")
            doc = json.loads(self._ws.recv(timeout=remaining))
            if not isinstance(doc, dict) or doc.get("id") != self._id:
                continue  # an event or another answer
            if "error" in doc:
                raise _HermesRpcError(str((doc["error"] or {}).get("message") or doc["error"])[:200])
            return doc.get("result") or {}

    def __enter__(self):
        from websockets.sync.client import connect
        self._conn = connect(self._url, subprotocols=self._protocols, open_timeout=self._timeout_s,
                             close_timeout=2, ping_interval=None, proxy=None, max_size=8 * 1024 * 1024)
        self._ws = self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        if self._conn is not None:
            self._conn.__exit__(*exc)


class _StdlibWsRpc:
    """The same JSON-RPC connection with no third-party dependency: a minimal RFC 6455 client
    (text frames, fragmentation, ping/pong, close) for installs without `websockets`."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, protocols, timeout_s):
        import base64
        import socket
        import ssl
        from urllib.parse import urlsplit
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError(f"unsupported gateway url: {url[:80]}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        self._sock = socket.create_connection((u.hostname, port), timeout=timeout_s)
        if u.scheme == "wss":
            self._sock = ssl.create_default_context().wrap_socket(self._sock, server_hostname=u.hostname)
        self._timeout_s = timeout_s
        self._id = 0
        self._buf = b""
        self._deadline = time.monotonic() + timeout_s
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self._sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n"
                            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                            f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: {', '.join(protocols)}\r\n\r\n").encode())
        while b"\r\n\r\n" not in self._buf:
            self._buf += self._recv_chunk()
            if len(self._buf) > 65536:
                raise ConnectionError("gateway handshake too large")
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        if "101" not in lines[0]:
            raise ConnectionError(f"gateway refused the websocket: {lines[0][:120]}")
        hdrs = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        expect = base64.b64encode(hashlib.sha1((key + self._GUID).encode()).digest()).decode()
        if hdrs.get("sec-websocket-accept") != expect:
            raise ConnectionError("gateway websocket accept mismatch")
        if hdrs.get("sec-websocket-protocol") not in protocols:
            raise ConnectionError("gateway websocket protocol mismatch")

    def _recv_chunk(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("gateway rpc timed out")
        self._sock.settimeout(remaining)
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionError("gateway closed the websocket")
        return chunk

    def _recv_exact(self, n):
        while len(self._buf) < n:
            self._buf += self._recv_chunk()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        masked, ln = b2 & 0x80, b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._recv_exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._recv_exact(8), "big")
        if ln > 16 * 1024 * 1024:
            raise ConnectionError("gateway frame too large")
        mask = self._recv_exact(4) if masked else None
        data = self._recv_exact(ln)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self._sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def call(self, method, **params):
        self._id += 1
        self._deadline = time.monotonic() + self._timeout_s
        self._send_frame(1, json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode())
        frag = b""
        while True:
            fin, opcode, data = self._read_frame()
            if opcode == 9:  # ping: answer and keep waiting
                self._send_frame(10, data)
                continue
            if opcode == 8:
                raise ConnectionError("gateway closed the websocket")
            if opcode not in (0, 1, 2):
                continue
            frag += data
            if not fin:
                continue
            payload, frag = frag, b""
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("id") != self._id:
                continue
            if "error" in doc:
                raise _HermesRpcError(str((doc["error"] or {}).get("message") or doc["error"])[:200])
            return doc.get("result") or {}

    def close(self):
        try:
            self._deadline = time.monotonic() + 2
            self._send_frame(8, (1000).to_bytes(2, "big"))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _hermes_ws_rpc(url, protocols, timeout_s):
    try:
        import websockets.sync.client  # noqa: F401
    except ImportError:
        return _StdlibWsRpc(url, protocols, timeout_s)
    return _WebsocketsRpc(url, protocols, timeout_s)


def interrupt_hermes_session(session_id, home=None, timeout_s=5.0):
    """Stop ONE session on a Hermes gateway without stopping the gateway process.

    Killing `hermes chat` does not reliably stop the gateway-side session: the transport client
    detaches on SIGINT ('accepted work continues at the gateway'), and the classic -q client's
    signal interrupt is best-effort (a lost RPC, a startup race or SIGKILL leaves the turn running).
    The documented external stop is the gateway JSON-RPC `session.interrupt` -- what chat's /stop
    sends -- reached through `hermes gateway attach --json` (the owner's WebSocket url + single-use
    ticket subprotocols). `session.interrupt` needs the live execution_generation, read from a
    lightweight `session.resume`. Returns (ok, detail) for the run log; ok means the session is no
    longer running. Never starts a gateway and never signals the gateway process, so a shared
    gateway keeps serving everyone else.
    """
    if not session_id:
        return False, "no session id"
    if not shutil.which("hermes"):
        return False, "hermes binary not found"
    env = {k: v for k, v in os.environ.items() if not _is_stripped_hermes_env_key(k)}
    if home:
        env["HERMES_HOME"] = str(home)
    try:
        p = subprocess.run(["hermes", "gateway", "attach", "--json"], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=max(8.0, timeout_s + 3))
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"gateway attach failed: {e}"
    if p.returncode != 0:
        # No attachable owner: nothing can be running there. (On success the stdout carries the
        # single-use ticket and is never logged.)
        return False, f"gateway attach exited {p.returncode}: {_tail(p.stderr or p.stdout)}"
    try:
        doc = json.loads(p.stdout)
        url, protocols = doc["url"], list(doc["protocols"])
    except (ValueError, KeyError, TypeError) as e:
        return False, f"gateway attach gave no url/protocols: {e}"

    def _detach(rpc, sub):
        if sub:
            try:
                rpc.call("session.detach", session_id=session_id, subscription_id=sub)
            except (_HermesRpcError, OSError, ValueError):
                pass

    try:
        with _hermes_ws_rpc(url, protocols, timeout_s) as rpc:
            snap = rpc.call("session.resume", session_id=session_id, omit_messages=True)
            sub = snap.get("subscription_id")
            if not snap.get("running"):
                _detach(rpc, sub)
                return True, "session was not running"
            gen = snap.get("execution_generation")
            try:
                rpc.call("session.interrupt", session_id=session_id, execution_generation=gen)
            except _HermesRpcError as e:
                if "stale_generation" not in str(e):
                    raise
                # The turn settled or a new one started between resume and interrupt: re-read once.
                snap = rpc.call("session.resume", session_id=session_id, omit_messages=True)
                sub = snap.get("subscription_id") or sub
                if not snap.get("running"):
                    _detach(rpc, sub)
                    return True, "session was not running"
                gen = snap.get("execution_generation")
                rpc.call("session.interrupt", session_id=session_id, execution_generation=gen)
            _detach(rpc, sub)
            return True, f"interrupted generation {gen}"
    except _HermesRpcError as e:
        if "not_found" in str(e):
            return True, "session not found at the gateway"
        return False, f"gateway refused the stop: {e}"
    except (OSError, ValueError) as e:
        return False, f"gateway rpc failed: {e}"


def hermes_dbs(home=None):
    base = Path(home) if home else HERMES_HOME
    dbs = [("default", base / "state.db")]
    for p in sorted((base / "profiles").glob("*/state.db")):
        dbs.append((p.parent.name, p))
    return [(n, p) for n, p in dbs if p.exists()]


def hermes_session(sid, home=None):
    for _, p in hermes_dbs(home):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            c.row_factory = sqlite3.Row
            if sid:
                row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            elif home:
                row = c.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
            else:
                row = None
            c.close()
            if row:
                return dict(row)
        except sqlite3.Error:
            continue
    return None


def export_hermes_session(home, dest):
    """Write the transcript of every session in an isolated per-run *home* to *dest* (JSONL, one message per line,
    with its timestamp) so the run can be audited after the home is deleted. Returns the message count, or None."""
    n = 0
    try:
        with open(dest, "w") as fo:
            for _, p in hermes_dbs(home):
                c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
                c.row_factory = sqlite3.Row
                try:
                    for r in c.execute("SELECT * FROM messages ORDER BY timestamp, id"):
                        fo.write(json.dumps(dict(r), default=str) + "\n")
                        n += 1
                finally:
                    c.close()
    except (OSError, sqlite3.Error):
        return None
    return n


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


# ---------------------------------------------------------------- stall probes (live run logs)
# These read only lines the HARNESS wrote (anchored on its logger prefix / record type), never the model's or a
# tool's output, so a card that is *about* rate limiting cannot trip them.
_RL_RE = re.compile(r"\b429\b|RateLimit|rate[ _-]?limit|too many requests|upstream capacity", re.I)
_HERMES_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) (?:DEBUG|INFO|WARNING|ERROR) "
                          r"(?:\[[^\]\s]+\] )?(agent\.[\w.]+): (.*)$")
_HERMES_OK = re.compile(r"API call #\d+:")
_HERMES_RETRY = re.compile(r"Retrying API call in|API call failed \(attempt|Nous 429 looks like upstream capacity")


def _read_tail(path, n=524288):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _streak(events):
    """events: time-ordered ('ok'|'rl', epoch, text). -> retry_count, retry_since, reset_at, last_progress_at, since the last 'ok'."""
    last_ok, count, since, hint = None, 0, None, None
    for kind, ts, text in events:
        if kind == "ok":
            last_ok, count, since, hint = ts, 0, None, None
        else:
            count += 1
            since = since or ts
            hint = parse_reset(text, base=ts) or hint
    return {"retry_count": count, "retry_since": since, "reset_at": hint, "last_progress_at": last_ok}


def probe_hermes_log(path):
    """Hermes per-run HERMES_HOME logs/agent.log. Missing file -> a probe with no progress yet (not None: the run has a log
    channel, it just has not written a model call). A 429 streak = provider-429 retry lines since the last `API call #N:`."""
    txt = _read_tail(path)
    if txt is None:
        return {"retry_count": 0, "retry_since": None, "reset_at": None, "last_progress_at": None}
    events = []
    for line in txt.splitlines():
        m = _HERMES_LINE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").timestamp() + int(m[2]) / 1000.0
        except ValueError:
            continue
        mod, msg = m[3], m[4]
        if mod == "agent.conversation_loop" and _HERMES_OK.match(msg):
            events.append(("ok", ts, msg))
        elif _HERMES_RETRY.search(msg) and _RL_RE.search(msg):
            events.append(("rl", ts, msg))
    return _streak(events)


def _claude_projects_dir(workdir):
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(workdir))


def probe_claude_transcript(run):
    """Claude Code session transcript (~/.claude/projects/<cwd slug>/<session>.jsonl): newest file written since the run
    started. `system`/`api_error` records with status 429 are the retry streak; assistant/user records end it. For the idle
    verdict last_progress_at also counts the file's mtime and anything written under its session dir (subagent transcripts,
    tool results), except while the file's tail is an api_error (a retry loop also grows the file)."""
    wd = getattr(run, "workdir", None)
    if not wd:
        return None
    cache = getattr(run, "cache", None)
    if cache is None:
        cache = {}
    path = cache.get("claude_path")
    if path is None or not os.path.exists(path) or time.time() - cache.get("claude_at", 0) > 60:
        try:  # re-pick now and then (a cheap glob of one project dir) so another session's file is not blamed for good
            files = [f for f in _claude_projects_dir(wd).glob("*.jsonl") if f.stat().st_mtime >= run.started - 2]
        except OSError:
            return None
        if not files:
            return None
        path = str(max(files, key=lambda f: f.stat().st_mtime))
        cache["claude_path"], cache["claude_at"] = path, time.time()
    txt = _read_tail(path, 262144)
    if txt is None:
        return None
    events, tail_err = [], False
    for line in txt.splitlines():
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
            ts = datetime.fromisoformat(str(o.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if o.get("type") == "system" and o.get("subtype") == "api_error":
            tail_err = True
            e = o.get("error") or {}
            blob = f"{e.get('status', '')} {e.get('message', '')} {e.get('formatted', '')}"
            if e.get("status") == 429 or _RL_RE.search(blob):
                events.append(("rl", ts, blob))
        elif o.get("type") in ("assistant", "user"):
            tail_err = False
            events.append(("ok", ts, ""))
    out = _streak(events)
    alive = out["last_progress_at"] or 0
    try:
        if not tail_err:
            alive = max(alive, os.stat(path).st_mtime)
        sdir = Path(path[:-len(".jsonl")])
        if sdir.is_dir():
            for f in sdir.rglob("*"):
                if f.is_file():
                    alive = max(alive, f.stat().st_mtime)
    except OSError:
        pass
    out["last_progress_at"] = alive or None
    return out


# ---------------------------------------------------------------- tiers and buckets
@dataclass
class Candidate:
    harness: str
    model: str = None
    effort: str = None
    spec: str = ""
    endpoint: str = None
    provider: str = None
    tier: str = None        # the tier this entry came from (None for a pinned task); keys extra_args_tier

    def label(self):
        if self.spec:
            return self.spec
        h = self.harness + (f"/{self.provider}" if self.provider else "") + (f"@{self.endpoint}" if self.endpoint else "")
        return f"{h}:{self.model or ''}{'@' + self.effort if self.effort else ''}"


def parse_spec(spec):
    """'claude:sonnet@high' | 'agy:Claude Sonnet 5.5 (High)' | 'codex:@low' | 'hermes'
    | 'pi@rtx3060:spark-1.7b' (harness on a registered endpoint)
    | 'hermes/nous:qwen/qwen4-max:free' (provider pin: everything before the FIRST ':' is the head, so model ids
      may contain '/' and ':' freely)"""
    spec = spec.strip()
    head, _, rest = spec.partition(":")
    harness, _, endpoint = head.strip().partition("@")
    harness, _, provider = harness.partition("/")
    harness, endpoint, provider = harness.strip(), endpoint.strip() or None, provider.strip() or None
    model, effort = rest.strip() or None, None
    if model and "@" in model:
        model, effort = model.rsplit("@", 1)
        model, effort = model.strip() or None, effort.strip() or None
    if harness not in ADAPTERS:
        raise ValueError(f"unknown harness {harness!r} in {spec!r}; known: {', '.join(ADAPTERS)}")
    if endpoint and not model:
        raise ValueError(f"{spec!r}: a harness on an endpoint needs a model, e.g. {harness}@{endpoint}:MODEL")
    if provider:
        if not ADAPTERS[harness].provider_pin:
            pinned = [n for n, a in ADAPTERS.items() if a.provider_pin]
            raise ValueError(f"{spec!r}: {harness} has no provider pin (only {', '.join(pinned)}); "
                             f"opencode and pi take the provider inside the model id: {harness}:PROVIDER/MODEL")
        if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", provider):
            raise ValueError(f"{spec!r}: bad provider name {provider!r}")
        if endpoint:
            raise ValueError(f"{spec!r}: a provider pin and an endpoint can't be combined (an endpoint is its own provider)")
    return Candidate(harness, model, effort, spec, endpoint, provider)


def bucket_for(conn, harness, model, endpoint=None, provider=None):
    if endpoint:
        ep = (store.cfg(conn, "endpoints") or {}).get(endpoint) or {}
        return ep.get("bucket") or f"ep:{endpoint}"
    bmap = store.cfg(conn, "bucket_map") or {}
    key = f"{harness}/{model or ''}"
    for pat, b in bmap.items():
        if fnmatch.fnmatch(key, pat) or fnmatch.fnmatch(harness, pat):
            return b
    if provider:
        return ADAPTERS[harness].bucket(model, provider=provider)
    return ADAPTERS[harness].bucket(model)


def candidates_for(conn, task):
    """Ordered candidates for a task: its pin if any, else its tier, then fallback tiers per tier_fallback."""
    if task["harness"]:
        c = parse_spec(f"{task['harness']}:{task['model'] or ''}")
        c.spec = f"{c.label()} (pinned)"
        return [c], []
    tiers = store.cfg(conn, "tiers")
    order = store.cfg(conn, "tier_order") or list(tiers)
    tier = task["tier"] or store.cfg(conn, "default_tier")
    primary = [_in_tier(parse_spec(s), tier) for s in tiers.get(tier, [])]
    fb = store.cfg(conn, "tier_fallback") or "none"
    extra = []
    if fb != "none" and tier in order:
        i = order.index(tier)
        ups, downs = order[i + 1:], list(reversed(order[:i]))
        seq = {"up": ups, "down": downs, "any": ups + downs}.get(fb, [])
        for t in seq:
            extra += [_in_tier(parse_spec(s), t) for s in tiers.get(t, [])]
    return primary, extra


def _in_tier(cand, tier):
    cand.tier = tier
    return cand


def extra_args_for(conn, cand):
    """CLI args appended to a launch: `extra_args[harness]` first, then `extra_args_tier[tier][harness]` for the tier
    the candidate's entry sits in (a fallback entry uses ITS tier, not the card's). Both empty by default -> []."""
    out = list((store.cfg(conn, "extra_args") or {}).get(cand.harness) or [])
    if cand.tier:
        out += list(((store.cfg(conn, "extra_args_tier") or {}).get(cand.tier) or {}).get(cand.harness) or [])
    return merge_settings_args(out) if cand.harness == "claude" else out


def _settings_obj(val):
    """A `--settings` value (inline JSON or a path to a JSON file) as a dict, or None when it is neither."""
    try:
        o = json.loads(val)
    except (TypeError, ValueError):
        try:
            o = json.loads(Path(val).expanduser().read_text())
        except (OSError, ValueError, TypeError):
            return None
    return o if isinstance(o, dict) else None


def _merge_settings(a, b):
    """Deep-merge settings dicts: dicts merge key by key, lists concatenate (hooks per event), else b wins."""
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge_settings(out[k], v)
        elif k in out and isinstance(out[k], list) and isinstance(v, list):
            out[k] = out[k] + [x for x in v if x not in out[k]]
        else:
            out[k] = v
    return out


def merge_settings_args(args):
    """claude keeps only the LAST of repeated `--settings`, so base + tier settings would drop the base hooks. Merge every
    `--settings X` / `--settings=X` into one `--settings JSON`, placed where the first one was. Unparseable values stay as is."""
    rest, objs, first = [], [], None
    i = 0
    while i < len(args):
        a = args[i]
        val = None
        if a == "--settings" and i + 1 < len(args):
            val, step = args[i + 1], 2
        elif isinstance(a, str) and a.startswith("--settings="):
            val, step = a[len("--settings="):], 1
        else:
            rest.append(a)
            i += 1
            continue
        o = _settings_obj(val)
        if o is None:
            rest += args[i:i + step]
        else:
            if first is None:
                first = len(rest)
            objs.append(o)
        i += step
    if len(objs) < 2:
        return list(args)
    merged = objs[0]
    for o in objs[1:]:
        merged = _merge_settings(merged, o)
    return rest[:first] + ["--settings", json.dumps(merged, separators=(",", ":"))] + rest[first:]
