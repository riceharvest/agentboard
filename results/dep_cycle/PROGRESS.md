# dep_cycle progress (t_94a3b58e)
- M1 add-time refusal: DONE. `ab edit --dep` refuses a cycle, prints `t_a -> t_b -> t_a`, `--allow-dep-cycle WHY` forces it (event dep_cycle_allowed). `ab add --dep` cannot close a cycle (new card has no dependents), so it needs no flag.
- M2 read-side: DONE. hold reason `dep_cycle` (store.hold_reason, task_dict live, snapshot `hold`, `ab ls --in-flight` bucket `dep_cycle N`).
- Test: tests/test_dep_cycle.py. Live step: `ab runner reload` (HANDOFF: admin actor).
