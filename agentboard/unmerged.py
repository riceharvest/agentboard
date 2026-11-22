"""Report done worktree cards whose branch never reached the default branch."""
import json
import subprocess
import time


def _git(repo, *a):
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def default_branch(repo):
    """origin/HEAD's name when set, else a local master/main, else the checked-out branch."""
    rc, o = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    names = [o.split("/", 1)[1]] if rc == 0 and "/" in o else []
    for n in [*names, "master", "main"]:
        if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{n}")[0] == 0:
            return n
    rc, o = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return o if rc == 0 and o != "HEAD" else "HEAD"


def check_branch(repo, branch, default=None):
    """None when merged; {'missing': True} when the branch is gone; else {'ahead': N, 'subject': s}."""
    default = default or default_branch(repo)
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] != 0:
        return {"missing": True}
    if _git(repo, "merge-base", "--is-ancestor", branch, default)[0] == 0:
        return None
    n = _git(repo, "rev-list", "--count", f"{default}..{branch}")[1]
    return {"ahead": int(n) if n.isdigit() else 0, "subject": _git(repo, "log", "-1", "--format=%s", branch)[1]}


def scan(c, repo=None, board=None):
    """Every done card with a stored worktree_branch -> {'unmerged': [...], 'count', 'total', 'oldest_days'}."""
    sql, args = "SELECT id, workdir, labels, updated_at FROM tasks WHERE status='done'", []
    if board:
        sql += " AND board=?"
        args.append(board)
    cache, rows, total = {}, [], 0
    for r in c.execute(sql + " ORDER BY updated_at", args).fetchall():
        br = (json.loads(r["labels"] or "{}") or {}).get("worktree_branch")
        if not br:
            continue
        total += 1
        rp = repo or r["workdir"] or "."
        if rp not in cache:
            cache[rp] = default_branch(rp)
        res = check_branch(rp, br, cache[rp])
        if res is None:
            continue
        rows.append({"id": r["id"], "branch": br, "repo": str(rp), "default": cache[rp],
                     "days": round(max(0, time.time() - (r["updated_at"] or time.time())) / 86400, 1), **res})
    return {"unmerged": rows, "count": len(rows), "total": total,
            "oldest_days": max((x["days"] for x in rows), default=0)}


def lines(rep):
    out = []
    for x in rep["unmerged"]:
        tail = "branch missing" if x.get("missing") else f"+{x['ahead']}  {x['subject']}"
        out.append(f"{x['id']}  {x['branch']}  {tail}")
    out.append(f"unmerged: {rep['count']} of {rep['total']} done worktree card(s), oldest {int(rep['oldest_days'])}d")
    return out


def warning(repo, branch):
    """The one-line `ab done` note for an unmerged branch, else None."""
    if not repo or not branch:
        return None
    try:
        d = default_branch(repo)
        r = check_branch(repo, branch, d)
    except OSError:
        return None
    if not r or r.get("missing"):
        return None
    return (f"note: {branch} has {r['ahead']} commit(s) not on {d} "
            f"(land it: git -C {repo} merge {branch})")
