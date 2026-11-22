"""Console entry point for `ab` / `agentboard`: check the checkout before importing the CLI.

`agentboard.cli` is the biggest and most-edited module, so it is the one an unfinished merge breaks: importing it raises
`SyntaxError` and every `ab` invocation on the host dies with a bare traceback. Here the preflight runs first, so the
cause is named instead. Nothing from the package is imported at module level (a conflicted `__init__` would defeat it).

`--version` and `guide` keep working on a conflicted checkout (they are the commands a lost agent reaches for first);
everything else, including `--help`, refuses with exit 2 until the merge is resolved.
"""
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent


def _first_line(exc):
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}".strip()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    first = argv[0] if argv else ""
    from . import _sanity
    # Post-parse exceptions: no DB, no CLI import, so they work even when cli.py itself has markers in it.
    if len(argv) == 1 and first in ("--version", "-V"):
        print(f"ab {_sanity.version_string()}")
        return 0
    if len(argv) == 1 and first == "guide":
        g = PKG / "GUIDE.md"
        if g.exists():
            print(g.read_text(), end="")
            return 0
    report = _sanity.conflict_report(PKG)
    if report:
        print(report, file=sys.stderr)
        return 2
    try:
        from .cli import main as cli_main
    except Exception as e:  # the checkout cannot import at all (any SyntaxError, or a broken import)
        print(f"checkout_unloadable: {_first_line(e)}", file=sys.stderr)
        return 2
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
