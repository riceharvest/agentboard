"""`ab waves`: dependency waves, critical path and parallelism of the unfinished cards of a board.

Wall time of a plan is governed by its critical path (and by the busiest resource pool), not by the number of cards or agent-hours. Each unfinished
card gets a duration estimate: label est_s, else the median run time of finished cards with the same `step` label prefix on that board, else the
board median, else DEFAULT_S. Finished and cancelled cards count as satisfied dependencies.
"""
from __future__ import annotations

import json
from statistics import median

from . import store

DEFAULT_S = 300.0
OPEN = ("open", "claimed")


def _labels(row):
    try:
        return json.loads(row["labels"] or "{}")
    except ValueError:
        return {}


def _run_seconds(conn, board):
    """task id -> total run seconds of finished runs, for done cards of the board."""
    q = ("SELECT t.id, t.labels, SUM(r.ended_at - r.started_at) s FROM tasks t JOIN runs r ON r.task_id = t.id "
         "WHERE t.board=? AND t.status='done' AND r.ended_at IS NOT NULL GROUP BY t.id")
    return [(r["id"], _labels(r), float(r["s"] or 0.0)) for r in conn.execute(q, (board,))]


def estimates(conn, board, cards):
    done = _run_seconds(conn, board)
    by_step = {}
    for _id, lb, s in done:
        step = str(lb.get("step") or "")
        if step:
            by_step.setdefault(step[:1], []).append(s)  # group letter of a step id (A..H) when present
    board_med = median([s for _, _, s in done]) if done else DEFAULT_S
    out = {}
    for c in cards:
        lb = c["labels"]
        try:
            est = float(lb["est_s"])
            src = "est_s"
        except (KeyError, ValueError, TypeError):
            grp = by_step.get(str(lb.get("step") or "")[:1])
            est, src = (median(grp), "group median") if grp else (board_med, "board median" if done else "default")
        out[c["id"]] = (est, src)
    return out


def compute(conn, board=None):
    where, args = "status IN ('open','claimed')", []
    if board:
        where += " AND board=?"
        args.append(board)
    rows = conn.execute(f"SELECT id, title, status, board, labels FROM tasks WHERE {where}", args).fetchall()
    cards = {r["id"]: {"id": r["id"], "title": r["title"], "status": r["status"], "board": r["board"], "labels": _labels(r)} for r in rows}
    deps = {i: [] for i in cards}
    for r in conn.execute("SELECT task_id, dep_id FROM deps"):
        if r["task_id"] in cards and r["dep_id"] in cards:
            deps[r["task_id"]].append(r["dep_id"])
    est = {}
    for b in {c["board"] for c in cards.values()}:
        est.update(estimates(conn, b, [c for c in cards.values() if c["board"] == b]))
    level, finish, pred = {}, {}, {}

    def walk(i, seen=()):
        if i in finish:
            return
        if i in seen:  # dependency cycle: treat the back edge as absent
            return
        d = [j for j in deps[i] if j not in seen]
        for j in d:
            walk(j, seen + (i,))
        level[i] = 1 + max([level[j] for j in d if j in level], default=-1)
        best = max(d, key=lambda j: finish.get(j, 0.0), default=None)
        pred[i] = best
        finish[i] = est[i][0] + (finish.get(best, 0.0) if best else 0.0)

    for i in cards:
        walk(i)
    waves = {}
    for i, lv in level.items():
        waves.setdefault(lv, []).append(i)
    end = max(finish, key=finish.get, default=None)
    path, cur = [], end
    while cur:
        path.append(cur)
        cur = pred.get(cur)
    path.reverse()
    total = sum(e for e, _ in est.values())
    crit = finish.get(end, 0.0) if end else 0.0
    caps = store.cfg(conn, "resources") or {}
    pools = {}
    for i, c in cards.items():
        for n in store.board_needs(conn, c["labels"], c["board"]):
            if n != "cpu":
                pools[n] = pools.get(n, 0.0) + est[i][0]
    bound = {n: s / max(1, int(caps.get(n, 1))) for n, s in pools.items()}
    wall = max([crit] + list(bound.values()))
    return {
        "cards": len(cards), "waves": [{"wave": k, "width": len(v), "cards": [{"id": i, "title": cards[i]["title"], "status": cards[i]["status"],
                                                                               "est_s": round(est[i][0]), "est_from": est[i][1]} for i in v]} for k, v in sorted(waves.items())],
        "critical_path": [{"id": i, "title": cards[i]["title"], "est_s": round(est[i][0])} for i in path],
        "critical_path_s": round(crit), "total_work_s": round(total), "parallelism": round(total / crit, 2) if crit else None,
        "resource_bound_s": {n: round(s) for n, s in bound.items()}, "wall_lower_bound_s": round(wall),
        "limited_by": ("critical path" if wall == crit else max(bound, key=bound.get)) if cards else None,
    }


def render(d):
    if not d["cards"]:
        return "no unfinished cards"
    h = lambda s: f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"
    lines = [f"{d['cards']} unfinished cards, {len(d['waves'])} waves; total work {h(d['total_work_s'])}, critical path {h(d['critical_path_s'])}, "
             f"parallelism {d['parallelism']}x; wall lower bound {h(d['wall_lower_bound_s'])} (limited by {d['limited_by']})"]
    if d["resource_bound_s"]:
        lines.append("resource bounds: " + ", ".join(f"{n} {h(s)}" for n, s in d["resource_bound_s"].items()))
    for w in d["waves"]:
        lines.append(f"wave {w['wave']} (width {w['width']}): " + "; ".join(f"{c['title'][:42]} [{h(c['est_s'])}]" for c in w["cards"][:6])
                     + (f"; +{len(w['cards']) - 6} more" if len(w["cards"]) > 6 else ""))
    lines.append("critical path: " + " > ".join(c["title"][:30] for c in d["critical_path"]))
    return "\n".join(lines)
