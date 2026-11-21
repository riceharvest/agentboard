"""The runner gives every worker shell AB_TASK/AB_ACTOR/AB_BOARD with this run's literal ids: through the child env, a
per-run BASH_ENV file (for shells that drop the env), and codex's shell_environment_policy."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env

sys.path.insert(0, ROOT)
from agentboard import harness as H  # noqa: E402


def test_codex_shell_env_args_carry_the_ids():
    args = H.Codex().shell_env_args({"AB_TASK": "t_x", "AB_ACTOR": "codex#3", "AB_BOARD": "main"})
    assert args[0] == "-c" and args[1].startswith("shell_environment_policy.set={")
    assert 'AB_ACTOR="codex#3"' in args[1] and 'AB_TASK="t_x"' in args[1] and 'AB_BOARD="main"' in args[1]


FAKE_CODEX = r'''#!/bin/sh
echo "$@" > "$FAKE_ARGV"
# a worker shell that drops the inherited env, keeping only what a shell would source: ab note + ab done with no ids
env -i PATH="$PATH" HOME="$HOME" AB_HOME="$AB_HOME" BASH_ENV="$BASH_ENV" bash -c '
  env | grep ^AB_ | sort > "$0"
  ab note "$AB_TASK" probe-note > "$1" 2>&1
  ab done "$AB_TASK" "worker done" >> "$1" 2>&1' "$FAKE_ENV" "$FAKE_OUT"
echo '{"type":"thread.started","thread_id":"th1"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
'''


def test_launch_exports_ids_and_note_done_work_without_force(abx, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text(FAKE_CODEX)
    (bindir / "codex").chmod(0o755)
    # `ab` on PATH for the worker shell
    (bindir / "ab").write_text(f"#!/bin/sh\nPYTHONPATH='{ROOT}' exec {sys.executable} -m agentboard \"$@\"\n")
    (bindir / "ab").chmod(0o755)
    cap = {k: str(tmp_path / k) for k in ("argv", "env", "out")}
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_ARGV": cap["argv"], "FAKE_ENV": cap["env"],
           "FAKE_OUT": cap["out"], "HOME": str(tmp_path)}
    wd = tmp_path / "wd"
    wd.mkdir()

    def j(*a):
        code, out, err = abx("--json", *a, env=env)
        assert code == 0, (a, out, err)
        return json.loads(out) if out.strip() else None

    for k, v in (("min_free_gib", "0"), ("poll_s", "1"), ("pacer", "false")):
        j("config", "set", k, v)
    tid = j("add", "env probe", "--harness", "codex", "--workdir", str(wd))["id"]
    j("runner", "pause")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        j("runner", "resume")
        for _ in range(60):
            if j("show", tid)["status"] == "done":
                break
            time.sleep(0.25)
        shown = j("show", tid)
        assert shown["status"] == "done", (shown["status"], open(cap["out"]).read() if os.path.exists(cap["out"]) else "")
        run = shown["runs"][0]["id"]
        got = dict(l.split("=", 1) for l in open(cap["env"]).read().split("\n") if l)
        assert got["AB_TASK"] == tid and got["AB_ACTOR"] == f"codex#{run}" and got["AB_BOARD"] == "main"
        assert "shell_environment_policy.set=" in open(cap["argv"]).read()
        out = open(cap["out"]).read()
        assert "error" not in out.lower() and "held by" not in out, out   # note + done ran with no --force
        code, thread, _ = abx("show", tid, env=env)
        assert "probe-note" in thread, thread
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
