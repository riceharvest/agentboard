# whoami effective identity
- [x] 1 repro: before the fix the 4 new tests fail (KeyError actor_source; whoami printed actor=agent for a run home)
- [x] 2 effective_identity() in agentboard/cli.py; actor/board/task/run effective + actor_source/board_source
- [x] 3 four cases in tests/test_whoami_inferred.py; whoami+run_identity: 22 passed
- [x] 4 baseline (HEAD 17c17a7): 5 failed, 242 passed, 3 skipped; after: 5 failed, 246 passed, 3 skipped (same 5:
  test_limit_resume::test_limit_deaths_resume_then_skip_route, test_park x3, test_worker_env::test_launch_exports_ids_and_note_done_work_without_force)
