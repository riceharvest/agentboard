"""`ab waves`: dependency waves, critical path and parallelism of the unfinished cards."""
import json


def _add(abx, title, *extra):
    code, o, e = abx("--json", "add", title, "--tier", "low", *extra)
    assert code == 0, e
    return json.loads(o)["id"]


def test_diamond_waves_and_critical_path(abx):
    a = _add(abx, "A", "-l", "est_s=60")
    b = _add(abx, "B", "-l", "est_s=300", "--dep", a)
    c = _add(abx, "C", "-l", "est_s=100", "--dep", a)
    d = _add(abx, "D", "-l", "est_s=30", "--dep", b, "--dep", c)
    code, o, e = abx("--json", "waves")
    assert code == 0, e
    w = json.loads(o)
    assert w["cards"] == 4
    assert [x["width"] for x in w["waves"]] == [1, 2, 1]
    assert [x["id"] for x in w["critical_path"]] == [a, b, d]
    assert w["critical_path_s"] == 390 and w["total_work_s"] == 490
    assert w["parallelism"] == round(490 / 390, 2)
    assert w["limited_by"] == "critical path"


def test_done_cards_are_satisfied_and_dropped(abx):
    a = _add(abx, "A", "-l", "est_s=60")
    b = _add(abx, "B", "-l", "est_s=60", "--dep", a)
    assert abx("claim", a)[0] == 0 and abx("done", a, "ok")[0] == 0
    w = json.loads(abx("--json", "waves")[1])
    assert w["cards"] == 1 and w["critical_path"][0]["id"] == b and w["waves"][0]["wave"] == 0


def test_resource_pool_bound_beats_a_short_critical_path(abx):
    ids = [_add(abx, f"G{i}", "-l", "est_s=100", "--gpu") for i in range(4)]
    w = json.loads(abx("--json", "waves")[1])
    assert w["critical_path_s"] == 100 and w["resource_bound_s"] == {"gpu": 400}
    assert w["wall_lower_bound_s"] == 400 and w["limited_by"] == "gpu" and len(ids) == 4


def test_empty_board(abx):
    code, o, e = abx("waves")
    assert code == 0 and "no unfinished cards" in o
