"""Hermes harness stop-on-kill and per-run HERMES_HOME isolation tests.
Everything runs against a throwaway AB_HOME and a mocked `hermes` binary."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, clean_env
from agentboard import harness as H


# Mock `hermes` that simulates a real gateway-backed `hermes chat` + `hermes gateway stop --yes`:
# - On `chat`: records its env & sanitized HERMES_HOME contents to CAPTURE_FILE, spawns a detached
#   background gateway process (in its own session) that writes gateway.pid + a child process,
#   writes a session row to $HERMES_HOME/state.db, and either exits (MODE=fast) or sleeps (MODE=hang).
# - On `gateway stop --yes`: reads $HERMES_HOME/gateway.pid and terminates that gateway if MODE!=ignore_stop.
FAKE_HERMES = r'''#!/usr/bin/env python3
import json, os, signal, sqlite3, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
hh = Path(os.environ["HERMES_HOME"])
hh.mkdir(parents=True, exist_ok=True)

if argv[:2] == ["gateway", "stop"]:
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"cmd": argv, "hermes_home": str(hh)}) + "\n")
    if os.environ.get("FAKE_IGNORE_STOP") == "1":
        sys.exit(1)
    pf = hh / "gateway.pid"
    if pf.is_file():
        doc = json.loads(pf.read_text())
        try:
            os.killpg(int(doc["pid"]), signal.SIGTERM)
        except OSError:
            pass
    sys.exit(0)

if argv[:2] == ["gateway", "attach"]:
    # The public attach step: url + single-use ticket subprotocols for the home's running gateway.
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"cmd": argv, "hermes_home": str(hh)}) + "\n")
    info = os.environ.get("FAKE_GW_INFO")
    if not info or not Path(info).is_file():
        print(json.dumps({"state": "absent", "reason_code": "no_gateway", "endpoint": None}))
        sys.exit(5)
    doc = json.loads(Path(info).read_text())
    print(json.dumps({"state": "ready", "url": doc["url"], "protocol": "hermes-gateway-v1",
                      "protocols": ["hermes-gateway-v1", "hermes-gateway-ticket.fake-ticket"],
                      "ticket": "fake-ticket",
                      "endpoint": {"profile_id": "default", "instance_id": "fake"}}))
    sys.exit(0)

mode = os.environ.get("FAKE_MODE", "fast")

# Record SIGINT if received (for non-isolated mode verification)
def on_sigint(signum, frame):
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"signal": "SIGINT", "hermes_home": str(hh)}) + "\n")
    sys.exit(130)

signal.signal(signal.SIGINT, on_sigint)

# Spawn a detached "ephemeral gateway" in its own session, plus a grandchild process in that session.
gw_code = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(120)\n"
)
grandchild_file = os.environ.get("FAKE_GRANDCHILD_FILE", str(hh / "grandchild.pid"))
gw = subprocess.Popen(
    [sys.executable, "-c", gw_code, grandchild_file],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(f"/proc/{gw.pid}/stat") as f:
    gw_start = int(f.read().rsplit(")", 1)[1].split()[19])

(hh / "gateway.pid").write_text(json.dumps({
    "pid": gw.pid,
    "kind": "hermes-gateway",
    "start_time": gw_start,
    "hermes_home": str(hh),
}))
if os.environ.get("FAKE_GW_PID_FILE"):
    Path(os.environ["FAKE_GW_PID_FILE"]).write_text(json.dumps({"pid": gw.pid, "start": gw_start}))

# Write a session row to $HERMES_HOME/state.db (like the gateway does at session.create:
# cwd = the client's workdir, ended_at NULL while the turn runs)
sid = os.environ.get("FAKE_SESSION_ID") or f"sess_{os.getpid()}"
db = sqlite3.connect(str(hh / "state.db"))
db.execute(
    "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, model TEXT, "
    "billing_provider TEXT, input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, "
    "reasoning_tokens INT, actual_cost_usd REAL, estimated_cost_usd REAL, cwd TEXT)"
)
db.execute(
    "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,10,5,3,0.012,0.010,?)",
    (sid, time.time(), None, "test-model", "openrouter", 200, 80, os.getcwd()),
)
db.commit()
db.close()

# Capture observed environment and HERMES_HOME files
if os.environ.get("FAKE_CAPTURE"):
    cap = {
        "hermes_home": str(hh),
        "ab_home": os.environ.get("AB_HOME"),
        "ab_actor": os.environ.get("AB_ACTOR"),
        "ab_board": os.environ.get("AB_BOARD"),
        "ab_run": os.environ.get("AB_RUN"),
        "ab_task": os.environ.get("AB_TASK"),
        "env_telegram": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "env_discord": os.environ.get("DISCORD_BOT_TOKEN"),
        "env_tui_gw": os.environ.get("HERMES_TUI_GATEWAY_URL"),
        "files": sorted(p.name for p in hh.iterdir()),
        "dotenv": (hh / ".env").read_text() if (hh / ".env").is_file() else "",
        "config": (hh / "config.yaml").read_text() if (hh / "config.yaml").is_file() else "",
        "auth": (hh / "auth.json").read_text() if (hh / "auth.json").is_file() else "",
        "cwd": os.getcwd(),
        "session_id": sid,
        "brief": argv[argv.index("-q") + 1] if "-q" in argv else "",
    }
    Path(os.environ["FAKE_CAPTURE"]).write_text(json.dumps(cap))

if os.environ.get("FAKE_NO_SESSION_LINE") != "1":
    sys.stderr.write(f"Session: {sid}\n")
    sys.stderr.flush()

if mode == "hang":
    # Wait until killed; meanwhile the detached gateway is also alive.
    time.sleep(120)

print("all done")
print(f"session_id: {sid}")
'''


def _install_fake_hermes(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    p = fake_bin / "hermes"
    p.write_text(FAKE_HERMES)
    p.chmod(0o755)
    return fake_bin


def _make_source_hermes_home(tmp_path):
    src = tmp_path / "src_hermes"
    src.mkdir()
    (src / "auth.json").write_text('{"nous": {"access_token": "tok-123"}}')
    (src / "auth").mkdir()
    (src / "auth" / "pool.json").write_text('{"ok": true}')
    (src / ".env").write_text(
        "# provider keys\n"
        "OPENROUTER_API_KEY=sk-or-test\n"
        "OPENAI_API_KEY=sk-openai-test\n"
        "HERMES_API_TIMEOUT=120\n"
        "# messaging tokens that must be stripped\n"
        "TELEGRAM_BOT_TOKEN=123:tg-secret\n"
        "TELEGRAM_ALLOWED_USERS=111,222\n"
        "export TELEGRAM_HOME_CHANNEL=999\n"
        "DISCORD_BOT_TOKEN=disc-secret\n"
        "SLACK_BOT_TOKEN=xoxb-secret\n"
        "SLACK_APP_TOKEN=xapp-secret\n"
        "WHATSAPP_ENABLED=true\n"
        "MATRIX_ACCESS_TOKEN=mat-secret\n"
        "HERMES_TUI_GATEWAY_URL=ws://127.0.0.1:9999\n"
        "SUDO_PASSWORD=should-not-leak\n"
        "CUSTOM_BOT_TOKEN=also-stripped\n"
    )
    (src / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: test-model\n"
        "platforms:\n"
        "  telegram:\n"
        "    enabled: true\n"
        "discord:\n"
        "  token: nope\n"
        "gateway:\n"
        "  port: 8787\n"
        "kanban:\n"
        "  enabled: true\n"
        "agent:\n"
        "  max_turns: 40\n"
    )
    (src / "gateway.lock").write_text("locked")
    (src / "state.db").write_text("not-copied")
    return src


def test_prepare_hermes_home_strips_tokens_and_gateway_state(tmp_path):
    src = _make_source_hermes_home(tmp_path)
    dst = H.prepare_hermes_home(tmp_path / "iso", source=src)

    assert (dst / "auth.json").read_text() == '{"nous": {"access_token": "tok-123"}}'
    assert (dst / "auth" / "pool.json").read_text() == '{"ok": true}'
    assert not (dst / "gateway.lock").exists()
    assert not (dst / "state.db").exists()

    env_txt = (dst / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-test" in env_txt
    assert "OPENAI_API_KEY=sk-openai-test" in env_txt
    assert "HERMES_API_TIMEOUT=120" in env_txt
    for forbidden in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL",
        "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
        "WHATSAPP_ENABLED", "MATRIX_ACCESS_TOKEN", "HERMES_TUI_GATEWAY_URL",
        "SUDO_PASSWORD", "CUSTOM_BOT_TOKEN",
    ):
        assert forbidden not in env_txt, forbidden

    cfg_txt = (dst / "config.yaml").read_text()
    assert "provider: openrouter" in cfg_txt
    assert "max_turns: 40" in cfg_txt
    for forbidden_section in ("platforms:", "discord:", "gateway:", "kanban:"):
        assert forbidden_section not in cfg_txt, forbidden_section


def test_stop_hermes_gateway_refuses_shared_home_and_mismatched_pid(tmp_path, monkeypatch):
    shared = tmp_path / "shared_hermes"
    shared.mkdir()
    monkeypatch.setattr(H, "HERMES_HOME", shared)

    # Spawn a dummy process to represent a live user gateway
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        st = int(H._pid_start(proc.pid))
        (shared / "gateway.pid").write_text(json.dumps({
            "pid": proc.pid, "start_time": st, "hermes_home": str(shared),
        }))
        # Refuses to stop shared HERMES_HOME
        assert H.stop_hermes_gateway(shared, force=False) is False
        assert H._pid_alive(proc.pid, st)

        # Isolated dir pointing at shared home's hermes_home in gateway.pid is also refused
        iso = tmp_path / "iso"
        iso.mkdir()
        (iso / "gateway.pid").write_text(json.dumps({
            "pid": proc.pid, "start_time": st, "hermes_home": str(shared),
        }))
        assert H.read_hermes_gateway_pid(iso) == (None, None)
        assert H.stop_hermes_gateway(iso, force=True) is False
        assert H._pid_alive(proc.pid, st)

        # Mismatched start_time (PID reuse) is refused
        (iso / "gateway.pid").write_text(json.dumps({
            "pid": proc.pid, "start_time": st + 999999, "hermes_home": str(iso),
        }))
        assert H.stop_hermes_gateway(iso, force=True) is False
        assert H._pid_alive(proc.pid, st)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_isolated_run_propagates_ab_env_strips_tokens_harvests_usage_and_cleans_up(abx, tmp_path):
    fake_bin = _install_fake_hermes(tmp_path)
    src_hh = _make_source_hermes_home(tmp_path)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    capture = tmp_path / "capture.json"
    gw_pid_file = tmp_path / "gw.pid"
    gc_pid_file = tmp_path / "gc.pid"
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_HOME": str(src_hh),
        "HOME": str(fake_home),
        "FAKE_MODE": "fast",
        "FAKE_CAPTURE": str(capture),
        "FAKE_GW_PID_FILE": str(gw_pid_file),
        "FAKE_GRANDCHILD_FILE": str(gc_pid_file),
        "TELEGRAM_BOT_TOKEN": "leaked-from-parent-env",
        "DISCORD_BOT_TOKEN": "leaked-discord-env",
        "HERMES_TUI_GATEWAY_URL": "ws://127.0.0.1:9999",
    }

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    tid = j("add", "hermes isolated ok", "--harness", "hermes/openrouter", "--model", "test-model", "--workdir", str(workdir))["id"]
    j("runner", "pause")
    p = subprocess.Popen(
        [sys.executable, "-m", "agentboard", "runner", "run"],
        cwd=ROOT, env=clean_env(abx.home, **env),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        j("runner", "resume")
        for _ in range(40):
            shown = j("show", tid)
            if shown["status"] == "done":
                break
            time.sleep(0.25)
        shown = j("show", tid)
        assert shown["status"] == "done", shown
        run = shown["runs"][0]
        assert run["input"] == 200 and run["output"] == 80
        assert abs((run["cost_usd"] or 0) - 0.012) < 1e-6

        # Verify captured worker env and sanitized HERMES_HOME
        cap = json.loads(capture.read_text())
        assert cap["hermes_home"] != str(src_hh)
        assert cap["hermes_home"] == str(src_hh / "profiles" / f"ab-run-{run['id']}")   # named-profile shape (hermes#191)
        assert cap["ab_home"] == str(abx.home)
        assert cap["ab_actor"] == f"hermes#{run['id']}"
        assert cap["ab_board"] == "main"
        assert cap["ab_run"] == str(run["id"])
        assert cap["ab_task"] == tid
        assert cap["cwd"] == str(workdir)
        assert cap["env_telegram"] is None
        assert cap["env_discord"] is None
        assert cap["env_tui_gw"] is None
        assert "TELEGRAM_BOT_TOKEN" not in cap["dotenv"]
        assert "DISCORD_BOT_TOKEN" not in cap["dotenv"]
        assert "OPENROUTER_API_KEY=sk-or-test" in cap["dotenv"]
        assert "platforms:" not in cap["config"]
        assert "provider: openrouter" in cap["config"]
        assert "gateway.lock" not in cap["files"]

        # Verify temp HERMES_HOME was cleaned up
        assert not Path(cap["hermes_home"]).exists()

        # Verify detached ephemeral gateway and its grandchild were stopped on exit
        gw_info = json.loads(gw_pid_file.read_text())
        assert not H._pid_alive(gw_info["pid"], gw_info["start"])
        if gc_pid_file.exists():
            gc_pid = int(gc_pid_file.read_text().strip())
            assert not H._pid_alive(gc_pid)

        # Verify usage table recorded the isolated run
        rows = {r["bucket"]: r for r in j("usage", "--by", "bucket")}
        assert rows["hermes-openrouter"]["5h"]["calls"] == 1
        assert rows["hermes-openrouter"]["5h"]["output"] == 80
        assert abs(rows["hermes-openrouter"]["5h"]["cost_usd"] - 0.012) < 1e-6
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_kill_hermes_stops_detached_gateway_and_cleans_up(abx, tmp_path):
    fake_bin = _install_fake_hermes(tmp_path)
    src_hh = _make_source_hermes_home(tmp_path)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    capture = tmp_path / "capture.json"
    gw_pid_file = tmp_path / "gw.pid"
    gc_pid_file = tmp_path / "gc.pid"
    fake_log = tmp_path / "fake.log"
    workdir = tmp_path / "work"
    workdir.mkdir()

    # Also put a live dummy gateway in the shared src_hh to verify it is NEVER killed.
    shared_gw = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    shared_gw_start = int(H._pid_start(shared_gw.pid))
    (src_hh / "gateway.pid").write_text(json.dumps({
        "pid": shared_gw.pid, "start_time": shared_gw_start, "hermes_home": str(src_hh),
    }))

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_HOME": str(src_hh),
        "HOME": str(fake_home),
        "FAKE_MODE": "hang",
        "FAKE_IGNORE_STOP": "1",  # also exercise fallback SIGTERM/SIGKILL on gateway.pid pgid
        "FAKE_CAPTURE": str(capture),
        "FAKE_GW_PID_FILE": str(gw_pid_file),
        "FAKE_GRANDCHILD_FILE": str(gc_pid_file),
        "FAKE_LOG": str(fake_log),
    }

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    tid = j("add", "hermes hang kill", "--harness", "hermes/openrouter", "--model", "test-model", "--workdir", str(workdir))["id"]
    j("runner", "pause")
    p = subprocess.Popen(
        [sys.executable, "-m", "agentboard", "runner", "run"],
        cwd=ROOT, env=clean_env(abx.home, **env),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        j("runner", "resume")
        # Wait until fake hermes and its detached gateway + grandchild are up
        for _ in range(40):
            if gw_pid_file.exists() and gc_pid_file.exists():
                break
            time.sleep(0.25)
        assert gw_pid_file.exists() and gc_pid_file.exists()
        gw_info = json.loads(gw_pid_file.read_text())
        gc_pid = int(gc_pid_file.read_text().strip())
        assert H._pid_alive(gw_info["pid"], gw_info["start"])
        assert H._pid_alive(gc_pid)

        run_id = j("show", tid)["runs"][0]["id"]
        cap = json.loads(capture.read_text())
        j("runner", "pause")  # prevent immediate re-claim after kill so we inspect run 1 cleanly
        j("kill", str(run_id))

        # Wait for the runner to finalize the killed run
        for _ in range(40):
            shown = j("show", tid)
            if shown["runs"][0]["outcome"] == "killed":
                break
            time.sleep(0.25)

        shown = j("show", tid)
        run = shown["runs"][0]
        assert run["outcome"] == "killed", run
        assert shown["status"] == "open" and shown["attempts"] == 0
        assert run["output"] == 80

        # Detached gateway AND its grandchild must be dead
        assert not H._pid_alive(gw_info["pid"], gw_info["start"])
        assert not H._pid_alive(gc_pid)

        # Shared user gateway must still be untouched and alive
        assert H._pid_alive(shared_gw.pid, shared_gw_start)

        # `hermes gateway stop --yes` was invoked against the isolated home (not src_hh)
        log_entries = [json.loads(l) for l in fake_log.read_text().splitlines() if l.strip()]
        assert any(e.get("cmd", [])[:3] == ["gateway", "stop", "--yes"] and e["hermes_home"] != str(src_hh) for e in log_entries)

        # Per-run HERMES_HOME was cleaned up
        assert not Path(cap["hermes_home"]).exists()
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_timeout_hermes_stops_detached_gateway(abx, tmp_path):
    fake_bin = _install_fake_hermes(tmp_path)
    src_hh = _make_source_hermes_home(tmp_path)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    gw_pid_file = tmp_path / "gw.pid"
    capture = tmp_path / "capture.json"
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_HOME": str(src_hh),
        "HOME": str(fake_home),
        "FAKE_MODE": "hang",
        "FAKE_CAPTURE": str(capture),
        "FAKE_GW_PID_FILE": str(gw_pid_file),
    }

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    tid = j("add", "hermes timeout", "--harness", "hermes/openrouter", "--model", "test-model",
            "--timeout", "2s", "--max-attempts", "1", "--workdir", str(workdir))["id"]
    j("runner", "pause")
    p = subprocess.Popen(
        [sys.executable, "-m", "agentboard", "runner", "run"],
        cwd=ROOT, env=clean_env(abx.home, **env),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        j("runner", "resume")
        for _ in range(40):
            shown = j("show", tid)
            if shown["status"] == "dead":
                break
            time.sleep(0.25)
        shown = j("show", tid)
        assert shown["status"] == "dead" and shown["runs"][0]["outcome"] == "timeout", shown
        gw_info = json.loads(gw_pid_file.read_text())
        assert not H._pid_alive(gw_info["pid"], gw_info["start"])
        cap = json.loads(capture.read_text())
        assert not Path(cap["hermes_home"]).exists()
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_non_isolated_hermes_sends_sigint_and_preserves_shared_gateway(abx, tmp_path):
    fake_bin = _install_fake_hermes(tmp_path)
    src_hh = _make_source_hermes_home(tmp_path)
    (src_hh / "state.db").unlink()  # let fake hermes create a real sqlite db in shared home
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    capture = tmp_path / "capture.json"
    gw_pid_file = tmp_path / "gw.pid"
    fake_log = tmp_path / "fake.log"
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_HOME": str(src_hh),
        "HOME": str(fake_home),
        "FAKE_MODE": "hang",
        "FAKE_CAPTURE": str(capture),
        "FAKE_GW_PID_FILE": str(gw_pid_file),
        "FAKE_LOG": str(fake_log),
    }

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    j("config", "set", "hermes_isolate", "false")
    tid = j("add", "hermes shared kill", "--harness", "hermes/openrouter", "--model", "test-model", "--workdir", str(workdir))["id"]
    j("runner", "pause")
    p = subprocess.Popen(
        [sys.executable, "-m", "agentboard", "runner", "run"],
        cwd=ROOT, env=clean_env(abx.home, **env),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    gw_info = None
    try:
        j("runner", "resume")
        for _ in range(40):
            if capture.exists() and gw_pid_file.exists():
                break
            time.sleep(0.25)
        assert capture.exists() and gw_pid_file.exists()
        gw_info = json.loads(gw_pid_file.read_text())
        run_id = j("show", tid)["runs"][0]["id"]
        # the shared gateway's tools do not see the run's env, so the brief has to spell the AB_* values out
        brief = json.loads(capture.read_text())["brief"]
        assert brief.startswith("ENVIRONMENT NOTE")
        assert f"AB_HOME={abx.home}" in brief and f"AB_TASK={tid}" in brief and f"AB_RUN={run_id}" in brief, brief[:400]
        j("runner", "pause")
        j("kill", str(run_id))
        for _ in range(40):
            shown = j("show", tid)
            if shown["runs"][0]["outcome"] == "killed":
                break
            time.sleep(0.25)
        assert j("show", tid)["runs"][0]["outcome"] == "killed"
        # SIGINT was delivered to hermes chat; shared gateway was NOT stopped
        entries = [json.loads(l) for l in fake_log.read_text().splitlines() if l.strip()]
        assert any(e.get("signal") == "SIGINT" for e in entries), entries
        assert not any(e.get("cmd", [])[:2] == ["gateway", "stop"] for e in entries), entries
        assert H._pid_alive(gw_info["pid"], gw_info["start"])
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
        if gw_info:
            try:
                os.killpg(gw_info["pid"], signal.SIGKILL)
            except OSError:
                pass


def test_export_hermes_session_keeps_the_transcript_with_timestamps(tmp_path):
    home = tmp_path / "hh"
    home.mkdir()
    db = sqlite3.connect(str(home / "state.db"))
    db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL)")
    db.executemany("INSERT INTO messages(session_id, role, content, tool_calls, timestamp) VALUES('s',?,?,?,?)",
                   [("user", "hi", None, 1.0), ("assistant", None, '[{"name":"terminal"}]', 2.0), ("tool", "ok", None, 3.0)])
    db.commit()
    db.close()
    dest = tmp_path / "out.jsonl"
    assert H.export_hermes_session(home, dest) == 3
    rows = [json.loads(l) for l in dest.read_text().splitlines()]
    assert [r["role"] for r in rows] == ["user", "assistant", "tool"] and rows[1]["timestamp"] == 2.0
    assert H.export_hermes_session(tmp_path / "missing", tmp_path / "none.jsonl") == 0


# A fake SHARED Hermes gateway speaking the documented external-client protocol: WebSocket JSON-RPC
# (hermes-gateway-v1 + a ticket subprotocol), one running session that keeps "working" (appending to
# FAKE_WORK_LOG) until session.interrupt arrives with the current execution_generation. Like the real
# gateway it survives client disconnects: detaching a viewer never stops accepted work.
FAKE_GATEWAY = r'''
import base64, hashlib, json, os, socket, threading, time
from pathlib import Path

GEN = 7
rpc_log = Path(os.environ["FAKE_RPC_LOG"])
work_log = Path(os.environ["FAKE_WORK_LOG"])
info_file = Path(os.environ["FAKE_GW_INFO"])
state = {"running": True}

def worker():
    while state["running"]:
        with open(work_log, "a") as f:
            f.write(f"work {time.time()}\n")
        time.sleep(0.2)

threading.Thread(target=worker, daemon=True).start()

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(4)
port = srv.getsockname()[1]
info_file.write_text(json.dumps({"url": f"ws://127.0.0.1:{port}/api/ws", "pid": os.getpid(), "port": port}))

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def recv_exact(conn, buf, n):
    while len(buf) < n:
        chunk = conn.recv(65536)
        if not chunk:
            raise ConnectionError("client gone")
        buf += chunk
    return buf[:n], buf[n:]

def read_frame(conn, buf):
    head, buf = recv_exact(conn, buf, 2)
    b1, b2 = head
    fin, op = b1 & 0x80, b1 & 0x0F
    masked, ln = b2 & 0x80, b2 & 0x7F
    if ln == 126:
        ext, buf = recv_exact(conn, buf, 2); ln = int.from_bytes(ext, "big")
    elif ln == 127:
        ext, buf = recv_exact(conn, buf, 8); ln = int.from_bytes(ext, "big")
    mask = b""
    if masked:
        mask, buf = recv_exact(conn, buf, 4)
    data, buf = recv_exact(conn, buf, ln)
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return fin, op, data, buf

def send_frame(conn, op, payload):
    header = bytes([0x80 | op])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + n.to_bytes(2, "big")
    else:
        header += bytes([127]) + n.to_bytes(8, "big")
    conn.sendall(header + payload)

def handle(conn):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    lines = head.decode(errors="replace").split("\r\n")
    hdrs = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
    protos = [p.strip() for p in hdrs.get("sec-websocket-protocol", "").split(",") if p.strip()]
    with open(rpc_log, "a") as f:
        f.write(json.dumps({"handshake": protos}) + "\n")
    accept = base64.b64encode(hashlib.sha1((hdrs.get("sec-websocket-key", "") + GUID).encode()).digest()).decode()
    conn.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: {protos[0] if protos else ''}\r\n\r\n").encode())
    while True:
        fin, op, data, buf = read_frame(conn, buf)
        if op == 8:
            send_frame(conn, 8, data[:2])
            return
        if op == 9:
            send_frame(conn, 10, data)
            continue
        if op != 1:
            continue
        doc = json.loads(data)
        method, params, rid = doc.get("method"), doc.get("params") or {}, doc.get("id")
        with open(rpc_log, "a") as f:
            f.write(json.dumps({"method": method, "params": params}) + "\n")
        err, result = None, {}
        if method == "session.resume":
            result = {"session_id": params.get("session_id"), "stored_session_id": params.get("session_id"),
                      "running": state["running"], "execution_generation": GEN, "subscription_id": "sub-1",
                      "revision": 1, "messages": []}
        elif method == "session.interrupt":
            if params.get("execution_generation") != GEN:
                err = {"code": -32000, "message": "stale_generation"}
            else:
                state["running"] = False
                result = {"execution_state": "idle", "execution_generation": GEN}
        elif method == "session.detach":
            result = {"ok": True}
        else:
            err = {"code": -32601, "message": "unknown_method"}
        answer = {"jsonrpc": "2.0", "id": rid}
        if err:
            answer["error"] = err
        else:
            answer["result"] = result
        send_frame(conn, 1, json.dumps(answer).encode())

while True:
    conn, _ = srv.accept()
    try:
        handle(conn)
    except (ConnectionError, OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
'''


def _start_fake_gateway(tmp_path, env):
    """Start FAKE_GATEWAY with the FAKE_* env; wait until it published its url. Returns (proc, info)."""
    gw = subprocess.Popen([sys.executable, "-c", FAKE_GATEWAY], env={**os.environ, **env},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    info_file = Path(env["FAKE_GW_INFO"])
    for _ in range(80):
        if info_file.exists():
            try:
                return gw, json.loads(info_file.read_text())
            except ValueError:
                pass
        if gw.poll() is not None:
            raise AssertionError("fake gateway exited early")
        time.sleep(0.1)
    gw.kill()
    raise AssertionError("fake gateway did not start")


def _gateway_rpcs(rpc_log):
    return [json.loads(l) for l in Path(rpc_log).read_text().splitlines() if l.strip()]


@pytest.mark.parametrize("session_line", [True, False], ids=["session-line", "gateway-store"])
def test_non_isolated_kill_interrupts_gateway_session(abx, tmp_path, session_line):
    """ab kill on a shared-gateway hermes run must stop the SESSION (session.interrupt over the
    gateway RPC), not only signal the client: a detached or killed client can leave accepted work
    running at the gateway. The shared gateway process itself must survive. Covers both session-id
    sources: the early 'Session:' stderr line, and (classic -q prints none) the gateway's session
    row matched by the run's workdir."""
    fake_bin = _install_fake_hermes(tmp_path)
    src_hh = _make_source_hermes_home(tmp_path)
    (src_hh / "state.db").unlink()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    capture = tmp_path / "capture.json"
    gw_info_file = tmp_path / "gw-info.json"
    rpc_log = tmp_path / "rpc.jsonl"
    work_log = tmp_path / "work.log"
    fake_log = tmp_path / "fake.log"
    runner_log = tmp_path / "runner.log"
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_HOME": str(src_hh),
        "HOME": str(fake_home),
        "FAKE_MODE": "hang",
        "FAKE_CAPTURE": str(capture),
        "FAKE_GW_INFO": str(gw_info_file),
        "FAKE_RPC_LOG": str(rpc_log),
        "FAKE_WORK_LOG": str(work_log),
        "FAKE_LOG": str(fake_log),
    }
    if session_line:
        env["FAKE_SESSION_ID"] = "sess_shared"
    else:
        env["FAKE_NO_SESSION_LINE"] = "1"
    gw, gw_info = _start_fake_gateway(tmp_path, env)

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    j("config", "set", "hermes_isolate", "false")
    tid = j("add", "hermes shared session kill", "--harness", "hermes/openrouter", "--model", "test-model",
            "--workdir", str(workdir))["id"]
    j("runner", "pause")
    with open(runner_log, "w") as log_f:
        p = subprocess.Popen(
            [sys.executable, "-m", "agentboard", "runner", "run"],
            cwd=ROOT, env=clean_env(abx.home, **env),
            stdout=subprocess.DEVNULL, stderr=log_f, start_new_session=True,
        )
    try:
        j("runner", "resume")
        for _ in range(40):
            if capture.exists():
                break
            time.sleep(0.25)
        assert capture.exists()
        expected_sid = json.loads(capture.read_text())["session_id"]
        run_id = j("show", tid)["runs"][0]["id"]
        assert work_log.exists() and work_log.stat().st_size > 0  # the session is working
        j("runner", "pause")
        j("kill", str(run_id))
        for _ in range(80):
            shown = j("show", tid)
            if shown["runs"][0]["outcome"] == "killed":
                break
            time.sleep(0.25)
        assert j("show", tid)["runs"][0]["outcome"] == "killed"
        # The gateway received the session stop for THIS run's session, with the live generation.
        rpcs = _gateway_rpcs(rpc_log)
        interrupts = [r for r in rpcs if r.get("method") == "session.interrupt"]
        assert interrupts, rpcs
        assert interrupts[0]["params"]["session_id"] == expected_sid, interrupts
        assert interrupts[0]["params"]["execution_generation"] == 7, interrupts
        assert any(r.get("method") == "session.resume" for r in rpcs), rpcs
        # the attach ticket subprotocols were used (the documented external-client dial)
        assert any("hermes-gateway-ticket.fake-ticket" in r.get("handshake", []) for r in rpcs), rpcs
        # The session's work stopped: the orphan keeps appending until interrupted.
        size = work_log.stat().st_size
        time.sleep(0.7)
        assert work_log.stat().st_size == size, "gateway session kept working after the kill"
        # The shared gateway process itself was NOT stopped.
        assert H._pid_alive(gw.pid, None)
        entries = [json.loads(l) for l in fake_log.read_text().splitlines() if l.strip()]
        assert not any(e.get("cmd", [])[:2] == ["gateway", "stop"] for e in entries), entries
        # The stop is recorded in the run log.
        log_text = runner_log.read_text()
        assert expected_sid in log_text and "interrupt" in log_text.lower(), log_text[-2000:]
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
        gw.kill()
        gw.wait()


@pytest.mark.parametrize("block_websockets", [False, True], ids=["websockets", "stdlib-fallback"])
def test_interrupt_hermes_session_stops_only_that_session(tmp_path, monkeypatch, block_websockets):
    """interrupt_hermes_session speaks the gateway RPC on both transports (websockets when installed,
    the stdlib fallback otherwise) and never touches the gateway process."""
    fake_bin = _install_fake_hermes(tmp_path)
    rpc_log = tmp_path / "rpc.jsonl"
    work_log = tmp_path / "work.log"
    env = {
        "FAKE_SESSION_ID": "sess_shared",
        "FAKE_GW_INFO": str(tmp_path / "gw-info.json"),
        "FAKE_RPC_LOG": str(rpc_log),
        "FAKE_WORK_LOG": str(work_log),
    }
    gw, gw_info = _start_fake_gateway(tmp_path, env)
    try:
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        if block_websockets:
            monkeypatch.setitem(sys.modules, "websockets.sync.client", None)
        ok, detail = H.interrupt_hermes_session("sess_shared")
        assert ok, detail
        assert "interrupted" in detail, detail
        rpcs = _gateway_rpcs(rpc_log)
        assert any(r.get("method") == "session.interrupt" and r["params"]["execution_generation"] == 7
                   for r in rpcs), rpcs
        size = work_log.stat().st_size
        time.sleep(0.7)
        assert work_log.stat().st_size == size
        assert H._pid_alive(gw.pid, None)
        # an already-idle session is a no-op success, not an error
        ok, detail = H.interrupt_hermes_session("sess_shared")
        assert ok and "not running" in detail, detail
    finally:
        gw.kill()
        gw.wait()
