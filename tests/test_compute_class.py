"""require_compute_class: with the rule on, a card that declares neither cpu nor gpu does not
launch, `ab add/edit --cpu|--gpu` set the label, and a cpu declaration is never a 1-slot resource.
Throwaway AB_HOME; spawn is stubbed so only the compute gate is under test."""
import json
import types

from agentboard import harness as H, runner, store


def _cand(harness="claude", model="sonnet", effort="high"):
    return H.Candidate(harness=harness, model=model, effort=effort)


def _setup(abx, monkeypatch, rule=True):
    code, _, err = abx("--json", "config", "set", "require_compute_class", "true" if rule else "false",
                       "--as", "tester", "--why", "t")
    assert code == 0, err
    c = store.connect(abx.home / store.DB_PATH.name)
    monkeypatch.setattr(runner.H, "candidates_for", lambda conn, task: ([_cand()], []))
    monkeypatch.setattr(runner.H, "ADAPTERS", {"claude": types.SimpleNamespace(available=lambda: True)})
    r = runner.Runner(c, log=types.SimpleNamespace(write=lambda s: None, flush=lambda s2: None))
    spawned = []

    def fake_spawn(t, cand, bucket, workdir, worktree=False, lockfree=False):
        spawned.append(t["id"])
        r.children[t["id"]] = types.SimpleNamespace(task_id=t["id"], board=t["board"], workdir=None)
        return True
    monkeypatch.setattr(r, "spawn", fake_spawn)
    return c, r, spawned


def _add(abx, *extra):
    code, out, err = abx("--json", "add", "card", "--as", "tester", "--why", "t", *extra)
    assert code == 0, err
    return json.loads(out)["id"]


def _labels(c, tid):
    return json.loads(c.execute("SELECT labels FROM tasks WHERE id=?", (tid,)).fetchone()[0])


def test_undeclared_card_is_gated_and_declared_ones_launch(abx, monkeypatch):
    c, r, spawned = _setup(abx, monkeypatch)
    bare = _add(abx, "--body", "x")
    cpu = _add(abx, "--body", "x", "--cpu")
    gpu = _add(abx, "--body", "x", "--gpu")
    r.fill(4)
    assert set(spawned) == {cpu, gpu}, spawned            # declared cards start
    assert bare not in spawned and r.gates[bare].startswith("compute:"), r.gates
    assert _labels(c, cpu).get("cpu") == "1" and _labels(c, gpu).get("gpu") == "1"
    assert store.compute_class(_labels(c, cpu)) == "cpu" and store.compute_class(_labels(c, gpu)) == "gpu"


def test_cpu_is_not_a_single_slot_resource(abx, monkeypatch):
    """Declaring cpu must never serialize the board: the second cpu card still gets a launch."""
    c, r, spawned = _setup(abx, monkeypatch)
    first = _add(abx, "--body", "x", "--cpu")
    second = _add(abx, "--body", "x", "--cpu")
    r.fill(4)
    assert set(spawned) == {first, second}, spawned
    assert store.resource_block(c, _labels(c, second)) is None


def test_rule_off_leaves_the_board_as_it_was(abx, monkeypatch):
    c, r, spawned = _setup(abx, monkeypatch, rule=False)
    bare = _add(abx, "--body", "x")
    r.fill(2)
    assert spawned == [bare] and not r.gates.get(bare)


def test_edit_sets_the_class(abx, monkeypatch):
    c, _, _ = _setup(abx, monkeypatch)
    tid = _add(abx, "--body", "x")
    code, _, err = abx("--json", "edit", tid, "--gpu", "--as", "tester", "--why", "t")
    assert code == 0, err
    assert store.compute_class(_labels(c, tid)) == "gpu"
