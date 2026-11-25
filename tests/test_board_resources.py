"""board_resources: one board's `gpu` is its own pool (own capacity), board_env: environment for that board's runs only; AB_OUT dir."""
import json
import os
import subprocess
import sys

from conftest import ROOT, clean_env


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, "--tier", "low", "--gpu", *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def _pools(abx):
    code, o, e = abx("--json", "resources")
    assert code == 0, e
    return {r["resource"]: r for r in json.loads(o)}


def test_per_board_gpu_pool_does_not_block_other_boards(abx):
    assert abx("boards", "create", "bb")[0] == 0
    assert abx("config", "set", "board_resources", json.dumps({"bb": {"gpu": "gpu:bb"}}))[0] == 0
    a1 = _add(abx, "a1")
    a2 = _add(abx, "a2")
    b1 = _add(abx, "b1", "-B", "bb")
    b2 = _add(abx, "b2", "-B", "bb")
    assert abx("claim", a1)[0] == 0
    p = _pools(abx)
    assert p["gpu"]["held_by"] == [a1] and p["gpu:bb"]["held_by"] == []
    assert p["gpu"]["waiting"] == 1 and p["gpu:bb"]["waiting"] == 2   # a2 waits for the shared pool; b1 and b2 want the board's own
    assert abx("-B", "bb", "claim", b1)[0] == 0
    p = _pools(abx)
    assert p["gpu:bb"]["held_by"] == [b1] and p["gpu"]["held_by"] == [a1]
    assert a2 and b2


def test_board_env_and_out_dir(abx, tmp_path):
    assert abx("config", "set", "board_env", json.dumps({"bb": {"OPT_IN": "1"}}))[0] == 0
    code = ("import json; from agentboard import store; c = store.connect(); "
            "print(json.dumps([store.board_env(c, 'bb'), store.board_env(c, 'main'), str(store.out_dir('t_x'))]))")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=clean_env(abx.home))
    assert p.returncode == 0, p.stderr
    bb, main, out = json.loads(p.stdout)
    assert bb == {"OPT_IN": "1"} and main == {}
    assert out.endswith("/out/t_x") and os.path.isdir(out)
