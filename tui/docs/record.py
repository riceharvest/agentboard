#!/usr/bin/env python3
"""Record the real ab-tui running on a throwaway generated board: a PTY, a terminal emulator (pyte) and PIL.

    pip install pyte pillow
    cargo build --release
    python3 tui/docs/record.py --bin tui/target/release/ab-tui --gif tui/docs/ab-tui.gif --png-dir tui/docs

Nothing here touches your real board: the board lives in a temp directory (AB_HOME), made by tests/make_fixture.py,
and while recording a few cards are finished, killed and added so the live effects show."""
import argparse
import fcntl
import hashlib
import os
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tui" / "tests"))
import make_fixture  # noqa: E402

FONTS = ["/usr/share/fonts/source-foundry-hack-fonts/Hack-Regular.ttf", "/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf",
         "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", "/usr/share/fonts/gdouros-symbola/Symbola.ttf"]
BOLD = ["/usr/share/fonts/source-foundry-hack-fonts/Hack-Bold.ttf", "/usr/share/fonts/google-noto/NotoSansMono-Bold.ttf"]
BG = (13, 16, 23)
NAMED = {"black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121), "brown": (229, 229, 16), "yellow": (229, 229, 16), "blue": (36, 114, 200),
         "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229), "brightblack": (102, 102, 102), "brightred": (241, 76, 76),
         "brightgreen": (35, 209, 139), "brightbrown": (245, 245, 67), "brightyellow": (245, 245, 67), "brightblue": (59, 142, 234),
         "brightmagenta": (214, 112, 214), "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255)}


def color(name, default):
    if name in (None, "default"):
        return default
    if name in NAMED:
        return NAMED[name]
    try:
        return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


class Renderer:
    def __init__(self, cols, rows, size=14):
        self.fonts = [ImageFont.truetype(p, size) for p in FONTS if os.path.exists(p)]
        self.bold = [ImageFont.truetype(p, size) for p in BOLD if os.path.exists(p)] or self.fonts
        self.cw = round(self.fonts[0].getlength("M"))
        self.ch = size + 5
        self.cols, self.rows = cols, rows
        self.size = (cols * self.cw, rows * self.ch)
        self.notdef = [bytes(f.getmask("￿")) for f in self.fonts]

    def font_for(self, ch, bold):
        pool = self.bold if bold else self.fonts
        for i, f in enumerate(pool):
            if bytes(f.getmask(ch)) != bytes(f.getmask("￿")):
                return f
        for f in self.fonts:
            if bytes(f.getmask(ch)) != bytes(f.getmask("￿")):
                return f
        return pool[0]

    def draw(self, grid):
        img = Image.new("RGB", self.size, BG)
        d = ImageDraw.Draw(img)
        for y, row in enumerate(grid):
            x = 0
            while x < self.cols:  # merge runs of the same background
                ch, fg, bg, bold, rev = row[x]
                bgc = fg if rev else bg
                x2 = x
                while x2 + 1 < self.cols and (row[x2 + 1][2] if not row[x2 + 1][4] else row[x2 + 1][1]) == bgc:
                    x2 += 1
                if bgc != BG:
                    d.rectangle([x * self.cw, y * self.ch, (x2 + 1) * self.cw - 1, (y + 1) * self.ch - 1], fill=bgc)
                x = x2 + 1
            for x, (ch, fg, bg, bold, rev) in enumerate(row):
                if ch != " " and ch != "":
                    d.text((x * self.cw, y * self.ch), ch, font=self.font_for(ch, bold), fill=bg if rev else fg)
        return img


def grid_of(screen):
    out = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        row = []
        for x in range(screen.columns):
            c = line[x]
            row.append((c.data or " ", color(c.fg, (222, 227, 236)), color(c.bg, BG), bool(c.bold), bool(c.reverse)))
        out.append(row)
    return out


def keepalive(db_path, pid, stop):
    """The runner row of the throwaway board needs a fresh heartbeat to show as running."""
    while not stop.is_set():
        try:
            c = sqlite3.connect(db_path, timeout=5)
            c.execute("UPDATE runner SET heartbeat=?, pid=? WHERE id=1", (time.time(), pid))
            c.commit()
            c.close()
        except sqlite3.Error:
            pass
        stop.wait(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--gif", required=True)
    ap.add_argument("--png-dir")
    ap.add_argument("--cols", type=int, default=132)
    ap.add_argument("--rows", type=int, default=38)
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="ab-tui-demo-"))
    sleeper = subprocess.Popen(["sleep", "600"])
    tasks = make_fixture.build(home, run_pids=[sleeper.pid])
    ram_cmd = "awk '/MemAvailable/ {printf \"free RAM %.1f GiB\\n\", $2/1048576}' /proc/meminfo"
    (home / "tui.toml").write_text('owner = "lead"\npoll_ms = 700\nbell = false\ngpu_command = "echo GPU0 2% 1.1/12.0 GiB"\nram_command = \'\'\'' + ram_cmd + '\'\'\'\n\n[[panel]]\ntitle = "Digest"\ncommand = "echo nightly soak: 41 runs, 3 failed"\nrefresh = 30\n')
    stop = threading.Event()
    threading.Thread(target=keepalive, args=(str(next(home.glob("*.db"))), sleeper.pid, stop), daemon=True).start()

    env = {k: v for k, v in os.environ.items() if not k.startswith("AB_")}
    env.update(AB_HOME=str(home), AB_BIN=str(ROOT / "ab"), AB_ACTOR="lead", TERM="xterm-256color", COLORTERM="truecolor", PYTHONPATH=str(ROOT))

    def ab(*args, actor="worker-1"):
        e = dict(env)
        e["AB_ACTOR"] = actor
        return subprocess.run([sys.executable, "-m", "agentboard", *args], cwd=ROOT, env=e, capture_output=True, text=True)

    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", a.rows, a.cols, 0, 0))
        os.execve(a.bin, [a.bin, "--board-home", str(home), "--owner", "lead", "--as", "lead"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", a.rows, a.cols, 0, 0))
    screen = pyte.Screen(a.cols, a.rows)
    stream = pyte.ByteStream(screen)
    rend = Renderer(a.cols, a.rows)

    # (time, keys sent to the TUI or a callable run against the board, png name or None)
    def finish_run1():
        ab("done", tasks["run1"], "ported the harvester to the v2 log layout, 12 tests pass", actor="worker-1")

    def kill_dead():
        tid = ab("add", "Rebuild the arm64 wheel", "--max-attempts", "1", "--label", "type=build", "--tier", "medium", actor="worker-2").stdout.split()[0]
        ab("claim", tid, actor="worker-2")
        ab("fail", tid, "the cross compiler is missing on this host", actor="worker-2")

    def new_decision():
        ab("add", "Decide: publish the 0.4 release notes?", "--manual", "--label", "type=decision", "--label", "owner=lead", "--priority", "6",
           "--body", "Options:\n- publish now\n- wait for the soak\n\nDefault: wait for the soak", actor="worker-1")

    script = [
        (0.0, None, None), (2.2, None, "board"),
        (2.6, "jj", None), (3.2, finish_run1, None), (4.6, kill_dead, None), (6.4, None, None),
        (6.8, "\t", None), (8.6, None, "running"), (9.6, "1", None), (10.2, "l", None), (10.6, "l", None),
        (11.2, "\r", None), (13.0, None, "card"),
        (13.4, "\x1b", None), (13.6, new_decision, None), (14.8, "4", None), (16.6, None, "decisions"),
        (17.0, "a", None), (17.6, "B", None), (17.9, "\r", None), (19.3, "5", None), (21.0, None, "usage"),
        (21.4, "6", None), (23.0, None, "feed"), (23.4, "7", None), (25.2, None, "ops"),
        (25.6, "?", None), (27.2, None, "help"), (27.6, "\x1b", None), (28.0, "1", None), (30.0, None, None),
    ]
    frames, t0, idx = [], time.time(), 0
    shots = {}
    last_hash, end = None, script[-1][0]
    step = 1.0 / a.fps
    next_cap = 0.0
    while True:
        now = time.time() - t0
        while idx < len(script) and script[idx][0] <= now:
            _, act, png = script[idx]
            if isinstance(act, str):
                os.write(fd, act.encode())
            elif callable(act):
                threading.Thread(target=act, daemon=True).start()
            if png:
                shots[png] = now
            idx += 1
        r, _, _ = select.select([fd], [], [], 0.01)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            stream.feed(data)
        if now >= next_cap:
            next_cap = now + step
            g = grid_of(screen)
            h = hashlib.md5(repr(g).encode()).hexdigest()
            frames.append((now, g, h))
            for name, ts in list(shots.items()):
                if now >= ts and a.png_dir and ts >= 0:
                    rend.draw(g).save(Path(a.png_dir) / f"{name}.png", optimize=True)
                    del shots[name]
        if now > end + 0.5:
            break
    os.write(fd, b"q")
    time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    stop.set()
    sleeper.terminate()

    # collapse identical frames and write the GIF
    imgs, durs = [], []
    for i, (t, g, h) in enumerate(frames):
        nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 1.5
        d = max(20, int((nxt - t) * 1000))
        if imgs and h == last_hash:
            durs[-1] += d
            continue
        last_hash = h
        imgs.append(rend.draw(g))
        durs.append(d)
    pal = [im.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for im in imgs]
    pal[0].save(a.gif, save_all=True, append_images=pal[1:], duration=durs, loop=0, optimize=True, disposal=1)
    print(f"{len(frames)} frames sampled, {len(imgs)} distinct, {sum(durs) / 1000:.1f} s, {Path(a.gif).stat().st_size / 1e6:.2f} MB -> {a.gif}")


if __name__ == "__main__":
    main()
