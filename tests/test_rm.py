"""`ab rm`: a delete path for harnesses whose shell refuses `rm -f`; only inside the run's worktree ($AB_WORKDIR)."""
import os


def test_rm_inside_and_outside(abx, tmp_path):
    wt = tmp_path / "wt"; (wt / "sub").mkdir(parents=True); (wt / ".git").mkdir()
    (wt / "a.txt").write_text("x"); (wt / "sub" / "b").write_text("y")
    out = tmp_path / "outside.txt"; out.write_text("keep")
    (wt / "link").symlink_to(out)
    env = {"AB_WORKDIR": str(wt)}
    code, o, e = abx("rm", str(wt / "a.txt"), env=env)
    assert code == 0 and not (wt / "a.txt").exists(), (o, e)
    code, o, e = abx("rm", str(wt / "sub"), env=env)
    assert code == 1 and "use -r" in e and (wt / "sub").exists()
    assert abx("rm", "-r", str(wt / "sub"), env=env)[0] == 0 and not (wt / "sub").exists()
    assert abx("rm", "-f", str(wt / "nope"), env=env)[0] == 0
    assert abx("rm", str(wt / "link"), env=env)[0] == 0 and out.read_text() == "keep" and not os.path.lexists(wt / "link")
    for bad in (out, wt, wt / ".git", wt / ".." / "outside.txt", wt / "x" / ".." / ".." / "outside.txt"):
        code, o, e = abx("rm", "-rf", str(bad), env=env)
        assert code == 1 and "refused" in e, (bad, e)
    assert out.read_text() == "keep" and (wt / ".git").is_dir()
    print(e)
