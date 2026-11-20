"""A fail whose reason is a grant refusal by ab itself is not retried by another non-admin worker: the card is held
for the overseer (label needs_admin=overseer, shown by `ab anomalies`). Throwaway AB_HOME, fake `claude`, no real harness."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env
from agentboard import store

FAKE_CLAUDE = r'''#!/bin/sh
echo "claude ran" >> "$FAKE_LOG"
err=$(ab config set some_key some_value 2>&1)
ab fail "$AB_TASK" "ab refused: $err"
'''


def test_grant_refusal_unit_regex():
    for why in ("PermissionError: claude#7 has no 'admin' on main:* (see `ab grants explain claude#7 admin`)",
                "no admin grant for this", "claude#1 has no 'write' on lab:main (see ...)"):
        assert store.GRANT_REFUSAL_RE.search(why), why
    assert not store.GRANT_REFUSAL_RE.search("tests failed: 3 errors")


def test_non_admin_config_set_is_one_run_no_retry_needs_admin(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_LOG": str(tmp_path / "fake.log")}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "true"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"), ("backoff", "1s")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku")
    j("runner", "pause")
    j("grants", "add", "claude#*", "--can", "read,post,create,claim,manage", "--grant-board", "*")  # workers: no admin
    (tmp_path / "w").mkdir()
    t = j("add", "set a config key", "--body", "run ab config set", "--workdir", str(tmp_path / "w"))["id"]
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=open(tmp_path / "runner.log", "ab"), stderr=subprocess.STDOUT, start_new_session=True)
    try:
        j("runner", "slots", "1")
        j("runner", "resume")
        for _ in range(80):
            if any(i["kind"] == "needs-admin" for i in j("anomalies", "--all-boards")):
                break
            time.sleep(0.5)
        time.sleep(4)  # a retry, if it were going to happen, starts within backoff (1s) + poll (1s)
        shown = j("show", t)
        assert (tmp_path / "fake.log").read_text().count("claude ran") == 1
        assert len(shown["runs"]) == 1
        items = j("anomalies", "--all-boards")
        assert any(i["kind"] == "needs-admin" and i["task_id"] == t for i in items), items
    finally:
        try:
            j("runner", "stop", "--now")
        except AssertionError:
            pass
        p.terminate()
