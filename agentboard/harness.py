"""Harness adapters: how to launch each agent CLI headless (on its own account or on an OpenAI-compatible endpoint),
read its result and usage, and classify failures."""
import fnmatch
import hashlib
import json
import os
import re
import shutil
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

    def bucket(self, model, provider=None):
        return hermes_bucket(provider or hermes_default_provider() or "")


class Codex(Adapter):
    name, bin, default_bucket = "codex", "codex", "openai-codex"

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
    endpoint: str = None
    provider: str = None

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
