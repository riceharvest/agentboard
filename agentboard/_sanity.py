"""Cheap sanity checks on the checkout `ab` runs from. Imported before `agentboard.cli`.

A worker that leaves the shared checkout mid-merge makes every module unparsable, so every `ab` call on the host dies in
`SyntaxError` naming a line but not the cause. This module is stdlib-only, imports nothing from the package, and is
called by `_entry` before the CLI is imported (the check cannot live in `cli.py`: that is the file that fails to parse).
"""
import re
from pathlib import Path

MARKER = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")
PACKAGE = Path(__file__).resolve().parent


def checkout_root(package_dir=None):
    """The checkout the package lives in: the parent dir of the `agentboard` package."""
    pkg = Path(package_dir or PACKAGE).resolve()
    return pkg.parent if pkg.name == "agentboard" else pkg


def _scan_files(package_dir=None):
    pkg = Path(package_dir or PACKAGE).resolve()
    root = checkout_root(pkg)
    # Package modules plus the .py modules at the checkout root: a conflicted a.py at the root breaks
    # `python -m agentboard` from there just as badly, and the acceptance case is exactly that.
    files = sorted(set(pkg.rglob("*.py")) | set(root.glob("*.py")))
    return root, files


def conflicted_modules(package_dir=None):
    """[(path, lineno, stripped_line)] for merge-conflict markers in the checkout's python modules.

    Scans every `*.py` under the package dir and `*.py` at the checkout root. Reads files; imports nothing."""
    root, files = _scan_files(package_dir)
    out = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if MARKER.match(line):
                out.append((f, n, line.strip()))
    return out


def _rel(f, root):
    try:
        return str(Path(f).relative_to(root))
    except ValueError:
        return str(f)


def conflict_report(package_dir=None):
    """The multi-line message for a checkout with merge-conflict markers, or None when it is clean."""
    root, _ = _scan_files(package_dir)
    conflicts = conflicted_modules(package_dir)
    if not conflicts:
        return None
    lines = [f"ab: checkout at {root} has unresolved merge-conflict markers:"]
    lines += [f"  {_rel(f, root)}:{n}  {line}" for f, n, line in conflicts]
    lines.append("ab: resolve the merge (git status, git checkout --ours/--theirs, git merge --abort) and retry.")
    return "\n".join(lines)


def syntax_problem(package_dir=None):
    """None when every scanned module compiles, else one line naming the first SyntaxError.

    Used by the runner (cached on the files' size/mtime) to refuse to claim cards from a checkout that cannot import."""
    root, files = _scan_files(package_dir)
    for f in files:
        try:
            compile(f.read_text(errors="replace"), str(f), "exec")
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} ({_rel(f, root)}, line {e.lineno})"
        except (OSError, ValueError):
            continue
    return None


def unloadable_reason(package_dir=None):
    """None when the checkout is loadable, else one line saying why: conflict markers first, then any SyntaxError."""
    for f, n, line in conflicted_modules(package_dir):
        return f"unresolved merge-conflict markers in {_rel(f, _scan_files(package_dir)[0])}:{n} ({line})"
    return syntax_problem(package_dir)


def version_string():
    """The installed package version, without importing `agentboard.cli` (so `ab --version` works on a conflicted tree)."""
    try:
        from importlib.metadata import version
        return version("agentboard")
    except Exception:
        pass
    try:
        import agentboard
        return getattr(agentboard, "__version__", "unknown")
    except Exception:
        return "unknown"
