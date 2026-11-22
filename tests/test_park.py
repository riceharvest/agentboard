"""`ab park`: free the slot while a long external job runs, resume with a fresh agent.
Throwaway AB_HOME, fake `claude` that starts a detached job, parks on it and exits; the resume run
(checks its brief for RESUMING AFTER PARK) finishes the card. Also file parks, reload survival,
wake-condition unit checks and the no-attempt guarantee."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env

FAKE_CLAUDE = r"""#!/bin/sh
echo "$@" > "$FAKE_ARGS.$$"
brief=""
prev=""
for a in "$@"; do
  [ "$prev" = "-p" ] && brief="$a"
  prev="$a"
done
case "$brief" in
  *"JUST SLEEP"*) sleep 5; ab done "$AB_TASK" "other work done"; exit 0 ;;
  *"RESUMING AFTER PARK"*)
    echo "resumed: reading the job output and finishing"
    ab done "$AB_TASK" "resumed after park and finished"
    exit 0 ;;
esac
if [ -n "$PARK_REPLY_Q" ]; then
  th=$(ab --json ask main "$PARK_REPLY_Q" --wait 0s | python3 -c 'import json,sys; print(json.load(sys.stdin)["thread"])')
  echo "$th" > "$PARK_THREADFILE"
  ab park "$AB_TASK" --reply "$th" --note "$PARK_NOTE"
  exit 0
fi
if [ -n "$PARK_FILE" ]; then
  echo "parking on file $PARK_FILE"
  ab park "$AB_TASK" --file "$PARK_FILE" --grace "${PARK_GRACE:-2s}" --note "$PARK_NOTE"
else
  rm -f "$PARK_PIDFILE"
  setsid sh -c 'echo $$ > "$PARK_PIDFILE"; exec sleep "$PARK_SLEEP"' >/dev/null 2>&1 &
  i=0
  while [ ! -s "$PARK_PIDFILE" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done
  pid=$(cat "$PARK_PIDFILE")
  echo "parking on pid $pid"
  ab park "$AB_TASK" --pid "$pid" --note "$PARK_NOTE"
fi
echo "parked, exiting cleanly"
exit 0
"""

NOTE = "eval job runs detached; when it ends read its output and report the score"


@pytest.fixture
def rig(abx, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE)
    (fake / "claude").chmod(0o755)
    (fake / "ab").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/ab" "$@"\n')  # this checkout's ab, not an installed one (quoted: worktree paths may contain spaces)
    (fake / "ab").chmod(0o755)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home),
           "FAKE_ARGS": str(tmp_path / "args"), "PARK_NOTE": NOTE,
           "PARK_SLEEP": "20", "PARK_PIDFILE": str(tmp_path / "job.pid"),
           "PARK_FILE": "", "PARK_GRACE": "2s"}

    def j(*args, rc=0):
        code, out, err = abx("--json", *args, env=env)
        assert code == rc, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999")):
        j("config", "set", k, v)
    j("tiers", "set", "medium", "claude:haiku")
    j("runner", "pause")
    procs = []

    def start(slots="1"):
        logf = open(tmp_path / "runner.log", "ab")
        p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                             stdout=logf, stderr=logf, start_new_session=True)
        procs.append(p)
        j("runner", "slots", slots)
        j("runner", "resume")
    j.start = start
    j.env = env
    j.tmp = tmp_path
    yield j
    try:
        j("runner", "stop", "--now")
    except AssertionError:
        pass
    for p in procs:
        p.terminate()


def wait(cond, secs=45):
    for _ in range(secs * 2):
        if cond():
            return True
        time.sleep(0.5)
    return False


def parked(j, tid):
    t = j("show", tid)
    return t["status"] == "open" and t.get("park") and not t["park"].get("woken_at")


def test_park_pid_frees_slot_and_new_run_resumes(rig, abx):
    """Card A starts `sleep 20` detached and parks on its pid: the slot frees immediately (card B starts),
    after the sleep ends A becomes ready and a NEW run finishes it with the park note in its brief.
    Attempts stay 0; the parked run's outcome is `parked`."""
    j = rig
    a = j("add", "long eval", "--body", "Run the long eval.", "--workdir", str(j.tmp / "wa"))["id"]
    b = j("add", "other work", "--body", "JUST SLEEP", "--workdir", str(j.tmp / "wb"))["id"]
    j.start(slots="1")
    assert wait(lambda: parked(j, a)), j("show", a)
    t = j("show", a)
    assert t["state"] == "running" and t["attempts"] == 0 and t["owner"] is None  # a pid/file park counts as running (ae0c111)
    pid = t["park"]["pid"]
    assert t["park"]["note"] == NOTE and t["park"]["run"] == t["runs"][0]["id"]
    assert wait(lambda: j("show", a)["runs"][0]["outcome"] == "parked"), j("show", a)["runs"]
    os.kill(pid, 0)  # the detached job really runs
    # the slot freed at once: B starts while A's job still sleeps (and finishes, freeing the slot again)
    assert wait(lambda: j("show", b)["status"] == "claimed", secs=10), j("show", b)
    assert j("show", a)["attempts"] == 0
    assert wait(lambda: j("show", b)["status"] == "done", secs=15), j("show", b)
    assert parked(j, a)  # A still waits on its job even though the slot is free again
    # ls / snapshot show what the parked card waits on
    code, txt, err = abx("ls", env=j.env)
    assert code == 0 and f"parked: pid {pid}" in txt, txt
    snap = j("snapshot")
    sa = next(x for x in snap["tasks"] if x["id"] == a)
    assert sa["state"] == "running" and sa["park"]["pid"] == pid
    assert a not in [x["id"] for x in j("ls", "--ready")["items"]]
    # after the sleep ends the card wakes and a NEW run resumes it (it may finish between two polls)
    assert wait(lambda: j("show", a)["status"] in ("claimed", "done")), (j("show", a), open(j.tmp / "runner.log").read())
    assert wait(lambda: j("show", a)["status"] == "done"), j("show", a)
    t = j("show", a)
    assert t["attempts"] == 0 and len(t["runs"]) == 2 and t["runs"][1]["id"] != t["runs"][0]["id"]
    t = j("show", a)
    assert "resumed after park" in t["result"]
    assert wait(lambda: [r["outcome"] for r in j("show", a)["runs"]] == ["parked", "agent_done"]), j("show", a)["runs"]
    assert t["attempts"] == 0
    # the resume run's brief carried the park note and the parked run's last output lines
    briefs = [open(os.path.join(str(j.tmp), f)).read() for f in os.listdir(j.tmp) if f.startswith("args.")]
    resume = [x for x in briefs if "RESUMING AFTER PARK" in x]
    assert len(resume) == 1, briefs
    assert NOTE in resume[0] and "LAST OUTPUT LINES OF THE PARKED RUN" in resume[0]
    assert "parked, exiting cleanly" in resume[0]  # tail of run 1's stdout
    assert f"pid {pid} exited" in resume[0]


def test_park_file_wakes_when_file_appears(rig, abx):
    """--file park: stays waiting while the file is absent, wakes after it appears and is stable for grace."""
    j = rig
    out = j.tmp / "results.json"
    j.env["PARK_FILE"] = str(out)
    j.env["PARK_GRACE"] = "2s"
    a = j("add", "score when results land", "--workdir", str(j.tmp / "wa"))["id"]
    j.start(slots="1")
    assert wait(lambda: parked(j, a)), j("show", a)
    t = j("show", a)
    assert t["park"]["file"] == str(out) and t["park"]["grace_s"] == 2.0
    assert wait(lambda: j("show", a)["runs"][0]["outcome"] == "parked")
    assert t["attempts"] == 0
    time.sleep(4)
    assert parked(j, a)  # no file yet: still waiting
    assert a not in [x["id"] for x in j("ls", "--ready")["items"]]
    out.write_text('{"score": 0.9}\n')
    assert wait(lambda: j("show", a)["status"] in ("claimed", "done")), j("show", a)
    assert wait(lambda: j("show", a)["status"] == "done"), j("show", a)
    t = j("show", a)
    assert t["attempts"] == 0 and "resumed after park" in t["result"]
    assert wait(lambda: [r["outcome"] for r in j("show", a)["runs"]] == ["parked", "agent_done"]), j("show", a)["runs"]


def test_park_survives_runner_reload(rig):
    """A reload while parked keeps the park (it lives on the card, not in the runner or an agent)."""
    j = rig
    out = j.tmp / "results.json"
    j.env["PARK_FILE"] = str(out)
    j.env["PARK_GRACE"] = "1s"
    a = j("add", "parked across reload", "--workdir", str(j.tmp / "wa"))["id"]
    j.start(slots="1")
    assert wait(lambda: parked(j, a)), j("show", a)
    before = j("show", a)["park"]
    cursor = max(e["id"] for e in j("log", "--limit", "5"))
    assert j("runner", "reload")["action"] == "reload"
    # reload re-execs in place (same pid): the new process logs runner_start again
    assert wait(lambda: any(e["kind"] == "runner_start" and e["id"] > cursor for e in j("log", "--limit", "30")), secs=20)
    assert j("runner", "status")["running"]
    time.sleep(3)
    t = j("show", a)
    assert t["state"] == "running" and t["attempts"] == 0
    assert t["park"]["file"] == before["file"] and not t["park"].get("woken_at")
    out.write_text("done\n")
    assert wait(lambda: j("show", a)["status"] in ("claimed", "done")), j("show", a)
    assert wait(lambda: j("show", a)["status"] == "done"), j("show", a)
    assert j("show", a)["attempts"] == 0


# ---------------------------------------------------------------- unit checks (no runner process)
def _conn(abx):
    from agentboard import store
    return store.connect(abx.home / store.DB_PATH.name)


def test_park_reply_wakes_with_the_reply_in_the_brief(rig):
    """--reply park: the run exits at once (slot frees), our own and the runner's posts do not wake it,
    a reply from someone else does, and the fresh agent's brief carries the reply text."""
    j = rig
    tf = j.tmp / "thread.txt"
    j.env["PARK_REPLY_Q"] = "which region should the eval use?"
    j.env["PARK_THREADFILE"] = str(tf)
    a = j("add", "needs an answer", "--workdir", str(j.tmp / "wa"))["id"]
    j.start(slots="1")
    assert wait(lambda: parked(j, a)), j("show", a)
    t = j("show", a)
    thread = tf.read_text().strip()
    assert t["attempts"] == 0 and t["park"]["reply_thread"] == thread
    assert wait(lambda: j("show", a)["runs"][0]["outcome"] == "parked"), j("show", a)["runs"]
    time.sleep(3)
    assert parked(j, a)  # no reply yet: still waiting, no slot held
    j("--as", "overseer#9", "reply", thread, "use eu-west, the budget is capped at 5 dollars")
    assert wait(lambda: j("show", a)["status"] in ("claimed", "done")), j("show", a)
    assert wait(lambda: j("show", a)["status"] == "done"), j("show", a)
    t = j("show", a)
    assert t["attempts"] == 0 and len(t["runs"]) == 2
    briefs = [open(os.path.join(str(j.tmp), f)).read() for f in os.listdir(j.tmp) if f.startswith("args.")]
    resume = [x for x in briefs if "RESUMING AFTER PARK" in x]
    assert len(resume) == 1 and "use eu-west, the budget is capped at 5 dollars" in resume[0], briefs
    assert "overseer#9" in resume[0]


def test_park_validation_and_permissions(abx):
    from agentboard import store
    tid = abx("--json", "add", "x")
    tid = json.loads(tid[1])["id"]
    abx("--json", "claim", tid)  # actor: agent
    # note required, exactly one condition, pid must be alive, claim must be held
    for args, msg in ((["park", tid, "--pid", str(os.getpid())], "--note"),
                      (["park", tid, "--pid", str(os.getpid()), "--file", "/tmp/f", "--note", "n"], "exactly one"),
                      (["park", tid, "--pid", "999999", "--note", "n"], "not running"),
                      (["park", tid, "--note", "n"], "--pid"),
                      (["park", tid, "--reply", "m99999", "--note", "n"], "no thread")):
        code, out, err = abx("--json", *args)
        assert code == 1 and msg in out + err, (args, out, err)
    code, out, err = abx("--json", "--as", "someone-else", "park", tid, "--file", "/tmp/f", "--note", "n")
    assert code == 1 and "held by" in out + err
    # force works for a manager
    code, out, err = abx("--json", "--as", "boss", "park", tid, "--file", "/tmp/f", "--grace", "0", "--note", "forced", "--force")
    assert code == 0, err
    c = _conn(abx)
    try:
        t = store.task_dict(c, store.must_task(c, tid))
        assert t["state"] == "running" and t["attempts"] == 0 and t["owner"] is None
        assert t["park"]["note"] == "forced"
        assert store.ready_tasks(c) == []
        # cancel clears the park
        store.cancel(c, tid, "agent", "stop waiting")
        assert store.must_task(c, tid)["park"] is None
    finally:
        c.close()


def test_wake_conditions_pid_starttime_and_file_growth(abx, tmp_path):
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    abx("--json", "claim", tid)
    f = tmp_path / "out.log"
    abx("--json", "park", tid, "--file", str(f), "--grace", "1s", "--note", "wait for out.log")
    c = _conn(abx)
    try:
        assert runner.wake_parked(c) == []  # absent
        f.write_text("line 1\n")
        assert runner.wake_parked(c) == []  # first sight: stability window starts
        f.write_text("line 1\nline 2\n")  # still growing resets the window
        assert runner.wake_parked(c) == []
        time.sleep(1.2)
        w = runner.wake_parked(c)
        assert w and w[0][0] == tid and "stable" in w[0][1]
        assert store.must_task(c, tid)["status"] == "open"
        assert [r["id"] for r in store.ready_tasks(c)] == [tid]
        assert runner.wake_parked(c) == []  # already woken
        # the brief carries the note and the wake reason
        brief = runner.build_brief(c, store.must_task(c, tid), str(tmp_path), 1, "claude#9")
        assert "RESUMING AFTER PARK" in brief and "wait for out.log" in brief and "stable" in brief
    finally:
        c.close()
    # pid park: alive pid with matching start time stays parked; a reused pid (start mismatch) wakes
    tid2 = json.loads(abx("--json", "add", "y")[1])["id"]
    abx("--json", "claim", tid2)
    abx("--json", "park", tid2, "--pid", str(os.getpid()), "--note", "wait for me")
    c = _conn(abx)
    try:
        assert runner.wake_parked(c) == []
        spec = json.loads(store.must_task(c, tid2)["park"])
        spec["pid_start"] = "1"  # same pid, different start time: pid was reused
        c.execute("UPDATE tasks SET park=? WHERE id=?", (json.dumps(spec), tid2))
        w = runner.wake_parked(c)
        assert w and w[0][0] == tid2 and "exited" in w[0][1]
    finally:
        c.close()


def test_wake_waits_for_the_parking_run_to_exit(abx, tmp_path):
    """The card must not wake (and double-start) while the agent that parked it is still exiting."""
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    abx("--json", "claim", tid)
    f = tmp_path / "out.log"
    f.write_text("x\n")
    abx("--json", "park", tid, "--file", str(f), "--grace", "0", "--note", "n")
    c = _conn(abx)
    try:
        c.execute("INSERT INTO runs(task_id,harness,started_at) VALUES(?,?,?)", (tid, "claude", store.now()))
        assert runner.wake_parked(c) == []  # a live run still holds the card
        c.execute("UPDATE runs SET ended_at=? WHERE task_id=?", (store.now(), tid))
        assert runner.wake_parked(c) and store.ready_tasks(c)
    finally:
        c.close()


def test_parked_run_outcome_is_parked_not_an_attempt(abx, tmp_path):
    """finalize(): a run that parked its card ends `parked` even on a non-zero exit, attempts unchanged."""
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    abx("--json", "claim", tid)  # owner: agent
    c = _conn(abx)
    try:
        c.execute("UPDATE tasks SET owner='claude#1' WHERE id=?", (tid,))
        out = tmp_path / "1.out"
        out.write_text("parked, exiting\n")
        c.execute("INSERT INTO runs(id,task_id,harness,bucket,pid,started_at,out_path,err_path) VALUES(1,?,?,?,999999,?,?,?)",
                  (tid, "claude", "claude-sub", store.now(), str(out), str(tmp_path / "1.err")))
        store.park(c, tid, "claude#1", "job running", pid=os.getpid(), pid_start=runner.pid_start(os.getpid()))
        ch = runner.Child(1, tid, "claude#1", "claude", None, "claude-sub", 999999, None, None, 3600,
                          str(tmp_path), str(out), str(tmp_path / "1.err"), store.now())
        ch.board, ch.endpoint, ch.provider = "main", None, None
        runner.Runner(c).finalize(ch, 1)  # non-zero exit
        r = c.execute("SELECT outcome FROM runs WHERE id=1").fetchone()
        assert r["outcome"] == "parked"
        t = store.must_task(c, tid)
        assert t["status"] == "open" and t["attempts"] == 0 and store.park_active(t["park"])
    finally:
        c.close()


def test_mcp_exposes_park():
    from agentboard import mcp
    assert "ab_park" in mcp.TOOLS
    argv = mcp.to_argv("ab_park", {"id": "t_1", "pid": 42, "note": "n"})
    assert argv[:2] == ["--json", "park"] and "--pid" in argv and "42" in argv


def test_retry_brief_shows_earlier_notes(abx, tmp_path):
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "retry me")[1])["id"]
    abx("note", tid, "attempt 1: tried X")
    c = _conn(abx)
    try:
        brief = runner.build_brief(c, store.must_task(c, tid), str(tmp_path), 2, "claude#9")
        assert "NOTES SO FAR" in brief and "attempt 1: tried X" in brief
    finally:
        c.close()


def test_park_resumed_accepts_claimed_or_done_between_polls(tmp_path, monkeypatch):
    """hermes#162 race: a woken card can be claimed and finished between two polls, leaving status=done and no park
    spec. The wake check must still count it as resumed (claimed OR done after the park), and never while the
    park is the newest state."""
    monkeypatch.setenv("AB_HOME", str(tmp_path))
    import subprocess
    from agentboard import store, runner
    c = store.connect()
    tid = store.add_task(c, "me", "t", "b")
    tid = tid if isinstance(tid, str) else tid[0]
    store.claim(c, "me", tid=tid)
    p = subprocess.Popen(["sleep", "0.2"])
    store.park(c, tid, "me", "x", pid=p.pid)
    assert not store.park_resumed(c, tid)
    p.wait()
    assert runner.wake_parked(c) and not store.park_resumed(c, tid)  # woken is not resumed yet
    store.claim(c, "r", tid=tid)
    assert store.park_resumed(c, tid)  # claimed
    store.complete(c, tid, "r", "ok")  # finished before any poll
    row = c.execute("SELECT status, park FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "done" and row["park"] is None  # state alone shows nothing
    assert store.park_resumed(c, tid)


def test_no_double_dispatch_while_a_run_is_live(abx):
    """A card re-opened (parked / released) while its run is still alive is never given a second run."""
    import subprocess
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    p = subprocess.Popen(["sleep", "30"])
    c = _conn(abx)
    try:
        c.execute("INSERT INTO runs(id,task_id,harness,pid,pid_start,started_at) VALUES(1,?,?,?,?,?)",
                  (tid, "claude", p.pid, runner.pid_start(p.pid), store.now()))
        r = runner.Runner(c)
        assert store.ready_tasks(c) and r.has_live_run(tid)
        assert r.fill(4) is None
        assert c.execute("SELECT COUNT(*) FROM runs WHERE task_id=?", (tid,)).fetchone()[0] == 1
        assert store.must_task(c, tid)["status"] == "open"
    finally:
        c.close()
        p.kill()
        p.wait()


def test_tick_renews_a_live_childs_lease_instead_of_burning_an_attempt(abx):
    """A slow tick must not let reap() fail the attempt of a card whose run the runner still holds."""
    from agentboard import runner, store
    tid = json.loads(abx("--json", "add", "x")[1])["id"]
    abx("--json", "claim", tid)
    c = _conn(abx)
    try:
        c.execute("UPDATE tasks SET owner='claude#1', lease_until=? WHERE id=?", (store.now() - 5, tid))
        r = runner.Runner(c)
        r.children[1] = ch = runner.Child(1, tid, "claude#1", "claude", None, "b", os.getpid(), None, None, 3600, "/", "", "", store.now())
        ch.board = "main"
        try:
            r.tick()  # may fail later on (no harness); the renewal runs before reap
        except Exception:
            pass
        t = store.must_task(c, tid)
        assert t["attempts"] == 0 and t["status"] == "claimed" and t["lease_until"] > store.now()
    finally:
        c.close()
