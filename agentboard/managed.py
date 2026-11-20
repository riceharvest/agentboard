"""Managed endpoints: a local model server the runner starts when a card needs it and stops when it sits idle.

Opt-in per endpoint (`ab endpoints add NAME ... --start-cmd CMD`). The runner only ever stops a server it started
itself; one that was already up (started by hand, by another tool) is used as it is and left running. Endpoints that
share one device name the same `--group`: only one of them is up at a time, and the runner switches between them only
when the one that is up has no board runs.

State lives in the control table under `endpoint_state` (name -> pid, pid_start, status, started_at, ...), so a runner
reload re-adopts a server it started and still stops it when idle.
"""
import json
import os
import re
import signal
import subprocess
import urllib.request

from . import store
from .store import cfg, event, now, tx

STATE_KEY = "endpoint_state"
DEF_START_TIMEOUT = 900      # s from start_cmd to healthy before the start counts as failed
DEF_IDLE_STOP = 300          # s without a board run before an owned server is stopped
STOP_GRACE = 120             # s after the stop before SIGKILL to the process group
FAIL_COOLDOWN = 1800         # s an endpoint is skipped after a failed start
BUSY_RC, BUSY_COOLDOWN = 75, 300   # start_cmd exit 75 = "refused, device busy now": retry after 5 min, not 30
HEALTH_TTL = 5               # s a health result is reused within one runner
DEMAND_WINDOW = 30           # s: an endpoint a card asked for this recently is "wanted" and is not switched away from
DEF_SWITCH_AFTER = 1800      # s a card may wait on a wanted group-mate before the runner switches anyway (when idle)


def is_managed(e):
    return bool(e and e.get("start_cmd"))


def _pid_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _alive(pid, start):
    if not pid:
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
    return start is None or st == start


def probe(e, timeout=3):
    """(ok, detail). Healthy = HTTP 200 from health_url (default BASE/models) and, if health_match is set, the body
    matches that regex; with no health_match a JSON model list must be non-empty (a server that is up but has no model
    loaded answers an empty list)."""
    url = e.get("health_url") or e["base_url"].rstrip("/") + "/models"
    key = (os.environ.get(e["api_key_env"]) if e.get("api_key_env") else None) or "local"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1 << 20).decode("utf-8", "replace")
            code = r.status
    except Exception as ex:
        return False, f"down ({type(ex).__name__})"
    if code != 200:
        return False, f"HTTP {code}"
    if e.get("health_match"):
        return (True, "up") if re.search(e["health_match"], body) else (False, "up, but health_match not found")
    try:
        d = json.loads(body)
        if isinstance(d, dict) and isinstance(d.get("data"), list) and not d["data"]:
            return False, "up, no model loaded"
    except ValueError:
        pass
    return True, "up"


class Lifecycle:
    """Owned by the runner. gate() is asked before a run is started on an endpoint; tick() stops idle servers."""

    def __init__(self, conn, log):
        self.conn = conn
        self.log = log
        self._health = {}        # name -> (t, ok, detail)
        self.last_busy = {}      # name -> epoch of the last tick with a board run on it
        self._proc = {}          # name -> Popen of a server started by this process (reaped in tick)
        self.demand = {}         # name -> last time a card asked for it
        self.wanting = {}        # name -> since when cards have been waiting for it (cleared when one gets through)

    # -- state (re-read on every change: several helpers write it)
    def state(self):
        return cfg(self.conn, STATE_KEY) or {}

    def _put(self, name, rec, ev=None, data=None):
        st = self.state()
        if rec is None:
            st.pop(name, None)
        else:
            st[name] = rec
        with tx(self.conn):
            # straight to control: no "config" event per health change; transitions get their own events
            self.conn.execute("INSERT INTO control(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (STATE_KEY, json.dumps(st)))
            if ev:
                event(self.conn, None, "runner", ev, {"endpoint": name, **(data or {})})

    def endpoints(self):
        return cfg(self.conn, "endpoints") or {}

    def healthy(self, name, e, fresh=False):
        h = self._health.get(name)
        if h and not fresh and now() - h[0] < HEALTH_TTL:
            return h[1], h[2]
        ok, detail = probe(e)
        self._health[name] = (now(), ok, detail)
        return ok, detail

    @staticmethod
    def runs_on(children, name):
        return sum(1 for ch in children.values() if getattr(ch, "endpoint", None) == name)

    # -- the gate
    def gate(self, name, children, limits):
        r = self._gate(name, children, limits)
        if r is not None:
            self.demand[name] = now()      # only refused cards count as demand (one that got through doesn't keep it)
        return r

    def _gate(self, name, children, limits):
        """None = go. Otherwise (why, hold): hold=True means the card should wait for this endpoint (it is starting or
        switching) instead of falling through to the tier's next candidate."""
        eps = self.endpoints()
        e = eps.get(name) or {}
        self.wanting.setdefault(name, now())
        n = self.runs_on(children, name)
        if e.get("max_runs") and n >= int(e["max_runs"]):
            return f"endpoint {name} full ({n}/{e['max_runs']} runs)", False
        if not is_managed(e):
            self.wanting.pop(name, None)
            return None
        lim = limits.get(f"endpoint:{name}")
        if lim:
            return f"endpoint {name} {lim['kind']} until {store.fmt_ts(lim['until'])}: {lim.get('reason') or ''}".strip(), False
        mine = self.state().get(name)
        ok, detail = self.healthy(name, e)
        if ok:
            if mine and mine.get("status") == "stopping":
                return f"endpoint {name} is stopping", True
            if mine and mine.get("status") == "starting":
                load_s = round(now() - mine.get("started_at", now()))
                mine.update(status="up", up_at=now())
                self._put(name, mine, "endpoint_up", {"load_s": load_s})
                self.log(f"endpoint {name} up after {load_s}s")
            self.wanting.pop(name, None)
            return None
        if mine:
            alive = _alive(mine.get("pid"), mine.get("pid_start"))
            status = mine.get("status")
            if status == "stopping":
                return f"endpoint {name} is stopping", True
            age = now() - mine.get("started_at", now())
            if alive:
                limit = int(e.get("start_timeout") or DEF_START_TIMEOUT)
                if status == "starting" and age > limit:
                    self._fail(name, f"not healthy {age:.0f}s after start ({detail})", stop=True)
                    return f"endpoint {name} start timed out", False
                return f"endpoint {name} starting", True
            if status == "starting":
                rc = self._rc(name)
                why = (f"start_cmd refused with exit {rc} (busy), retry in {BUSY_COOLDOWN // 60} min" if rc == BUSY_RC else
                       f"server exited (rc {rc}) {age:.0f}s after start, before it was healthy ({detail})") + f"; log {mine.get('log')}"
                self._fail(name, why, cooldown=BUSY_COOLDOWN if rc == BUSY_RC else FAIL_COOLDOWN)
                return f"endpoint {name} start failed", False
            self._put(name, None, "endpoint_exited")     # was up, died: start it again below
        grp = e.get("group")
        if grp:
            st = self.state()
            for other, oe in eps.items():
                if other == name or oe.get("group") != grp:
                    continue
                om = st.get(other)
                o_alive = bool(om) and _alive(om.get("pid"), om.get("pid_start"))
                if not (o_alive or self.healthy(other, oe)[0]):
                    continue
                if not om:
                    return f"group {grp}: {other} is up and was not started by the board (left alone)", False
                busy = self.runs_on(children, other)
                if busy:
                    return f"group {grp}: {other} serves {busy} run(s)", False
                if om.get("status") == "starting":
                    return f"group {grp}: {other} is starting", False
                waited = now() - self.wanting.get(name, now())
                patience = int(e.get("switch_after") or DEF_SWITCH_AFTER)
                if om.get("status") != "stopping" and now() - self.demand.get(other, 0) < DEMAND_WINDOW and waited < patience:
                    return f"group {grp}: {other} is idle but other cards still want it", False
                if om.get("status") != "stopping":
                    self.stop(other, f"switching group {grp} to {name}")
                return f"group {grp}: switching from {other}", True
        return self.start(name, e)

    def start(self, name, e):
        logdir = store.HOME / "endpoints" / name
        logdir.mkdir(parents=True, exist_ok=True)
        logf = logdir / "server.log"
        env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
        try:
            with open(logf, "ab") as fo:
                fo.write(f"\n--- {store.fmt_ts(now())} start: {e['start_cmd']}\n".encode())
                fo.flush()
                proc = subprocess.Popen(e["start_cmd"], shell=True, stdout=fo, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True, env=env,
                                        cwd=e.get("start_dir") or str(store.HOME))
        except OSError as ex:
            self._fail(name, f"start_cmd failed: {ex}")
            return f"endpoint {name} start failed: {ex}", False
        self._proc[name] = proc
        self.last_busy[name] = now()
        self._put(name, {"status": "starting", "pid": proc.pid, "pid_start": _pid_start(proc.pid), "started_at": now(),
                         "log": str(logf)}, "endpoint_start", {"pid": proc.pid})
        self.log(f"endpoint {name}: started pid {proc.pid} ({e['start_cmd']})")
        return f"endpoint {name} starting", True

    def _rc(self, name):
        p = self._proc.get(name)
        return p.poll() if p is not None else None

    def _fail(self, name, why, stop=False, cooldown=FAIL_COOLDOWN):
        if stop:
            self.stop(name, why)
        else:
            self._put(name, None)
        with tx(self.conn):
            store.set_limit(self.conn, f"endpoint:{name}", "busy" if cooldown == BUSY_COOLDOWN else "start_failed",
                            now() + cooldown, why[:300])
            event(self.conn, None, "runner", "endpoint_failed", {"endpoint": name, "why": why[:300]})
        self.log(f"endpoint {name}: {why}")

    def stop(self, name, why):
        mine = self.state().get(name)
        if not mine:
            return False
        e = self.endpoints().get(name) or {}
        if e.get("stop_cmd"):
            try:
                subprocess.run(e["stop_cmd"], shell=True, timeout=120, capture_output=True,
                               env={k: v for k, v in os.environ.items() if not k.startswith("AB_")})
            except (OSError, subprocess.SubprocessError) as ex:
                self.log(f"endpoint {name}: stop_cmd failed: {ex}")
        if _alive(mine.get("pid"), mine.get("pid_start")):
            try:
                os.killpg(mine["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        mine.update(status="stopping", stop_at=now(), stop_why=why)
        self._put(name, mine, "endpoint_stop", {"why": why})
        self.log(f"endpoint {name}: stopping ({why})")
        return True

    def tick(self, children):
        """Stop owned servers that have had no board run for idle_stop seconds; finish stops; forget dead servers."""
        for proc in self._proc.values():
            proc.poll()
        eps = self.endpoints()
        for name, mine in self.state().items():
            e = eps.get(name)
            if mine.get("pid") and name not in self._proc and _pid_start(mine["pid"]) == mine.get("pid_start"):
                try:  # after a runner reload (execv, same pid) the server is still our child but has no Popen: reap it
                    os.waitpid(int(mine["pid"]), os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
            alive = _alive(mine.get("pid"), mine.get("pid_start"))
            status = mine.get("status")
            if status == "stopping":
                if not alive:
                    self._put(name, None, "endpoint_stopped")
                    self._proc.pop(name, None)
                    self.log(f"endpoint {name}: stopped")
                elif now() - mine.get("stop_at", now()) > STOP_GRACE:
                    try:
                        os.killpg(mine["pid"], signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                continue
            if not alive:
                if status == "up":               # a starting server that died is reported by gate() to its card
                    self._put(name, None, "endpoint_exited")
                    self.log(f"endpoint {name}: server exited on its own")
                elif now() - mine.get("started_at", now()) > 60:
                    rc = self._rc(name)
                    self._fail(name, f"server exited (rc {rc}) before it was healthy; log {mine.get('log')}",
                               cooldown=BUSY_COOLDOWN if rc == BUSY_RC else FAIL_COOLDOWN)
                continue
            if e is None:
                self.stop(name, "endpoint removed from the config")
                continue
            if self.runs_on(children, name):
                self.last_busy[name] = now()
                continue
            if status == "starting":
                if self.healthy(name, e)[0]:
                    mine.update(status="up", up_at=now())
                    self._put(name, mine, "endpoint_up", {"load_s": round(now() - mine.get("started_at", now()))})
                    self.last_busy[name] = now()
                elif now() - mine.get("started_at", now()) > int(e.get("start_timeout") or DEF_START_TIMEOUT):
                    self._fail(name, "start timed out", stop=True)
                continue
            idle_s = int(e["idle_stop"]) if e.get("idle_stop") is not None else DEF_IDLE_STOP
            since = max(self.last_busy.get(name, 0), mine.get("up_at") or mine.get("started_at") or 0)
            if idle_s >= 0 and now() - since >= idle_s:
                self.stop(name, f"idle {now() - since:.0f}s")

    def stop_all(self, why):
        for name, mine in self.state().items():
            if mine.get("status") != "stopping":
                self.stop(name, why)
