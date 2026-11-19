"""The `ab tui` launcher, the usage history the TUI draws sparklines from, and the fixture the Rust render tests use.
Every test runs against a throwaway AB_HOME."""
import json
import os
import subprocess
import sys

import pytest

jsonschema = pytest.importorskip("jsonschema")
from conftest import ROOT, clean_env  # noqa: E402

SCHEMA = os.path.join(ROOT, "agentboard", "snapshot.schema.json")
FIXTURE = os.path.join(ROOT, "tui", "tests", "fixtures", "snapshot.json")
MAKE = os.path.join(ROOT, "tui", "tests", "make_fixture.py")


def validator():
    return jsonschema.Draft7Validator(json.load(open(SCHEMA)))


def test_committed_fixture_matches_the_schema():
    snap = json.load(open(FIXTURE))
    validator().validate(snap)
    assert snap["runner"]["children"] and snap["usage_hourly"] and snap["usage_split"] and snap["events"]
    assert "/home/" not in open(FIXTURE).read().replace("/home/user/ab-home", ""), "the fixture must not carry local paths"


def test_fixture_generator_makes_a_valid_snapshot(tmp_path):
    out = tmp_path / "snap.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    p = subprocess.run([sys.executable, MAKE, str(out)], capture_output=True, text=True, env=env, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    validator().validate(json.load(open(out)))


def test_snapshot_usage_history(abx):
    abx("add", "x")  # creates the home and the db
    import sqlite3
    import time
    db = sqlite3.connect(next(abx.home.glob("*.db")))
    now = time.time()
    for i in range(30):
        db.execute("INSERT INTO usage(source,key,ts,harness,bucket,model,input,output) VALUES('t',?,?,?,?,?,10,100)",
                   (str(i), now - i * 600, "claude", "claude-sub", "sonnet"))
    db.commit()
    db.close()
    rc, out, err = abx("snapshot", "--json")
    assert rc == 0, err
    snap = json.loads(out)
    validator().validate(snap)
    hourly = snap["usage_hourly"]["claude-sub"]
    assert len(hourly) == 168 and sum(hourly) == 3000 and hourly[-1] > 0
    assert snap["usage_split"][0]["harness"] == "claude" and snap["usage_split"][0]["output"] == 3000


def test_tui_launcher_without_binary_prints_the_install_line(abx):
    env = clean_env(abx.home, PATH="/usr/bin:/bin")
    p = subprocess.run([sys.executable, "-m", "agentboard", "tui"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert p.returncode == 127
    assert "cargo install --git https://github.com/riceharvest/agentboard ab-tui" in p.stderr
    assert not list(abx.home.glob("*.db")), "`ab tui` must not open the database itself"


def test_tui_launcher_passes_arguments_to_the_binary(abx, tmp_path):
    fake = tmp_path / "ab-tui"
    fake.write_text('#!/bin/sh\necho "args:$@ home:$AB_HOME"\n')
    fake.chmod(0o755)
    env = clean_env(abx.home, AB_TUI_BIN=str(fake))
    p = subprocess.run([sys.executable, "-m", "agentboard", "tui", "--no-fx", "--board-home", "/x"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == f"args:--no-fx --board-home /x home:{abx.home}"
