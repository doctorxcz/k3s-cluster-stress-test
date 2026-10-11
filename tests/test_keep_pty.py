"""The real screens in a pseudo terminal (fake kubectl): --keep-probes end to end, with the keys a user presses."""
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_kubectl.py"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _run_in_pty(args, tmp_path, keys, cols=150, rows=45, wait=4.0, env_extra=None):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), NO_COLOR="1", TERM="xterm",
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), COLUMNS=str(cols), LINES=str(rows), **(env_extra or {}))
    proc = subprocess.Popen([sys.executable, "-m", "stress_test", *args], stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path, env=env)
    os.close(slave)
    out = b""

    def pump(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                out += chunk
    pump(wait)
    for key in keys:
        os.write(master, key.encode())
        pump(1.0)
    pump(0.5)
    try:
        code = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = -9
    os.close(master)
    return code, ANSI.sub("", out.decode("utf-8", "replace"))


def test_nodes_screen_with_keep_probes_shows_the_probe_life_and_leaves_the_pods(tmp_path):
    code, text = _run_in_pty(["--nodes-view", "--keep-probes", "2"], tmp_path, ["q"])
    assert code == 0, text[-600:]
    assert "kept up to 2 h after you leave" in text
    assert re.search(r"probe\s+kept · made .* ago · (1 h|2 h|1 h 5\d min).* left", text), text[-800:]
    assert "Leaving the probe pods running" in text


def test_nodes_screen_without_keep_probes_says_temporary_and_removes_the_pods(tmp_path):
    code, text = _run_in_pty(["--nodes-view"], tmp_path, ["q"])
    assert code == 0, text[-600:]
    assert "probe  temporary" in text.replace("\n", " ") or "temporary (removed when you leave)" in text
    assert "Removing the probe pods" in text


@pytest.mark.parametrize("cols", [80, 150, 240])
def test_dashboard_in_every_width_shows_the_probe_line_in_the_pods_view(tmp_path, cols):
    code, text = _run_in_pty(["--dashboard", "--keep-probes", "1"], tmp_path, ["q"] if cols > 160 else ["\t", "q"] if cols >= 100 else ["\r", "\x1b", "q"], cols=cols)
    assert code == 0, text[-600:]
    assert re.search(r"probe kept · made .* ago · 1 h left", text), text[-800:]
