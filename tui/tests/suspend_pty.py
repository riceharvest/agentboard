"""PTY test of the suspend/resume path: `s` (shell) and `e` ($EDITOR) in the workdir of the selected card leave the alternate
screen, run the command, and come back. Real binary, throwaway AB_HOME.   python3 tui/tests/suspend_pty.py [path/to/ab-tui]"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "tui" / "target" / "release" / "ab-tui")
home = tempfile.mkdtemp(prefix="ab-tui-suspend-")
work = tempfile.mkdtemp(prefix="ab-tui-work-")
env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
env.update(AB_HOME=home, AB_BIN=str(ROOT / "ab"), AB_ACTOR="tui-test", TERM="xterm-256color", SHELL="/bin/true", EDITOR="true")
env.pop("VISUAL", None)
subprocess.run([str(ROOT / "ab"), "add", "card with a workdir", "--workdir", work], env=env, check=True, capture_output=True)

pid, fd = pty.fork()
if pid == 0:
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    os.execve(BIN, [BIN, "--board-home", home, "--no-fx"], env)
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
out = b""


def pump(seconds):
    global out
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                out += os.read(fd, 65536)
            except OSError:
                return


pump(3)
os.write(fd, b"s")
pump(2)
os.write(fd, b"e")
pump(2)
os.write(fd, b"q")
pump(1)
_, status = os.waitpid(pid, 0)
enter, leave = out.count(b"\x1b[?1049h"), out.count(b"\x1b[?1049l")
last = out.rfind(b"\x1b[?1049h")
ok = True
for name, cond in [("entered the alternate screen 3 times (start, after shell, after editor)", enter == 3),
                   ("left it 3 times (shell, editor, quit)", leave == 3),
                   ("header redrawn after the last resume", b"runner" in out[last:]),
                   ("the card list is back after the last resume", b"card with a" in out[last:]),
                   ("exit status 0", os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)]:
    print(("ok   " if cond else "FAIL ") + name)
    ok = ok and cond
print(f"enter={enter} leave={leave} exit_status={status}")
sys.exit(0 if ok else 1)
