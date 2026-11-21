"""`ab version` prints the install; the dirty-tree refusal of `ab done` names it."""
import json
import re
import subprocess


def test_version_fields(abx):
    code, o, e = abx("version")
    assert code == 0, (o, e)
    assert re.search(r"code_version: [0-9a-f]{10}", o)
    assert "install_path:" in o and "package_path:" in o and "checkout_head:" in o
    d = json.loads(abx("--json", "version")[1])
    assert set(d) >= {"code_version", "install_path", "package_path", "checkout_head"}


def test_done_dirty_refusal_names_install(abx, tmp_path):
    wd = tmp_path / "shared"; wd.mkdir()
    subprocess.run(["git", "-C", str(wd), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(wd), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    b = json.loads(abx("--json", "add", "B")[1])["id"]
    assert abx("claim", b)[0] == 0
    (wd / "mine.txt").write_text("x")
    code, o, e = abx("done", b, "x", env={"AB_WORKDIR": str(wd)})
    t = o + e
    v = json.loads(abx("--json", "version")[1])
    assert code != 0 and "mine.txt" in t
    assert v["code_version"] in t and "install" in t and "predate" in t
