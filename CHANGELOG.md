# Changelog

## Unreleased

- Runner auto-worktree: when a ready card's workdir is a busy git checkout, the runner automatically starts it in a per-task git worktree (`ab/<task>` from `HEAD`) unless disabled per card (`-l worktree=off`) or globally (`ab config set auto_worktree false`). `ab ls --ready` reports `workdir busy (auto worktree off)` only when a card actually waits.
- Worker env hygiene: the runner exports `AB_RUN_HOME`, and `ab` strips `AB_RUN`, `AB_ACTOR`, `AB_BOARD`, and `AB_TASK` whenever invoked with a different `AB_HOME` than the worker's own board.
- `ab-tui`: show per-bucket pacer launch budgets (`launch_budget` / `budget_why`) in Usage, surface and toggle `pacer` (`P`) and `auto_worktree` (`w`) in Ops (and `:pacer` / `:auto_worktree` in the palette), render intended negative outcomes (`labels.outcome=negative`) with a neutral `NEG` badge excluded from dead-card failure counts and red glitch effects, and display worktree metadata (`worktree_branch`, `worktree_dir`, `worktree=off`) and ready `wait_reason` in Card detail.
