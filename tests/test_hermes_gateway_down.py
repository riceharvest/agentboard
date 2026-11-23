"""The gateway is not up when `hermes chat` launches: stdout carries "Hermes couldn't start the model connection:
Gateway did not answer ...", no turn ran, no tokens. That is infrastructure, not the card's fault: the run is
`outcome=gateway_down`, the card is requeued without an attempt, and the harness cools briefly so the bounded
retries do not burn in seconds. Throwaway AB_HOME, fake `hermes`.
"""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env

FAKE = r'''#!/usr/bin/env python3
import os, sys
argv = sys.argv[1:]
if argv[:2] == ["gateway", "stop"]:
    sys.exit(0)
if argv[:2] == ["gateway", "attach"]:
    print('{"state": "absent"}'); sys.exit(5)
open(os.environ["FAKE_LOG"], "a").write("chat\n")
sys.stdout.write("Hermes couldn't start the model connection: Gateway did not answer session.create. "
                 "Your message was not sent. Run `hermes doctor` to check the setup, or /model to pick a different provider.\n")
sys.exit(1)
'''


def test_gateway_not_up_requeues_without_burning_attempt(abx, tmp_path):
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
    tid = j("add", "gw down", "--harness", "hermes/openrouter", "--model", "m", "--workdir", str(workdir), "--as", "tester", "--why", "test")["id"]
    j("runner", "pause", "--as", "tester", "--why", "test")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        j("runner", "resume", "--as", "tester", "--why", "test")
        for _ in range(80):
            shown = j("show", tid)
            if any(r["outcome"] == "gateway_down" for r in shown["runs"]):
                break
            time.sleep(0.25)
        shown = j("show", tid)
        down = [r for r in shown["runs"] if r["outcome"] == "gateway_down"]
        assert down, shown
        assert "Gateway did not answer" in (down[0]["detail"] or ""), down[0]
        assert not any(r["outcome"] == "failed" for r in shown["runs"]), shown["runs"]
        assert shown["status"] != "dead" and not shown.get("attempts"), shown
        # the retries are spaced: the harness carries a short limit, so the third failure cannot happen in the same second
        assert j("config", "show").get("cooldown.gateway") == "2m"
    finally:
        j("runner", "stop", "--now", "--as", "tester", "--why", "test")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_hermes_parse_rc0_gateway_timeout_not_ok():
    from agentboard import harness as H
    out = ("Hermes couldn't start the model connection: Gateway connection timed out; "
           "no local fallback. Your message was not sent.")
    res = H.Hermes().parse(0, out, "")
    assert not res.ok
    assert "Gateway connection timed out" in (res.error or "")


def test_hermes_sanitize_config_strips_plugins():
    from agentboard import harness as H
    cfg = "model:\n  provider: openrouter\nplugins:\n  enabled:\n    - foo\n    - bar\ntoolsets:\n  - terminal\n"
    cleaned = H._sanitize_hermes_config(cfg)
    assert "plugins:" not in cleaned
    assert "foo" not in cleaned
    assert "provider: openrouter" in cleaned
    assert "toolsets:" in cleaned


