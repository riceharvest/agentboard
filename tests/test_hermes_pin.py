"""Provider pin for Hermes: `hermes/PROVIDER:MODEL`. Everything runs against a throwaway AB_HOME and a fake `hermes`."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env
from agentboard import harness as H

MODEL = "qwen/qwen4-max:free"

# Fake hermes: logs its argv; `--provider nous` fails the way a logged-out Nous Portal does, anything else succeeds
# and writes a session row (with a misleading billing_provider) to the state.db the usage harvester reads.
FAKE_HERMES = r'''#!/usr/bin/env python3
import json, os, sqlite3, sys, time
argv = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(argv) + "\n")
prov = argv[argv.index("--provider") + 1] if "--provider" in argv else None
if prov == "nous":
    sys.stderr.write("No access token found for Nous Portal login. Run `hermes model` to re-authenticate.\n")
    sys.exit(1)
home = os.environ["HERMES_HOME"]
os.makedirs(home, exist_ok=True)
db = sqlite3.connect(os.path.join(home, "state.db"))
db.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, model TEXT, billing_provider TEXT,"
           " input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,"
           " actual_cost_usd REAL, estimated_cost_usd REAL)")
db.execute("INSERT OR REPLACE INTO sessions VALUES('sess_" + str(os.getpid()) + "',?,?,?,?,?,?,0,0,0,NULL,NULL)",
           (time.time(), time.time(), argv[argv.index("-m") + 1], "unknown", 120, 45))
db.commit()
print("all done")
print("session_id: sess_" + str(os.getpid()))
'''


def test_parse_spec_survives_slash_and_colon():
    c = H.parse_spec(f"hermes/nous:{MODEL}")
    assert (c.harness, c.provider, c.model, c.effort, c.endpoint) == ("hermes", "nous", MODEL, None, None)
    c = H.parse_spec(f"hermes/openrouter:{MODEL}@high")
    assert (c.provider, c.model, c.effort) == ("openrouter", MODEL, "high")
    c = H.parse_spec("hermes/custom:")
    assert (c.provider, c.model) == ("custom", None)
    # unchanged forms
    assert H.parse_spec(f"hermes:{MODEL}").provider is None and H.parse_spec(f"hermes:{MODEL}").model == MODEL
    assert H.parse_spec("pi@rtx:spark").endpoint == "rtx"
    assert H.parse_spec(f"hermes/nous:{MODEL}").label() == f"hermes/nous:{MODEL}"


@pytest.mark.parametrize("spec,msg", [
    ("opencode/openrouter:foo", "no provider pin"),
    ("claude/anthropic:opus", "no provider pin"),
    ("hermes/bad name:m", "bad provider"),
    ("hermes/nous@ep:m", "can't be combined"),
])
def test_bad_provider_pins_are_rejected(spec, msg):
    with pytest.raises(ValueError, match=msg):
        H.parse_spec(spec)


def test_command_has_provider_and_exact_model():
    a = H.ADAPTERS["hermes"]
    cmd = a.command("do it", MODEL, provider="nous")
    assert cmd[:3] == ["hermes", "chat", "-q"]
    i = cmd.index("--provider")
    assert cmd[i:i + 4] == ["--provider", "nous", "-m", MODEL], cmd
    # Adapter.build with a parsed candidate (what the runner calls)
    argv, env = a.build("do it", H.parse_spec(f"hermes/nous:{MODEL}"))
    assert argv == cmd and env == {}
    # no pin: no --provider (Hermes' own default applies), as before
    assert "--provider" not in H.ADAPTERS["hermes"].build("x", H.parse_spec(f"hermes:{MODEL}"))[0]


def test_bucket_per_provider():
    a = H.ADAPTERS["hermes"]
    assert a.bucket(MODEL, provider="nous") == "hermes-nous"
    assert a.bucket(MODEL, provider="openrouter") == "hermes-openrouter"
    assert a.bucket(MODEL, provider="custom") == "local"  # same mapping the usage harvester applies


def test_classifies_nous_auth_error():
    err = "No access token found for Nous Portal login. Run `hermes model` to re-authenticate.\n"
    r = H.ADAPTERS["hermes"].parse(1, "", err)
    assert not r.ok and r.error_kind == "auth" and "Nous Portal" in r.error


def test_pinned_tiers_pass_check_and_cards_accept_the_pin(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "hermes").write_text(FAKE_HERMES)
    (fake / "hermes").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}"}
    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    j("tiers", "set", "low", f"hermes/nous:{MODEL}", f"hermes/openrouter:{MODEL}")
    j("tiers", "add", "low", f"hermes:{MODEL}")
    assert j("tiers", "check") == []
    j("tiers", "set", "low", "opencode/x:m", rc=1)  # not stored
    t = j("add", "pinned card", "--harness", "hermes/nous", "--model", MODEL)
    shown = j("show", t["id"])
    assert (shown["harness"], shown["model"]) == ("hermes/nous", MODEL)
    j("add", "bad pin", "--harness", "opencode/zzz", "--model", "m", rc=1)
    j("add", "bad pin 2", "--harness", "hermes/bad name", "--model", "m", rc=1)
    j("edit", t["id"], "--harness", "hermes/openrouter")
    assert j("show", t["id"])["harness"] == "hermes/openrouter"


def test_auth_cooldown_on_provider_bucket_and_usage_per_provider(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "hermes").write_text(FAKE_HERMES)
    (fake / "hermes").chmod(0o755)
    hh, log = tmp_path / "hermeshome", tmp_path / "argv.log"
    home = tmp_path / "fakehome"  # no real claude/pi/opencode session logs get harvested into the throwaway board
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HERMES_HOME": str(hh), "FAKE_LOG": str(log), "HOME": str(home)}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    j("config", "set", "min_free_gib", "0")
    j("config", "set", "poll_s", "1")
    j("config", "set", "pacer", "false")
    j("config", "set", "harvest_every_s", "1")
    bad = j("add", "nous card", "--harness", "hermes/nous", "--model", MODEL, "--workdir", str(tmp_path / "w1"))["id"]
    good = j("add", "openrouter card", "--harness", "hermes/openrouter", "--model", MODEL, "--workdir", str(tmp_path / "w2"))["id"]
    j("runner", "pause")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        j("runner", "resume")
        for _ in range(60):
            shown = j("show", good)
            if shown["status"] == "done" and j("show", bad)["runs"] and j("show", bad)["runs"][0]["outcome"]:
                break
            time.sleep(0.5)
        # the Nous run ended as an auth cooldown on its provider bucket, not a failure
        sb = j("show", bad)
        assert [r["outcome"] for r in sb["runs"]] == ["auth"], sb["runs"]
        assert sb["runs"][0]["bucket"] == "hermes-nous" and sb["runs"][0]["provider"] == "nous"
        assert sb["status"] == "open" and sb["attempts"] == 0
        lim = {r["scope"]: r for r in j("limits")}
        assert lim["bucket:hermes-nous"]["kind"] == "auth" and "Nous Portal" in lim["bucket:hermes-nous"]["reason"]
        assert "harness:hermes" not in lim  # other Hermes providers keep running
        # the OpenRouter card ran, and its usage is under hermes-openrouter although the session row says "unknown"
        sg = j("show", good)
        assert sg["status"] == "done" and sg["runs"][0]["bucket"] == "hermes-openrouter"
        rows = {r["bucket"]: r for r in j("usage", "--by", "bucket")}
        assert rows["hermes-openrouter"]["5h"]["calls"] == 1 and rows["hermes-openrouter"]["5h"]["output"] == 45
        assert set(rows) == {"hermes-openrouter", "hermes-nous"}, set(rows)  # not "local" / "hermes-unknown" from the session row
        assert rows["hermes-nous"]["5h"]["output"] == 0 and rows["hermes-nous"]["5h"]["runs"] == 1
        # the nous card does not keep retrying while the cooldown lasts
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        assert sum(1 for c in calls if c[c.index("--provider") + 1] == "nous") == 1
        for c in calls:
            i = c.index("--provider")
            assert c[i + 2:i + 4] == ["-m", MODEL]
    finally:
        j("runner", "stop", "--now")
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
