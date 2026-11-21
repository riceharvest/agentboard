# ab done attribution
M1 done: claim-time dirty_snapshot event + hash compare (agentboard/cli.py). M2 done: tests/test_done_dirty_concurrent.py (2 pass), test_done_dirty.py 4 pass. Full suite: 176 passed, failures pre-existing (test_limit_resume, test_park, test_worker_env; same fail on HEAD~1 cli.py).
Caveat: a path NEW since claim is ignored only while another actor holds/held a claim overlapping this one; solo cards still refuse on own new files. Snapshot only taken when AB_WORKDIR is set at `ab claim`; otherwise legacy mtime rule.
