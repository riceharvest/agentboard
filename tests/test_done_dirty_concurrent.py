"""Claim-time dirty snapshot: other cards' edits after our claim are not ours; our own edits to an already-dirty file are."""
import json
import subprocess


def _shared(tmp_path):
    wd = tmp_path / "shared"; wd.mkdir()
    subprocess.run(["git", "-C", str(wd), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(wd), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    return wd


def _add(abx, t):
    return json.loads(abx("--json", "add", t)[1])["id"]


def test_other_card_edit_after_claim_is_ignored(abx, tmp_path):
    wd = _shared(tmp_path); env = {"AB_WORKDIR": str(wd)}
    a, b = _add(abx, "A"), _add(abx, "B")
    assert abx("claim", a, "--as", "actA", env=env)[0] == 0
    assert abx("claim", b, "--as", "actB", env=env)[0] == 0
    (wd / "other.txt").write_text("b wrote this after a claimed")
    code, o, e = abx("done", a, "x", "--as", "actA", env=env)
    assert code == 0, (code, o, e)
    assert "ignored" in (o + e) and "other.txt" in (o + e)


def test_edit_of_already_dirty_file_is_mine(abx, tmp_path):
    wd = _shared(tmp_path); env = {"AB_WORKDIR": str(wd)}
    (wd / "pre.txt").write_text("v1")
    a = _add(abx, "A")
    assert abx("claim", a, "--as", "actA", env=env)[0] == 0
    (wd / "pre.txt").write_text("v2 changed by this task")
    code, o, e = abx("done", a, "x", "--as", "actA", env=env)
    assert code != 0 and "pre.txt" in (o + e), (code, o, e)
    assert abx("done", a, "x", "--allow-dirty", "--as", "actA", env=env)[0] == 0
