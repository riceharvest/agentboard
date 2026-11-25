"""Knowledge factory: mine runs, outcomes, reviews and logs into compact, actionable priors.

Read-only analysis over `tasks`, `runs`, `events` and optional run logs (`runs/*.out`, `runs/*.err`),
producing:
  - Per-(board, type) outcome, churn, park-loop, review-verdict and cache-efficiency metrics
  - Per-template (`label:tpl`) A/B performance leaderboard
  - Recurring failure/error signatures and WEAKER/FAIL review critiques
  - Compact per-(board, type) priors that can be persisted in `control` (`_knowledge_priors`)
    and optionally injected into worker briefs when `inject_priors` config is enabled.
"""
import json
import os
import re
from collections import Counter, defaultdict

from . import outcomes, store

DONE = outcomes.DONE
_LOCK_WORDS = ("lock", "busy", "gpu", "xpu", "held by", "queued behind", "slot", "resource")
_ERR_PATTERNS = [
    ("venv_no_module", re.compile(r"No module named ['\"]?([A-Za-z0-9_.-]+)")),
    ("gateway_down", re.compile(r"Gateway (?:connection timed out|starting: deadline|did not answer)")),
    ("gateway_lost", re.compile(r"Gateway disconnected")),
    ("rate_limit", re.compile(r"(?:HTTP 429|usage limit|5-hour limit|quota reached|rate limit)", re.I)),
    ("credits_depleted", re.compile(r"(?:HTTP 402|CreditsDepleted)", re.I)),
    ("gpu_lock_busy", re.compile(r"(?:lock held|gpu.*busy|queued behind|waiting for .*lock)", re.I)),
    ("cuda_oom", re.compile(r"(?:CUDA out of memory|OutOfMemoryError|OOM)", re.I)),
    ("dirty_worktree", re.compile(r"(?:uncommitted edits|untracked or modified files|worktree.*dirty)", re.I)),
    ("permission_or_auth", re.compile(r"(?:PermissionError|API key has expired|auth)", re.I)),
    ("timeout_or_stall", re.compile(r"(?:no sign of life|timed out|TimeoutError|deadline)", re.I)),
    ("file_not_found", re.compile(r"FileNotFoundError:[^\n]{0,80}")),
    ("assertion_failed", re.compile(r"AssertionError[^\n]{0,80}")),
]


def _review_verdict(result):
    u = (result or "").strip().upper()[:140]
    if not u:
        return "OTHER"
    if re.search(r"\bFAIL\b", u):
        return "FAIL"
    if re.search(r"\bWEAKER\b", u):
        return "WEAKER"
    if re.search(r"\bPASS\b", u):
        return "PASS"
    return "OTHER"


def _is_lock_park(detail, park_json):
    txt = ((detail or "") + " " + (park_json or "")).lower()
    return any(w in txt for w in _LOCK_WORDS)


def _scan_tail(path, max_bytes=24576):
    if not path or not os.path.isfile(path):
        return ""
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > max_bytes:
                f.seek(sz - max_bytes)
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _extract_errors(text):
    hits = []
    if not text:
        return hits
    for name, pat in _ERR_PATTERNS:
        m = pat.search(text)
        if m:
            if name == "venv_no_module" and m.lastindex:
                hits.append(f"venv_no_module:{m.group(1)}")
            else:
                hits.append(name)
    return hits


def _short_critique(result, limit=180):
    s = " ".join((result or "").strip().split())
    return s[:limit]


def mine(conn, since_s=None, board=None, scan_logs=True, max_logs=250, t=None):
    """Mine runs, tasks, reviews and optional run logs into structured knowledge and compact priors."""
    t = t or store.now()
    cutoff = t - since_s if since_s else 0
    sql = (
        "SELECT r.id, r.task_id, r.harness, r.model, r.tier, r.outcome, r.started_at, r.ended_at,"
        " r.input, r.output, r.cache_read, r.cache_write, r.cost_usd, r.detail, r.out_path, r.err_path,"
        " k.board, k.status tstatus, k.labels, k.result, k.park, k.title"
        " FROM runs r JOIN tasks k ON k.id=r.task_id"
    )
    args = []
    if board:
        sql += " WHERE k.board=?"
        args.append(board)
    sql += " ORDER BY r.id"
    runs = conn.execute(sql, args).fetchall()

    last_done = {}
    for r in runs:
        if r["outcome"] in DONE:
            last_done[r["task_id"]] = r["id"]

    retries = {
        e["task_id"]: e["ts"]
        for e in conn.execute("SELECT task_id, MAX(ts) ts FROM events WHERE kind='retry' GROUP BY task_id")
    }

    # Collect review verdicts and link back to reviewed tasks
    reviews_by_target = {}
    review_critiques = defaultdict(list)  # (board, target_type) -> [(verdict, snippet)]
    task_meta = {}
    for k in conn.execute("SELECT id, board, status, labels, result FROM tasks"):
        lb = json.loads(k["labels"] or "{}")
        task_meta[k["id"]] = (k["board"] or "main", lb.get("type") or "untyped", lb, k["status"], k["result"] or "")

    verdict_totals = Counter()
    for tid, (bd, tp, lb, st, res) in task_meta.items():
        if board and bd != board:
            continue
        if st != "done":
            continue
        if tp == "review" or "reviews" in lb:
            v = _review_verdict(res)
            verdict_totals[v] += 1
            tgt = lb.get("reviews")
            if tgt:
                reviews_by_target[tgt] = (v, _short_critique(res))
                tbd, ttp = task_meta.get(tgt, (bd, "untyped"))[:2]
                if v in ("WEAKER", "FAIL"):
                    review_critiques[(tbd, ttp)].append((v, _short_critique(res)))
                    review_critiques[(tbd, "*")].append((v, _short_critique(res)))

    bt_stats = defaultdict(lambda: {
        "runs": 0, "accepted": 0, "abandoned": 0, "parked_lock": 0, "parked_job": 0,
        "failed": 0, "verify_failed": 0, "infra": 0, "killed": 0, "limit": 0, "release_loop": 0,
        "agent_s": 0.0, "usd": 0.0, "waste_usd": 0.0, "output": 0, "cache_read": 0,
        "reviews": Counter(), "errors": Counter(), "tiers": defaultdict(lambda: [0, 0, 0.0]),
    })
    tpl_stats = defaultdict(lambda: {"runs": 0, "accepted": 0, "usd": 0.0, "agent_s": 0.0, "reviews": Counter()})
    card_parks = defaultdict(lambda: {"board": "", "type": "", "title": "", "status": "", "parks": 0, "runs": 0, "usd": 0.0})
    totals = {"runs": 0, "accepted": 0, "abandoned": 0, "parked": 0, "parked_lock": 0,
              "failed": 0, "verify_failed": 0, "infra": 0, "usd": 0.0, "waste_usd": 0.0, "agent_s": 0.0}

    logs_scanned = 0
    for r in runs:
        if r["started_at"] is not None and r["started_at"] < cutoff:
            continue
        lb = json.loads(r["labels"] or "{}")
        bd = r["board"] or "main"
        tp = lb.get("type") or "untyped"
        oc = r["outcome"]
        dur = max(0.0, (r["ended_at"] or t) - (r["started_at"] or t))
        usd = float(r["cost_usd"] or 0.0)

        s = bt_stats[(bd, tp)]
        s["runs"] += 1
        s["agent_s"] += dur
        s["usd"] += usd
        s["output"] += r["output"] or 0
        s["cache_read"] += r["cache_read"] or 0

        totals["runs"] += 1
        totals["agent_s"] += dur
        totals["usd"] += usd

        cp = card_parks[r["task_id"]]
        cp["board"], cp["type"], cp["title"], cp["status"] = bd, tp, r["title"] or "", r["tstatus"] or ""
        cp["runs"] += 1
        cp["usd"] += usd

        is_acc = False
        if oc in DONE:
            rv = reviews_by_target.get(r["task_id"], (None, ""))[0]
            if (last_done.get(r["task_id"]) == r["id"] and r["tstatus"] == "done"
                    and (r["result"] or "").strip()
                    and retries.get(r["task_id"], 0) <= (r["ended_at"] or 0)
                    and rv != "FAIL"):
                is_acc = True
                s["accepted"] += 1
                totals["accepted"] += 1
                if rv:
                    s["reviews"][rv] += 1
        else:
            s["waste_usd"] += usd
            totals["waste_usd"] += usd
            if oc in ("agent_open", "agent_claimed"):
                s["abandoned"] += 1
                totals["abandoned"] += 1
            elif oc == "parked":
                totals["parked"] += 1
                cp["parks"] += 1
                if _is_lock_park(r["detail"], r["park"]):
                    s["parked_lock"] += 1
                    totals["parked_lock"] += 1
                else:
                    s["parked_job"] += 1
            elif oc == "verify_failed":
                s["verify_failed"] += 1
                totals["verify_failed"] += 1
            elif oc in ("failed", "agent_dead", "auth"):
                s["failed"] += 1
                totals["failed"] += 1
            elif oc in ("gateway_lost", "gateway_down", "interrupted"):
                s["infra"] += 1
                totals["infra"] += 1
            elif oc in ("killed", "idle", "expired"):
                s["killed"] += 1
            elif oc == "limit":
                s["limit"] += 1
            elif oc == "release_loop":
                s["release_loop"] += 1

        # Tier/model tracking (exclude lock parks from tier denominator so GPU waits don't poison model stats)
        tk = f"{r['tier'] or '-'}:{r['harness'] or '-'}/{r['model'] or '-'}"
        if not (oc == "parked" and _is_lock_park(r["detail"], r["park"])):
            s["tiers"][tk][0] += 1
            if is_acc:
                s["tiers"][tk][1] += 1
            s["tiers"][tk][2] += usd

        # Template tracking
        tpl = lb.get("tpl")
        if tpl:
            ts = tpl_stats[tpl]
            ts["runs"] += 1
            ts["usd"] += usd
            ts["agent_s"] += dur
            if is_acc:
                ts["accepted"] += 1
                rv = reviews_by_target.get(r["task_id"], (None, ""))[0]
                if rv:
                    ts["reviews"][rv] += 1

        # Error signature mining on non-accepted runs
        if not is_acc and oc is not None:
            errs = _extract_errors(r["detail"] or "")
            if scan_logs and not errs and logs_scanned < max_logs and oc in ("failed", "verify_failed", "gateway_down", "gateway_lost", "agent_open"):
                tail = _scan_tail(r["err_path"]) + "\n" + _scan_tail(r["out_path"])
                errs = _extract_errors(tail)
                logs_scanned += 1
            for e in errs:
                s["errors"][e] += 1

    # Build board/type rows & compact priors
    by_bt = []
    priors = {}
    for (bd, tp), s in sorted(bt_stats.items(), key=lambda x: -x[1]["runs"]):
        eff_runs = max(0, s["runs"] - s["parked_lock"])
        acc_rate = round(s["accepted"] / eff_runs, 3) if eff_runs else 0.0
        cr_ratio = round(s["cache_read"] / s["output"], 1) if s["output"] else 0.0
        best_tier = None
        cand = [(k, v[0], v[1] / v[0], v[2]) for k, v in s["tiers"].items() if v[0] >= 5]
        if cand:
            cand.sort(key=lambda x: (-x[2], -x[1]))
            best_tier = {"route": cand[0][0], "runs": cand[0][1], "rate": round(cand[0][2], 3)}
        row = {
            "board": bd, "type": tp, "runs": s["runs"], "eff_runs": eff_runs,
            "accepted": s["accepted"], "acc_rate": acc_rate,
            "abandoned": s["abandoned"], "parked_lock": s["parked_lock"], "parked_job": s["parked_job"],
            "failed": s["failed"], "verify_failed": s["verify_failed"], "infra": s["infra"],
            "killed": s["killed"], "limit": s["limit"], "release_loop": s["release_loop"],
            "agent_h": round(s["agent_s"] / 3600, 2), "usd": round(s["usd"], 2),
            "waste_usd": round(s["waste_usd"], 2), "cache_to_out": cr_ratio,
            "reviews": dict(s["reviews"]), "top_errors": s["errors"].most_common(5),
            "best_route": best_tier,
        }
        by_bt.append(row)

        # Distill compact priors for injection (only when meaningful signal exists)
        pitfalls = []
        if s["parked_lock"] >= 5:
            pitfalls.append(f"Lock/resource contention caused {s['parked_lock']} park loops; use `ab park --resource` or check lock once and park instead of polling.")
        if s["abandoned"] >= 5:
            pitfalls.append(f"{s['abandoned']} runs exited without closing (`agent_open`/`agent_claimed`); always end with `ab done`, `ab park`, `ab defer`, or `ab fail`.")
        if s["verify_failed"] >= 2:
            pitfalls.append(f"{s['verify_failed']} runs failed `--verify`; run the exact verify command locally before calling `ab done`.")
        for err, cnt in s["errors"].most_common(3):
            if cnt >= 2 and err != "gpu_lock_busy":
                pitfalls.append(f"Recurring error `{err}` ({cnt}x) — check environment/preconditions before long work.")
        crits = review_critiques.get((bd, tp)) or review_critiques.get((bd, "*")) or []
        rev_notes = []
        if crits:
            w_cnt = sum(1 for v, _ in crits if v == "WEAKER")
            f_cnt = sum(1 for v, _ in crits if v == "FAIL")
            if w_cnt + f_cnt >= 2:
                rev_notes.append(f"Past reviews on {bd}/{tp}: {w_cnt} WEAKER, {f_cnt} FAIL — keep claims strictly bounded to verified numbers and exact scope.")
            for v, snip in crits[-2:]:
                rev_notes.append(f"[{v}] {snip[:140]}")
        if pitfalls or rev_notes:
            priors[f"{bd}/{tp}"] = {
                "board": bd, "type": tp, "runs": s["runs"], "acc_rate": acc_rate,
                "pitfalls": pitfalls[:4], "review_notes": rev_notes[:3],
                "best_route": best_tier,
            }

    templates = sorted(
        [{
            "tpl": k, "runs": v["runs"], "accepted": v["accepted"],
            "acc_rate": round(v["accepted"] / v["runs"], 3) if v["runs"] else 0.0,
            "usd": round(v["usd"], 2), "agent_h": round(v["agent_s"] / 3600, 2),
            "reviews": dict(v["reviews"]),
        } for k, v in tpl_stats.items()],
        key=lambda x: (-x["runs"], x["tpl"]),
    )

    park_loops = sorted(
        [{"id": tid, **v, "usd": round(v["usd"], 2)} for tid, v in card_parks.items() if v["parks"] >= 3],
        key=lambda x: (-x["parks"], -x["runs"]),
    )[:20]

    summary = {
        **totals,
        "usd": round(totals["usd"], 2),
        "waste_usd": round(totals["waste_usd"], 2),
        "agent_h": round(totals["agent_s"] / 3600, 2),
        "review_verdicts": dict(verdict_totals),
        "logs_scanned": logs_scanned,
        "priors_count": len(priors),
    }
    return {
        "generated_at": t,
        "since_s": since_s,
        "board": board,
        "summary": summary,
        "by_board_type": by_bt,
        "templates": templates,
        "park_loops": park_loops,
        "priors": priors,
    }


def save_priors(conn, rep, actor="miner"):
    """Persist mined priors into `control._knowledge_priors`."""
    payload = {"generated_at": rep["generated_at"], "priors": rep["priors"]}
    store.cfg_set(conn, "_knowledge_priors", payload, actor=actor, why="ab mine --write")
    return len(rep["priors"])


def load_priors(conn):
    raw = store.cfg(conn, "_knowledge_priors")
    return raw if isinstance(raw, dict) else {"generated_at": None, "priors": {}}


def brief_priors(conn, task):
    """Compact auto-mined pitfalls & review checklist for `runner.build_brief` when `inject_priors` is enabled."""
    if not store.cfg(conn, "inject_priors"):
        return ""
    data = load_priors(conn)
    priors = data.get("priors") or {}
    if not priors:
        return ""
    bd = task["board"] or "main"
    lb = json.loads(task["labels"] or "{}") if isinstance(task["labels"], str) else (task["labels"] or {})
    tp = lb.get("type") or "untyped"
    entry = priors.get(f"{bd}/{tp}")
    if not entry:
        return ""
    lines = []
    for p in (entry.get("pitfalls") or [])[:3]:
        lines.append(f"- {p}")
    for r in (entry.get("review_notes") or [])[:2]:
        lines.append(f"- {r}")
    if not lines:
        return ""
    block = f"\nKNOWN PITFALLS & PRIORS (auto-mined for {bd}/{tp}, {entry.get('runs', 0)} runs, {entry.get('acc_rate', 0):.0%} accepted):\n" + "\n".join(lines) + "\n"
    return block[:1400]


def render_mine(rep):
    s = rep["summary"]
    out = [
        f"mined {s['runs']} runs ({s['agent_h']:.1f}h, ${s['usd']:.2f} tracked, ${s['waste_usd']:.2f} non-accepted)",
        f"  accepted={s['accepted']} abandoned={s['abandoned']} parked={s['parked']} (lock={s['parked_lock']}) "
        f"failed={s['failed']} verify_failed={s['verify_failed']} infra={s['infra']}",
        f"  reviews={s['review_verdicts']}  priors={s['priors_count']}  logs_scanned={s['logs_scanned']}",
        "",
        "top (board/type) hotspots:",
    ]
    for r in rep["by_board_type"][:15]:
        errs = ", ".join(f"{k}={v}" for k, v in r["top_errors"][:3])
        out.append(
            f"  {r['board']}/{r['type']:11s} n={r['runs']:4d} acc={r['acc_rate']:5.1%} "
            f"abnd={r['abandoned']:2d} lock_pk={r['parked_lock']:3d} fail={r['failed']+r['verify_failed']:2d} "
            f"infra={r['infra']:2d} ${r['usd']:6.2f} cr/out={r['cache_to_out']:5.0f}x"
            + (f"  [{errs}]" if errs else "")
        )
    if rep["templates"]:
        out.append("\ntemplate A/B (label:tpl):")
        for x in rep["templates"][:10]:
            out.append(f"  {x['tpl']:22s} n={x['runs']:3d} acc={x['acc_rate']:5.1%} ${x['usd']:6.2f} {x['agent_h']:5.1f}h")
    if rep["park_loops"]:
        out.append("\ntop park-loop cards:")
        for x in rep["park_loops"][:8]:
            out.append(f"  {x['id']} [{x['board']}] ({x['status']}) parks={x['parks']}/{x['runs']} ${x['usd']:.2f} {x['title'][:55]}")
    return "\n".join(out)


def render_priors(data, board=None, task_type=None):
    priors = data.get("priors") or {}
    lines = []
    for k, v in sorted(priors.items()):
        if board and v.get("board") != board:
            continue
        if task_type and v.get("type") != task_type:
            continue
        lines.append(f"[{k}] runs={v.get('runs')} acc={v.get('acc_rate', 0):.1%}")
        for p in v.get("pitfalls") or []:
            lines.append(f"  pitfall: {p}")
        for r in v.get("review_notes") or []:
            lines.append(f"  review:  {r}")
    return "\n".join(lines) or "no mined priors yet (run `ab mine --write`)"
