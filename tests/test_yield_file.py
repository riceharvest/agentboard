"""--yield-file: while the file exists a managed endpoint gets no new runs, current runs finish, then the server gets SIGTERM;
when the file is gone the next card starts the server again. Throwaway AB_HOME, fake endpoint server, fake `claude` harness
that sleeps FAKE_SLEEP seconds (env AB_YIELD_TEST_SLEEP overrides the default 12)."""
import json
import os
import subprocess
import sys
import time

from conftest import ROOT, clean_env

SERVER = r'''import http.server, json, os, signal, sys
log = sys.argv[1]
def w(m):
    with open(log, "a") as f: f.write(m + "\n")
signal.signal(signal.SIGTERM, lambda *a: (w("server SIGTERM"), os._exit(0)))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"data": [{"id": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
w("server start")
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
'''
HARNESS = r'''#!/bin/sh
echo "run start $(date +%s) base=${ANTHROPIC_BASE_URL:-none}" >> "$FAKE_LOG"
sleep "$FAKE_SLEEP"
tid=$(echo "$2" | sed -n "s/.*agentboard task \(t_[0-9a-f]*\).*/\1/p" | head -1)
ab done "$tid" "ok" >/dev/null 2>&1
echo "run end $tid base=${ANTHROPIC_BASE_URL:-none}" >> "$FAKE_LOG"
'''


def test_yield_file(abx, tmp_path):
    sl = os.environ.get("AB_YIELD_TEST_SLEEP", "12")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text(HARNESS)
    (fake / "claude").chmod(0o755)
    (tmp_path / "server.py").write_text(SERVER)
    home = tmp_path / "fakehome"
    home.mkdir()
    flog, yf = tmp_path / "fake.log", tmp_path / "gpu.want"
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "FAKE_LOG": str(flog), "FAKE_SLEEP": sl}
    port = 18000 + os.getpid() % 1000

    def j(*args):
        code, out, err = abx("--json", *args, env=env)
        assert code == 0, (args, code, out, err)
        return json.loads(out) if out.strip() else None
    for k, v in (("pacer", "false"), ("min_free_gib", "0"), ("poll_s", "1"), ("harvest_every_s", "99999"), ("backoff", "1s")):
        j("config", "set", k, v)
    code, out, err = abx("endpoints", "add", "fake", "--base-url", f"http://127.0.0.1:{port}/v1", "--start-cmd",
                         f"{sys.executable} {tmp_path / 'server.py'} {flog} {port}", "--idle-stop", "-1", "--max-runs", "2",
                         "--yield-file", str(yf), env=env)
    assert code == 0, err
    j("tiers", "set", "medium", "claude@fake:m")
    j("runner", "pause")
    ids = []
    for i in range(4):
        w = tmp_path / f"w{i}"
        w.mkdir()
        ids.append(j("add", f"card {i}", "--body", "x", "--workdir", str(w), "--tier", "medium")["id"])
    logf = open(tmp_path / "runner.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "runner", "run"], cwd=ROOT, env=clean_env(abx.home, **env),
                         stdout=logf, stderr=logf, start_new_session=True)

    def lines():       # "run start" lines only for runs that used the endpoint (a rerouted card runs on the fallback)
        return [l for l in flog.read_text().splitlines() if "base=none" not in l] if flog.exists() else []

    def wait(pred, n=300):
        for _ in range(n):
            if pred():
                return True
            time.sleep(0.5)
        return False
    try:
        j("runner", "slots", "4")
        j("runner", "resume")
        assert wait(lambda: sum(l.startswith("run start") for l in lines()) == 2), lines()
        yf.write_text(f"{os.getpid()} test\n")            # a live pid: not stale
        assert wait(lambda: "server SIGTERM" in lines(), 400), lines()
        L = lines()
        assert sum(l.startswith("run start") for l in L) == 2, L           # no third run started
        assert L.index("server SIGTERM") > max(i for i, l in enumerate(L) if l.startswith("run end")), L   # after both ended
        yf.unlink()
        w = tmp_path / "w_after"
        w.mkdir()
        j("add", "card after", "--body", "x", "--workdir", str(w), "--tier", "medium")
        assert wait(lambda: sum(l == "server start" for l in lines()) == 2, 120), lines()
        assert wait(lambda: sum(l.startswith("run start") for l in lines()) >= 3, 120), lines()
    finally:
        try:
            j("runner", "stop", "--now")
        except AssertionError:
            pass
        p.terminate()
    print("\n".join(lines()))
    print((tmp_path / "runner.log").read_text())
