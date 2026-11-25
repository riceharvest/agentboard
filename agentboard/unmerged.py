"""Report done worktree cards whose branch never reached the default branch."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


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


def _dir_bytes(path):
    total = 0
    for root, _, files in os.walk(path, onerror=lambda _: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _worktree_repo(wt_path):
    """Main git repo root for a linked worktree, or None."""
    rc, common = _git(wt_path, "rev-parse", "--git-common-dir")
    if rc != 0 or not common:
        return None
    p = Path(common)
    if not p.is_absolute():
        p = (Path(wt_path) / p).resolve()
    return str(p.parent) if p.name == ".git" else str(p)


def prune_worktrees(c, wt_root, dry_run=True, include_unmerged=False, board=None):
    """Safely prune finished-task worktrees under `wt_root`.

    Never touches `open` or `claimed` tasks. Skips dirty worktrees and (unless `include_unmerged`)
    branches that have commits not merged into the default branch.
    """
    wt_root = Path(wt_root)
    rep = {"dry_run": bool(dry_run), "removed": [], "skipped_active": 0, "skipped_dirty": [],
           "skipped_unmerged": [], "errors": [], "bytes_reclaimed": 0}
    if not wt_root.is_dir():
        return rep
    tasks = {}
    for r in c.execute("SELECT id, board, status, workdir, labels FROM tasks"):
        tasks[r["id"]] = dict(r)
    repos_to_prune = set()
    default_cache = {}
    for entry in sorted(wt_root.iterdir()):
        if not entry.is_dir():
            continue
        tid = entry.name
        t = tasks.get(tid)
        if board and (not t or t["board"] != board):
            continue
        if t and t["status"] in ("open", "claimed"):
            rep["skipped_active"] += 1
            continue
        lb = json.loads(t["labels"] or "{}") if t else {}
        br = lb.get("worktree_branch") or f"ab/{tid}"
        repo = _worktree_repo(entry) or (t["workdir"] if t else None)
        # Check cleanliness if valid git worktree
        rc_st, st_out = _git(entry, "status", "--porcelain")
        if rc_st == 0 and st_out:
            rep["skipped_dirty"].append({"id": tid, "path": str(entry), "status": t["status"] if t else "missing"})
            continue
        # Check merge status if we know the repo
        if repo and os.path.isdir(repo) and not include_unmerged:
            if repo not in default_cache:
                default_cache[repo] = default_branch(repo)
            chk = check_branch(repo, br, default_cache[repo])
            if chk and not chk.get("missing") and chk.get("ahead", 0) > 0:
                rep["skipped_unmerged"].append({"id": tid, "branch": br, "ahead": chk["ahead"], "repo": repo})
                continue
        sz = _dir_bytes(entry)
        if not dry_run:
            ok = False
            if repo and os.path.isdir(repo):
                rc, _ = _git(repo, "worktree", "remove", "--force", str(entry))
                ok = (rc == 0)
                repos_to_prune.add(repo)
            if not ok and entry.exists():
                try:
                    shutil.rmtree(entry)
                    ok = True
                except OSError as e:
                    rep["errors"].append({"id": tid, "error": str(e)})
                    continue
            if not ok and entry.exists():
                rep["errors"].append({"id": tid, "error": "failed to remove"})
                continue
        rep["removed"].append({"id": tid, "status": t["status"] if t else "missing", "bytes": sz})
        rep["bytes_reclaimed"] += sz
    if not dry_run:
        for repo in sorted(repos_to_prune):
            _git(repo, "worktree", "prune")
    return rep


def render_prune(rep):
    gb = rep["bytes_reclaimed"] / (1024 ** 3)
    verb = "would remove" if rep["dry_run"] else "removed"
    lines_out = [
        f"worktrees prune ({'dry-run' if rep['dry_run'] else 'applied'}): {verb} {len(rep['removed'])} worktree(s) ({gb:.2f} GiB)",
        f"  skipped: active={rep['skipped_active']} dirty={len(rep['skipped_dirty'])} unmerged={len(rep['skipped_unmerged'])} errors={len(rep['errors'])}",
    ]
    return "\n".join(lines_out)
