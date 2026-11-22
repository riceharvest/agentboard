import json
import subprocess
import sys
import time

from conftest import ROOT, clean_env


def test_post_only_rc0(abx):
    rc, out, _ = abx("ask", "main", "hi", "--wait", "0s", "--as", "me#1")
    assert rc == 0 and "posted m1" in out
    rc, out, _ = abx("read", "main", "--json")
    assert len(json.loads(out)) == 1


def test_post_only_json(abx):
    rc, out, _ = abx("ask", "main", "hi", "--wait", "0s", "--as", "me#1", "--json")
    d = json.loads(out)
    assert rc == 0 and d["posted"] is True and d["waiting"] is True and d["replies"] == []


def test_positive_wait_no_reply_rc3(abx):
    rc, out, _ = abx("ask", "main", "hi", "--wait", "1s", "--as", "me#1")
    assert rc == 3 and "no reply" in out


def test_reply_rc0(abx):
    p = subprocess.Popen([sys.executable, "-m", "agentboard", "ask", "main", "q", "--wait", "20s", "--as", "me#1"],
                         cwd=ROOT, env=clean_env(abx.home), stdout=subprocess.PIPE, text=True)
    time.sleep(2)
    assert abx("reply", "m1", "answer", "--as", "me#2")[0] == 0
    out, _ = p.communicate(timeout=25)
    assert p.returncode == 0 and "answer" in out


def test_default_wait_still_10m():
    from agentboard import cli
    import inspect
    assert '"--wait", default="10m"' in inspect.getsource(cli)
