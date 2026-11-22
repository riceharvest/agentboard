"""RAM gate idle probe: with zero live runs under the floor, one run starts anyway (inside the band). Free RAM is stubbed
by monkeypatching runner.free_gib, so nothing depends on the host's memory. Throwaway AB_HOME."""
import json
import types

from agentboard import runner, store


def _add(abx, title):
    code, out, err = abx("--json", "add", title, "--cmd", "true")
    assert code == 0, err
    return json.loads(out)["id"]


def _setup(abx, monkeypatch, gib, n=2):
    ids = [_add(abx, f"c{i}") for i in range(n)]
    c = store.connect(abx.home / store.DB_PATH.name)
    store.cfg_set(c, "min_free_gib", 12, "test")
    monkeypatch.setattr(runner, "free_gib", lambda: gib)
    spawned = []
    r = runner.Runner(c, log=types.SimpleNamespace(write=lambda s: None, flush=lambda: None))

    def fake_spawn(t, workdir):
        spawned.append(t["id"])
        r.children[t["id"]] = types.SimpleNamespace(task_id=t["id"], board=t["board"], workdir=None)
        return True
    monkeypatch.setattr(r, "spawn_cmd", fake_spawn)
    return c, r, ids, spawned


def test_idle_board_starts_one_probe(abx, monkeypatch):
    c, r, ids, spawned = _setup(abx, monkeypatch, 11.0)
    try:
        r.fill(24)
        assert len(spawned) == 1
        other = [i for i in ids if i not in spawned][0]
        assert r.gates[other].startswith("ram_gate:") and f"idle probe {spawned[0]}" in r.gates[other]
        r.fill(24)  # one run live now: gate holds, no second probe
        assert len(spawned) == 1 and r.gates[other].startswith("ram_gate:")
    finally:
        c.close()


def test_live_child_keeps_gate(abx, monkeypatch):
    c, r, ids, spawned = _setup(abx, monkeypatch, 11.0)
    try:
        r.children["x"] = types.SimpleNamespace(task_id="x", board="main", workdir=None)
        r.fill(24)
        assert spawned == []
        assert all(v.startswith("ram_gate:") and "probe" not in v for v in r.gates.values()) and len(r.gates) == 2
    finally:
        c.close()


def test_below_band_stays_gated_and_one_probe_per_episode(abx, monkeypatch):
    c, r, ids, spawned = _setup(abx, monkeypatch, 1.5)  # under the 2 GiB hard floor
    try:
        r.fill(24)
        assert spawned == []
        monkeypatch.setattr(runner, "free_gib", lambda: 11.0)
        r.fill(24)
        assert len(spawned) == 1
        r.children.clear()  # probe ended, gate still tripped: same episode, no second probe
        r.fill(24)
        assert len(spawned) == 1
        monkeypatch.setattr(runner, "free_gib", lambda: 13.0)  # gate clears: new episode
        r.children.clear()
        r.fill(1)
        monkeypatch.setattr(runner, "free_gib", lambda: 11.0)
        r.children.clear()
        r.fill(24)
        assert len(spawned) == 3
    finally:
        c.close()


def test_grace_delays_probe(abx, monkeypatch):
    c, r, ids, spawned = _setup(abx, monkeypatch, 11.0)
    try:
        store.cfg_set(c, "ram_gate_idle_grace_s", 3600, "test")
        r.fill(24)
        assert spawned == []
    finally:
        c.close()
