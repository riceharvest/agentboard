"""Hermes gateway dies mid-turn: the client exits 1 with 'Gateway disconnected; turn outcome is unknown'.
The runner must not burn an attempt or mark the card failed; it requeues (bounded). Throwaway AB_HOME, fake `hermes`."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env
from agentboard import harness as H

FAKE = r'''#!/usr/bin/env python3
import os, signal, subprocess, sys, time
from pathlib import Path
argv = sys.argv[1:]
if argv[:2] == ["gateway", "stop"]:
    sys.exit(0)
if argv[:2] == ["gateway", "attach"]:
    print('{"state": "absent"}'); sys.exit(5)
gw = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(os.environ["FAKE_LOG"], "a").write("chat\n")
time.sleep(0.3)
os.kill(gw.pid, signal.SIGKILL)   # the gateway dies mid-turn
gw.wait()
sys.stdout.write("edited some files\n")
sys.stderr.write("Error: Gateway disconnected; turn outcome is unknown. Resume the printed session ID to check whether it "
                 "completed or was interrupted; do not resend the work.\nsession_id: local-abc123\n")
sys.exit(1)
'''


def test_gateway_death_requeues_without_burning_attempt(abx, tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "hermes").write_text(FAKE)
    (fake_bin / "hermes").chmod(0o755)
    src = tmp_path / "src_hermes"
    src.mkdir()
    (src / "config.yaml").write_text("model:\n  provider: openrouter\n")
    log = tmp_path / "calls.log"
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "HERMES_HOME": str(src), "HOME": str(tmp_path), "FAKE_LOG": str(log)}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None

    j("config", "set", "min_free_gib", "0", "--as", "tester", "--why", "test")
    j("config", "set", "poll_s", "1", "--as", "tester", "--why", "test")
    j("config", "set", "pacer", "false", "--as", "tester", "--why", "test")
    tid = j("add", "gw death", "--harness", "hermes/openrouter", "--model", "m", "--workdir", str(workdir), "--as", "tester", "--why", "test")["id"]
    j("runner", "pause", "--as", "tester", "--why", "test")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        j("runner", "resume", "--as", "tester", "--why", "test")
        for _ in range(80):
            shown = j("show", tid)
            if any(r["outcome"] == "gateway_lost" for r in shown["runs"]):
                break
            time.sleep(0.25)
        shown = j("show", tid)
        lost = [r for r in shown["runs"] if r["outcome"] == "gateway_lost"]
        assert lost, shown
        assert not any(r["outcome"] == "failed" for r in shown["runs"]), shown["runs"]
        assert shown["status"] != "dead" and not shown.get("attempts"), shown
    finally:
        j("runner", "stop", "--now", "--as", "tester", "--why", "test")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
