# codex rm -f guard
Cause: the guard is inside the codex CLI (rejects any `rm -f`/`rm -rf` shell command); agentboard cannot relax it.
Fix: `ab rm [-r] [-f] PATH...` (agentboard/cli.py), limited to $AB_WORKDIR (runner exports it; else git toplevel);
refuses outside paths, the root, .git, `..` escapes; removes a symlink, never its target. Brief lists it (runner.py BRIEF). Test: tests/test_rm.py.
- [x] milestone 1 done. Open: day-7 bin/lab-run-errors scan for the `rm -f style` signature (needs new runs).
