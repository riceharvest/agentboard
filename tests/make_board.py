"""Fill an AB_HOME with a generated board: `AB_HOME=/tmp/x python tests/make_board.py [N_TASKS]`.
Creates tasks (deps, labels, priorities, tiers), closed tasks, events, a second paused board, a watcher, a schedule,
caps with a stored plan reading, a cooldown, and one finished run whose output file has ANSI colours."""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentboard import store  # noqa: E402

store.WHY.set("generated board fixture")  # audited changes: a fixture is not an anonymous agent


def main(n=2000):
    random.seed(7)
    c = store.connect()
    if not store.board_row(c, "web"):
        store.board_create(c, "web", "gen", "second board", max_slots=1)
        store.board_update(c, "web", "gen", paused=1)
    ids = []
    with store.tx(c):
        for i in range(n):
            deps = random.sample(ids, k=min(len(ids), random.choice([0, 0, 0, 1, 2])))
            tid = store.add_task(c, "gen", f"generated task {i}", body="x" * 300, channel=random.choice(["main", "dev", "www/seo"]),
                                 labels={"kind": random.choice(["a", "b"]), "owner": "someone"} if i % 3 == 0 else {},
                                 priority=random.randint(0, 5), deps=deps, tier=random.choice(["low", "medium", "high", "max"]),
                                 harness="claude" if i % 17 == 0 else None, runnable=(i % 10 != 0),
                                 workdir=f"/tmp/work/{i % 40}", board="web" if i % 5 == 0 else "main", _in_tx=True,
                                 not_before=time.time() + 3600 if i % 11 == 0 else None)
            ids.append(tid)
        # closed tasks and a claimed one
        for i, tid in enumerate(ids[: n // 4]):
            st = ("done", "dead", "cancelled")[i % 3]
            c.execute("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?", (st, "result text " * 40, time.time() - i, tid))
        for i in range(300):
            store.event(c, ids[i], "gen", "message", {"msg": i, "body": f"event {i}"})
    store.set_limit(c, "bucket:claude-sub", "limit", time.time() + 1800, "five hour limit")
    with store.tx(c):
        store.cfg_set(c, "caps", {"claude-sub": {"window": "5h", "max_output": 400000, "max_5h_pct": 80, "pace_7d_lead_h": 12}}, "gen")
        c.execute("INSERT INTO schedules(name,kind,spec,action,enabled,created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                  ("always", "window", json.dumps({"from": "00:00", "to": "23:59", "days": None}), json.dumps({"slots": 3}), "gen", time.time()))
        c.execute("INSERT INTO watchers(board,name,spec,action,enabled,last_event_id,created_by,created_at) VALUES(?,?,?,?,1,0,?,?)",
                  ("main", "on-dead", json.dumps({"kinds": ["dead"], "all_boards": True}),
                   json.dumps({"brief": "look at it", "tier": "high", "cooldown": "5m"}), "gen", time.time()))
        reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
        reset5 = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        c.execute("INSERT INTO control(key,value) VALUES('_plan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps({"ts": time.time() - 40, "data": {"five_hour": 31.0, "seven_day": 62.0, "five_hour_resets": reset5,
                                                                  "seven_day_resets": reset}}),))
        buckets = ["claude-sub", "local", "opencode-free", "agy-gemini", "pi-default"]
        for i in range(12000):  # a busy week of usage rows (a real board has about 10,000 in 7 days)
            c.execute("INSERT OR IGNORE INTO usage(source,key,ts,harness,bucket,model,input,output) VALUES('gen',?,?,?,?,?,100,500)",
                      (str(i), time.time() - i * 50, "claude", buckets[i % 5], "some-model"))
        # one finished run with an out file full of ANSI
        runs = store.HOME / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"\x1b[1;32mline {i}\x1b[0m \x1b[38;5;208mõé\x1b[0m\n" for i in range(500))
        (runs / "1.out").write_bytes(lines.encode())
        (runs / "1.err").write_bytes(b"\x1b]0;title\x07warning: something\n")
        c.execute("INSERT INTO runs(id,task_id,harness,model,bucket,pid,started_at,ended_at,outcome,out_path,err_path) "
                  "VALUES(1,?,?,?,?,1,?,?,?,?,?)", (ids[-1], "claude", "sonnet", "claude-sub", time.time() - 100, time.time() - 50, "failed",
                                                    str(runs / "1.out"), str(runs / "1.err")))
    c.close()
    print(f"made {n} tasks in {store.DB_PATH}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
