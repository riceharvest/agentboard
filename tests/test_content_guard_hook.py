"""Self-test for the local pre-commit content-guard hook (false positives pass, real guards stay blocked)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT


def _find_local_hook():
    p = subprocess.run(["git", "-C", ROOT, "rev-parse", "--git-common-dir"], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    common = Path(p.stdout.strip())
    if not common.is_absolute():
        common = (Path(ROOT) / common).resolve()
    hook = common / "hooks" / "pre-commit"
    return hook if hook.is_file() else None


def _extract_word_patterns(hook_text):
    m = re.search(r"WORD_PAT='([^']+)'", hook_text)
    assert m, "pre-commit hook missing WORD_PAT"
    pat = m.group(1)
    grp = re.search(r"\\b\(([^)]+)\)\\b", pat)
    assert grp, f"WORD_PAT missing word-boundary group: {pat}"
    words = [w for w in grp.group(1).split("|") if w]
    return pat, words


@pytest.fixture
def hooked_repo(tmp_path):
    hook = _find_local_hook()
    if hook is None:
        pytest.skip("no local pre-commit hook installed")
    text = hook.read_text()
    if "WORD_PAT=" not in text:
        pytest.skip("local pre-commit hook is not the content-guard hook")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    dst = repo / ".git" / "hooks" / "pre-commit"
    shutil.copy2(hook, dst)
    dst.chmod(0o755)
    return repo, text


def _try_commit(repo, relpath, content, msg="test"):
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", relpath], check=True)
    p = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg], capture_output=True, text=True)
    if p.returncode != 0:
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "HEAD", "--", relpath], check=False)
        target.unlink(missing_ok=True)
    return p.returncode, p.stdout + p.stderr


def test_false_positives_pass(hooked_repo):
    repo, _ = hooked_repo
    # Substring of a word-bounded guard must pass
    rc, out = _try_commit(repo, "src/layout.rs", "let dir = Direction::Horizontal;\n")
    assert rc == 0, out
    # Fixture task IDs under tests/ (top-level or nested) must pass
    rc, out = _try_commit(repo, "tests/fixture.py", 'TASK_ID = "t_0123abcd"\n')
    assert rc == 0, out
    rc, out = _try_commit(repo, "tui/tests/sample.rs", 'const ID: &str = "t_deadbeef";\n')
    assert rc == 0, out


def test_real_guards_still_blocked(hooked_repo):
    repo, hook_text = hooked_repo
    _, words = _extract_word_patterns(hook_text)
    assert len(words) >= 5

    # Every guarded word is blocked in both lowercase and uppercase, outside and inside tests/
    for w in words:
        rc, out = _try_commit(repo, "agentboard/mod.py", f"note = '{w}'\n")
        assert rc != 0 and "BLOCKED" in out, f"expected {w!r} to be blocked"
        rc, out = _try_commit(repo, "agentboard/mod.py", f"note = '{w.upper()}'\n")
        assert rc != 0 and "BLOCKED" in out, f"expected {w.upper()!r} to be blocked"
        rc, out = _try_commit(repo, "tests/test_x.py", f"note = '{w}'\n")
        assert rc != 0 and "BLOCKED" in out, f"expected {w!r} inside tests/ to be blocked"

    # Task ID outside tests/ is still blocked
    rc, out = _try_commit(repo, "agentboard/mod.py", 'TASK_ID = "t_0123abcd"\n')
    assert rc != 0 and "BLOCKED" in out

    # Forbidden extensions (.db, .jsonl) are still blocked
    rc, out = _try_commit(repo, "data/sample.db", "not-really-sqlite\n")
    assert rc != 0 and "BLOCKED" in out
    rc, out = _try_commit(repo, "data/events.jsonl", "{}\n")
    assert rc != 0 and "BLOCKED" in out
