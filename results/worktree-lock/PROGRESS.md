# worktree=on must not wait on the shared workdir lock

Cause: the runner pick loop already sent `worktree=on` cards into a per-task git worktree and never
waited on the shared checkout (`runner.py` fill(): `elif explicit: ... wt = True`), but the hold
*reporting* path (`store.hold_reason()`) never consulted `worktree`/`explicit`, so such a card was
still labelled `workdir_locked:<shared path>` and showed up in `ab ls --stuck` / `ab show` as stuck.
Board idle with free slots; the lock was real but irrelevant to a card that runs in its own worktree.

Fix (`agentboard/store.py`):
- `workdir_lock_exempt(conn, labels, wd)` — one rule shared by hold reporting: explicit
  `worktree=on` is exempt when the workdir is a git checkout (the runner always isolates there);
  the automatic worktree is exempt only on a git dir (it does not exist otherwise). When
  `worktree=on` cannot isolate (non-git workdir) `why` is returned and the hold reason appends
  `(worktree=on cannot isolate: ...)` so the fallback is visible. `share_workdir true` = no lock.
- `workdir_holders(conn)` — one query mapping norm workdir -> [(task id, pid)] for claimed cards
  that actually take the lock (same rule as `busy_dirs`). `record_holds()` stores `holder` on the
  `hold` event; `current_hold()` returns it.
- `agentboard/cli.py`: `tline()` (used by `ab ls --stuck`, `ab ls --ready`) appends
  `held_by <id> pid <pid>` (or `held_by runner`), `show_text()` prints `held by ...`.
  Cost: `--stuck` reads the hold event it already read; no extra filesystem probe in the listing.
- README holds section, CHANGELOG, `ab ls --stuck` help text updated.

Tests: `tests/test_worktree_isolation.py` (worktree=on not held + negative control + cannot-isolate),
`tests/test_runner_hold.py` (stuck/show name the holder). All three new behaviour tests were run
against the pre-fix tree (`git stash` of store.py/cli.py) and fail there — they are not vacuous.

## Milestones
- [x] M1 find `workdir_locked` computation + reproduce with a test
- [x] M2 exempt `worktree=on` + negative control
- [x] M3 holder in `--stuck` + suite + commit

## Verification (throwaway AB_HOME, 2026-11-22)
Target tests:
```
$ env -i PATH="$PATH" HOME="$HOME" AB_HOME=$(mktemp -d) python3 -m pytest \
    tests/test_worktree_isolation.py tests/test_runner_hold.py -q
..........                                                               [100%]
10 passed in 41.13s
```
Pre-fix control (store.py + cli.py stashed):
```
FAILED tests/test_worktree_isolation.py::test_worktree_on_not_held_by_busy_shared_dir
FAILED tests/test_worktree_isolation.py::test_worktree_on_cannot_isolate_says_so_in_hold
FAILED tests/test_runner_hold.py::test_stuck_names_the_lock_holder
3 failed in 56.77s
```
Full suite:
```
$ env -i PATH="$PATH" HOME="$HOME" AB_HOME=$(mktemp -d) python3 -m pytest tests -q
4 failed, 210 passed, 3 skipped in 875.86s (0:14:35)
```
Same 4 failures on a clean `git archive master` tree (0b40e382), so pre-existing:
tests/test_limit_resume.py::test_limit_deaths_resume_then_skip_route,
tests/test_park.py::test_park_pid_frees_slot_and_new_run_resumes,
tests/test_park.py::test_park_survives_runner_reload,
tests/test_park.py::test_park_validation_and_permissions
(4 failed, 10 passed in the clean-tree rerun of those files).

Manual repro (runner + fake harness, shared git checkout; holder = long card, plain = worktree=off,
wt = worktree=on):
```
== ab ls --ready ==
t_35ebe144  ready     p0   medium main         wt  [type=build,worktree=on]
t_70ed7b47  ready     p0   medium main         plain  [type=build,worktree=off]  (workdir busy (auto worktree off); held_by t_be55c306 pid 3389023)

== ab ls --stuck ==
t_70ed7b47  ready     p0   medium main         plain  [type=build,worktree=off]  (held_by t_be55c306 pid 3389023)
holds by reason:
  workdir_locked:.../shared: 1

== ab show t_70ed7b47 ==
hold=workdir_locked:.../shared since 11-22 00:35
held by t_be55c306 pid 3389023

== ab show t_35ebe144 (worktree=on) ==
(no hold line; listed by `ab ls --ready`)
```

## Day-7 / day-30 (distribution plan)
Day 7: a board with a held workdir shows 0 `worktree=on` cards on `workdir_locked`. Day 30: no
issue asks "why is my worktree card stuck behind another card's lock". Check:
`ab ls --all-boards --stuck --json | jq '.[] | select(.labels.worktree=="on")'` should not name
such a card under `workdir_locked`.
