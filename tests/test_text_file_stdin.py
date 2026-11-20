"""`--file -` reads stdin for every text option (note/done/fail/add --body-file), and a missing
real path is a one-line error, not a FileNotFoundError traceback (AB_FRICTION hermes#274).
Throwaway AB_HOME via the abx fixture: each `ab` runs in a fresh subprocess with AB_HOME set
before the store is imported."""
import json


def add(abx, title="t"):
    code, out, err = abx("--json", "add", title)
    assert code == 0, (code, out, err)
    return json.loads(out)["id"]


def test_note_file_dash_reads_stdin(abx):
    tid = add(abx)
    code, out, err = abx("--json", "note", tid, "--file", "-", stdin="x")
    assert code == 0, (code, out, err)
    d = json.loads(abx("--json", "show", tid)[1])
    assert d["notes"][-1]["body"] == "x"


def test_done_and_fail_file_dash_read_stdin(abx):
    tid = add(abx, "done card")
    assert abx("claim", tid)[0] == 0
    code, out, err = abx("--json", "done", tid, "--summary-file", "-", stdin="result $0 `tick`")
    assert code == 0, (code, out, err)
    d = json.loads(abx("--json", "show", tid)[1])
    assert d["status"] == "done" and d["result"] == "result $0 `tick`"

    tid = add(abx, "fail card")
    assert abx("claim", tid)[0] == 0
    code, out, err = abx("--json", "fail", tid, "--why-file", "-", stdin="why it failed")
    assert code == 0, (code, out, err)
    d = json.loads(abx("--json", "show", tid)[1])
    assert d["notes"][-1]["body"] == "FAILED: why it failed"


def test_add_body_file_dash_reads_stdin(abx):
    code, out, err = abx("--json", "add", "body card", "--body-file", "-", stdin="line1\nline2 $x")
    assert code == 0, (code, out, err)
    d = json.loads(abx("--json", "show", json.loads(out)["id"])[1])
    assert d["body"] == "line1\nline2 $x"


def test_missing_file_is_one_line_error(abx, tmp_path):
    tid = add(abx)
    missing = str(tmp_path / "no-such-file.txt")
    for args in (("note", tid, "--file", missing),
                 ("done", tid, "--summary-file", missing),
                 ("fail", tid, "--why-file", missing),
                 ("add", "x", "--body-file", missing)):
        code, out, err = abx(*args)
        assert code == 1, (args, code, out, err)
        assert "Traceback" not in out + err, (args, out, err)
        assert err.startswith("error: ") and err.count("\n") == 1, (args, err)
        assert missing in err


def test_real_file_path_still_works(abx, tmp_path):
    tid = add(abx)
    f = tmp_path / "note.txt"
    f.write_text("from a real file")
    code, out, err = abx("--json", "note", tid, "--file", str(f))
    assert code == 0, (code, out, err)
    d = json.loads(abx("--json", "show", tid)[1])
    assert d["notes"][-1]["body"] == "from a real file"
