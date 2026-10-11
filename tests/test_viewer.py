"""The viewer: a test in the terminal runs detached and is only watched; q hands it over, Ctrl-C asks, a quick second Ctrl-C stops it."""
import fcntl
import io
import json
import os
import re
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from stress_test import ui, viewer
from test_integration import FAKE, ROOT

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---------------- helpers ---------------------------------------------------------------------------------------------------

def test_strip_first_move_only_removes_the_leading_move_up():
    assert viewer.strip_first_move(b"\x1b[7A\x1b[J\x1b[2Kline\n") == b"\x1b[2Kline\n"
    assert viewer.strip_first_move(b"plain\x1b[7A\x1b[J") == b"plain\x1b[7A\x1b[J"
    assert viewer.strip_first_move(b"") == b""


def test_wanted_needs_a_terminal_an_interactive_run_and_no_opt_out(monkeypatch):
    import argparse
    monkeypatch.delenv("STRESS_NO_ATTACH", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    ok = argparse.Namespace(non_interactive=False, no_detach=False)
    assert viewer.wanted(ok, True, True) is True
    assert viewer.wanted(ok, False, True) is False and viewer.wanted(ok, True, False) is False
    assert viewer.wanted(argparse.Namespace(non_interactive=True, no_detach=False), True, True) is False
    assert viewer.wanted(argparse.Namespace(non_interactive=False, no_detach=True), True, True) is False
    monkeypatch.setenv("STRESS_NO_ATTACH", "1")
    assert viewer.wanted(ok, True, True) is False
    monkeypatch.delenv("STRESS_NO_ATTACH")
    monkeypatch.setenv("TERM", "dumb")
    assert viewer.wanted(ok, True, True) is False


def test_size_file_and_the_environment_of_the_child(tmp_path, monkeypatch):
    console = str(tmp_path / "x.console.txt")
    assert viewer.write_size(console, (120, 40)) == (120, 40)
    assert json.loads(Path(console + ".size").read_text()) == {"cols": 120, "lines": 40}
    saved = viewer.saved_environment()
    monkeypatch.delenv("STRESS_NO_LIVE", raising=False)
    monkeypatch.setattr(viewer, "terminal_size", lambda: (120, 40))                  # what the viewer's terminal says
    with viewer.child_environment(console):
        assert os.environ["STRESS_VIEWER"] == "1" and os.environ["STRESS_FORCE_LIVE"] == "1"
        assert os.environ["STRESS_VIEW_SIZE_FILE"] == console + ".size"
        assert ui.view_size() == (120, 40) and ui.term_cols() == 120 and ui.term_rows() == 40       # the detached process takes the viewer's size
    viewer.restore_environment(saved)
    assert "STRESS_VIEWER" not in os.environ and "STRESS_FORCE_LIVE" not in os.environ
    monkeypatch.setenv("STRESS_NO_LIVE", "1")
    with viewer.child_environment(console):
        assert "STRESS_FORCE_LIVE" not in os.environ                                                   # live frames stay off when the user chose so
    viewer.restore_environment(saved)


def test_canvas_adds_the_hint_row_and_marks_the_frame_only_with_a_viewer(tmp_path, monkeypatch):
    console = tmp_path / "c.txt"
    with open(console, "ab", buffering=0) as raw:
        stream = io.TextIOWrapper(raw, write_through=True)
        monkeypatch.setenv("STRESS_VIEWER", "1")
        monkeypatch.setenv("STRESS_VIEW_FRAME_FILE", str(tmp_path / "c.frame"))
        monkeypatch.setenv("STRESS_VIEW_SIZE_FILE", str(tmp_path / "c.size"))
        viewer.write_size(str(tmp_path / "c"), (90, 30))
        os.rename(tmp_path / "c.size", tmp_path / "c.size")
        canvas = ui.Canvas(stream)
        canvas.draw(["one", "two"])
        first_end = console.stat().st_size
        canvas.draw(["one", "two"])
    text = console.read_bytes().decode()
    assert text.count("w  new window") == 2 and "\x1b[3A\x1b[J" in text            # 2 lines + the hint row are repainted
    assert int((tmp_path / "c.frame").read_text()) == first_end                                         # the newest frame starts after the first one


def test_the_live_screen_is_forced_for_a_detached_viewer_run(monkeypatch):
    monkeypatch.setenv("STRESS_FORCE_LIVE", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert ui.LiveScreen.wanted() and ui.color_enabled() and ui.live_tty()
    monkeypatch.setenv("STRESS_NO_LIVE", "1")
    assert not ui.LiveScreen.wanted()


# ---------------- watch(): keys and the end of the test -----------------------------------------------------------------------

class Script:
    """Keys given one per call, with the (fake) time they come at."""

    def __init__(self, events):
        self.events = list(events)        # (key or None, clock value)
        self.now = 0.0

    def keys(self, timeout):
        if not self.events:
            return None
        key, at = self.events.pop(0)
        self.now = at
        if key == "ctrl-c":
            raise KeyboardInterrupt
        return key

    def clock(self):
        return self.now


def run_watch(tmp_path, events, text=b"frame1\n", finished_after=None, wait=0.0):
    console = tmp_path / "w.console.txt"
    console.write_bytes(text)
    out = io.BytesIO()
    script = Script(events)
    killed = []
    state = {"calls": 0}

    def finished():
        state["calls"] += 1
        return (finished_after is not None and state["calls"] >= finished_after), 0
    result = viewer.watch(str(console), 4242, out=out, keys=script.keys, size=lambda: (100, 30), finished=finished,
                          kill=lambda pid, sig: killed.append((pid, sig)), clock=script.clock, poll_s=0.0, wait_for_file_s=wait)
    return result, out.getvalue(), killed


def test_q_sends_the_test_to_the_background_without_touching_it(tmp_path):
    result, out, killed = run_watch(tmp_path, [(None, 0), ("q", 1)])
    assert result["result"] == "background" and killed == [] and out.startswith(b"frame1")


def test_a_finished_test_ends_the_watch_with_its_exit_code_and_its_last_output(tmp_path):
    result, out, killed = run_watch(tmp_path, [(None, 0)] * 5, finished_after=2)
    assert result == {"result": "done", "code": 0} and killed == []


def test_ctrl_c_asks_and_s_stops(tmp_path):
    result, out, killed = run_watch(tmp_path, [("ctrl-c", 10.0), ("s", 14.0)] + [(None, 15.0)] * 3, finished_after=3)
    assert killed == [(4242, signal.SIGTERM)] and result["result"] == "stopped"
    assert b"\x1b[?1049h" in out and b"\x1b[?1049l" in out and b"THE TEST IS RUNNING" in out        # the question is on the alternate screen


def test_a_quick_second_ctrl_c_stops_at_once(tmp_path):
    result, out, killed = run_watch(tmp_path, [("ctrl-c", 10.0), ("ctrl-c", 10.6)] + [(None, 11.0)] * 3, finished_after=3)
    assert killed == [(4242, signal.SIGTERM)] and result["result"] == "stopped"


def test_a_slow_second_ctrl_c_only_closes_the_question(tmp_path):
    result, out, killed = run_watch(tmp_path, [("ctrl-c", 10.0), ("ctrl-c", 12.5), ("q", 13.0)])
    assert killed == [] and result["result"] == "background"                                       # the question closed, then q handed it over
    assert out.count(b"\x1b[?1049h") == 1 and out.count(b"\x1b[?1049l") == 1


def test_q_or_enter_in_the_question_keeps_it_running_in_the_background(tmp_path):
    for key in ("q", "\r"):
        result, out, killed = run_watch(tmp_path, [("ctrl-c", 10.0), (key, 11.0)])
        assert result["result"] == "background" and killed == []


def test_esc_in_the_question_goes_on_watching_and_the_held_output_comes_after(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"first\n")
    out = io.BytesIO()
    script = Script([(None, 0), ("ctrl-c", 10.0), (None, 10.1), ("esc", 11.0), ("q", 12.0)])
    reads = {"n": 0}
    real_watch_events = script.events

    # more output arrives while the question is shown
    def keys(timeout):
        reads["n"] += 1
        if reads["n"] == 3:
            with open(console, "ab") as fh:
                fh.write(b"during the question\n")
        return script.keys(timeout)
    result = viewer.watch(str(console), 1, out=out, keys=keys, size=lambda: (100, 30), finished=lambda: (False, None), kill=lambda *a: None,
                          clock=script.clock, poll_s=0.0, wait_for_file_s=0)
    data = out.getvalue()
    assert result["result"] == "background"
    assert data.index(b"?1049h") < data.index(b"?1049l") < data.index(b"during the question")          # held until the screen was back
    assert data.startswith(b"first\n")


def test_joining_in_the_middle_skips_the_first_move_up(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"old\n\x1b[3A\x1b[Jnew frame\n")
    start = len(b"old\n")
    out = io.BytesIO()
    result = viewer.watch(str(console), 1, out=out, keys=Script([("q", 1.0)]).keys, size=lambda: (80, 24), finished=lambda: (False, None),
                          start=start, skip_move=True, poll_s=0.0, wait_for_file_s=0)
    assert out.getvalue() == b"\x1b[Jnew frame\n".replace(b"\x1b[J", b"") or out.getvalue() == b"new frame\n"


def test_the_size_of_the_terminal_is_passed_on_when_it_changes(tmp_path):
    console = tmp_path / "s.console.txt"
    console.write_bytes(b"x\n")
    sizes = iter([(100, 30), (100, 30), (60, 20), (60, 20)])
    seen = []
    script = Script([(None, 0), (None, 0), (None, 0), ("q", 1)])
    viewer.watch(str(console), 1, out=io.BytesIO(), keys=script.keys, size=lambda: seen.append(next(sizes)) or seen[-1],
                 finished=lambda: (False, None), poll_s=0.0, wait_for_file_s=0)
    assert json.loads(Path(str(console) + ".size").read_text()) == {"cols": 60, "lines": 20}


def test_question_frame_fits_the_width():
    for width in (40, 60, 90):
        assert all(ui.visible_len(line) == width for line in viewer.question_lines(False, width))
    assert "Ctrl-C again within a second" in "\n".join(viewer.question_lines(False, 80))


# ---------------- the whole thing in a terminal (fake kubectl) ----------------------------------------------------------------

class Terminal:
    """A program run in a pseudo terminal that is its CONTROLLING terminal, so Ctrl-C (\\x03) really raises SIGINT."""

    def __init__(self, args, tmp_path, rows=40, cols=110):
        import pty
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), TERM="xterm",
                        STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), STRESS_TEST_MENU_STATE=str(tmp_path / "ms.json"),
                        STRESS_TEST_DEBUG_DIR=str(tmp_path / "debug"))
        self.env.pop("NO_COLOR", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "stress_test", *args], stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path,
                                     env=self.env, preexec_fn=lambda: (os.setsid(), fcntl.ioctl(0, termios.TIOCSCTTY, 0)))
        os.close(slave)
        self.out = b""

    def pump(self, seconds):
        import select
        end = time.time() + seconds
        while time.time() < end:
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.out += chunk

    def send(self, text):
        os.write(self.master, text.encode())

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end and self.proc.poll() is None:
            self.pump(0.1)
        return self.proc.poll()

    @property
    def text(self):
        return ANSI.sub("", self.out.decode("utf-8", "replace"))


def run_args(tmp_path):
    return ["--quick", "--node", "fake-node", "--yes", "--log-dir", str(tmp_path / "logs")]


def records(tmp_path):
    return sorted(p.stem for p in (tmp_path / "running").glob("*.json")) if (tmp_path / "running").exists() else []


def stop_all(tmp_path):
    for run_id in records(tmp_path):
        subprocess.run([sys.executable, "-m", "stress_test", "--stop", run_id], cwd=tmp_path, timeout=60, capture_output=True,
                       env=dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT),
                                STRESS_TEST_RUNNING_DIR=str(tmp_path / "running")))


def test_q_hands_the_running_test_over_and_it_can_be_joined_and_stopped(tmp_path):
    term = Terminal(run_args(tmp_path), tmp_path)
    try:
        term.pump(6)
        assert "w  new window" in term.text and records(tmp_path), term.text[-600:]       # the live frame + the hint, a record
        term.send("q")
        assert term.wait(15) == 0
        assert "The test goes on in the background" in term.text and "--attach" in term.text
        ids = records(tmp_path)
        assert len(ids) == 1                                                                                # the test is still running
        join = Terminal(["--attach", ids[0]], tmp_path)
        join.env = term.env
        join.pump(4)
        assert "Joining run" in join.text, join.text[-500:]
        join.send("q")
        assert join.wait(10) == 0 and "goes on in the background" in join.text and records(tmp_path) == ids
    finally:
        stop_all(tmp_path)
    time.sleep(1)
    assert records(tmp_path) == []


def test_two_quick_ctrl_c_stop_the_test(tmp_path):
    term = Terminal(run_args(tmp_path), tmp_path)
    try:
        term.pump(5)
        term.send("\x03")
        term.pump(0.4)
        term.send("\x03")
        code = term.wait(40)
        assert code is not None, term.text[-800:]
        assert "The test was stopped" in term.text or "Cancelled" in term.text or "stopped" in term.text.lower()
        time.sleep(1)
        assert records(tmp_path) == []
    finally:
        stop_all(tmp_path)


def test_ctrl_c_asks_and_q_keeps_the_test_running(tmp_path):
    term = Terminal(run_args(tmp_path), tmp_path)
    try:
        term.pump(5)
        term.send("\x03")
        term.pump(2)
        assert "THE TEST IS RUNNING" in term.text
        term.send("q")
        assert term.wait(15) == 0 and len(records(tmp_path)) == 1
    finally:
        stop_all(tmp_path)


def test_no_detach_keeps_the_classic_foreground(tmp_path):
    term = Terminal(run_args(tmp_path) + ["--no-detach"], tmp_path)
    try:
        term.pump(5)
        assert "w  new window" not in term.text and "THE TEST" not in term.text
        term.send("\x03")
        term.wait(40)
    finally:
        stop_all(tmp_path)
        try:
            term.proc.kill()
        except OSError:
            pass


# ---------------- the optional window ------------------------------------------------------------------------------------------

def test_settings_add_the_view_flags_to_a_test_start_only():
    from stress_test import menu
    assert menu.apply_settings(["--profile", "classic"], {"view": "window"})[-1] == "--test-window"
    assert menu.apply_settings(["--quick"], {"view": "classic"})[-1] == "--no-detach"
    assert "--test-window" not in menu.apply_settings(["--quick"], {"view": "terminal"})
    assert menu.apply_settings(["--status"], {"view": "window"}) == ["--status"]
    assert menu.apply_settings(["--profile", "classic", "--no-detach"], {"view": "window"}).count("--test-window") == 0


def test_settings_screen_chooses_the_view(monkeypatch):
    from stress_test import menu
    state = {}
    monkeypatch.setattr(menu, "save_state", lambda s: state.update(s))
    answers = iter(["6", "2", "0"])
    menu.settings_screen(lambda prompt, default="": next(answers), state)
    assert state["settings"]["view"] == "window"
    assert "a new terminal window" in menu.VIEW_TEXT["window"]


def test_open_window_runs_attach_with_a_wait():
    seen = []
    assert viewer.open_window("abc123", opener=lambda argv: seen.append(argv) or True)
    assert seen[0][1:] == ["-m", "stress_test", "--attach", "abc123", "--attach-wait", "8"]
    assert viewer.open_window("abc123", opener=lambda argv: False) is False


def test_the_test_window_option_opens_a_window_and_frees_the_terminal(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "window-args.txt"
    fake = bin_dir / "xterm"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    fake.chmod(0o755)
    term = Terminal(run_args(tmp_path) + ["--test-window"], tmp_path)
    term.env["STRESS_TERMINAL"] = str(fake)                      # the user's own terminal choice (the real emulators are never started)
    term.env["DISPLAY"] = ":0"
    term.env.pop("TMUX", None)
    term.proc.kill()                                  # restart with the environment above
    term2 = None
    try:
        import pty
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
        proc = subprocess.Popen([sys.executable, "-m", "stress_test", *run_args(tmp_path), "--test-window"], stdin=slave, stdout=slave,
                                stderr=slave, cwd=tmp_path, env=term.env, preexec_fn=lambda: (os.setsid(), fcntl.ioctl(0, termios.TIOCSCTTY, 0)))
        os.close(slave)
        out = b""
        import select
        end = time.time() + 45
        while time.time() < end and proc.poll() is None:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break
        assert proc.poll() == 0, ANSI.sub("", out.decode("utf-8", "replace"))[-600:]
        text = ANSI.sub("", out.decode("utf-8", "replace"))
        assert "shown in a new terminal window" in text
        args = marker.read_text()
        assert "--attach" in args and "--attach-wait" in args and "stress_test" in args          # the fake emulator got the attach command
        assert len(records(tmp_path)) == 1                                                       # the test runs
    finally:
        stop_all(tmp_path)


def test_without_a_window_the_test_is_watched_in_this_terminal(tmp_path):
    term = Terminal(run_args(tmp_path) + ["--test-window"], tmp_path)          # no DISPLAY, no tmux in the test environment
    term.env.pop("DISPLAY", None)
    term.proc.kill()
    try:
        import pty
        master, slave = pty.openpty()
        env = dict(term.env)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("TMUX", None)
        proc = subprocess.Popen([sys.executable, "-m", "stress_test", *run_args(tmp_path), "--test-window"], stdin=slave, stdout=slave,
                                stderr=slave, cwd=tmp_path, env=env, preexec_fn=lambda: (os.setsid(), fcntl.ioctl(0, termios.TIOCSCTTY, 0)))
        os.close(slave)
        import select
        out = b""
        end = time.time() + 7
        while time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break
        os.write(master, b"q")
        end = time.time() + 12
        while time.time() < end and proc.poll() is None:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break
        text = ANSI.sub("", out.decode("utf-8", "replace"))
        assert "No terminal window could be opened" in text and "w  new window" in text and proc.poll() == 0
    finally:
        stop_all(tmp_path)


def test_old_console_files_are_pruned(tmp_path):
    old, new = tmp_path / "old.console.txt", tmp_path / "new.console.txt"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (time.time() - 9 * 86400, time.time() - 9 * 86400))
    assert viewer.prune(tmp_path) == 1 and not old.exists() and new.exists()
    assert viewer.prune(tmp_path / "missing") == 0


# ---------------- key w: the window ----------------------------------------------------------------------------------------------

def test_w_with_a_window_returns_window(tmp_path):
    calls = []
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"frame\n")
    script = Script([(None, 0), ("W", 1)])
    result = viewer.watch(str(console), 1, out=io.BytesIO(), keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None),
                          clock=script.clock, poll_s=0.0, wait_for_file_s=0, window=lambda kind: calls.append(kind) or True)
    assert result["result"] == "window" and calls == ["terminal"]


def test_w_without_a_possible_window_shows_a_notice_and_goes_on(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"frame\n")
    script = Script([(None, 0), ("w", 1), (None, 2), ("q", 3)])
    pauses, out = [], io.BytesIO()
    result = viewer.watch(str(console), 1, out=out, keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None),
                          clock=script.clock, poll_s=0.0, wait_for_file_s=0, window=lambda kind: False, pause=pauses.append)
    data = out.getvalue()
    assert result["result"] == "background" and pauses == [2.5]                          # it went on watching until q
    assert b"No window could be opened" in data and data.index(b"?1049h") < data.index(b"?1049l")


def test_w_does_nothing_while_the_test_is_being_stopped_or_without_a_window_function(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"frame\n")
    script = Script([(None, 0), ("w", 1), ("q", 2)])
    result = viewer.watch(str(console), 1, out=io.BytesIO(), keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None),
                          clock=script.clock, poll_s=0.0, wait_for_file_s=0)             # no window function: w is just another key
    assert result["result"] == "background"


def test_key_w_in_a_terminal_opens_the_window_and_the_test_goes_on(tmp_path):
    marker = tmp_path / "w-args.txt"
    fake = tmp_path / "xterm"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    fake.chmod(0o755)
    term = Terminal(run_args(tmp_path), tmp_path)
    term.proc.kill()                                                    # restart with the fake emulator in the environment
    term.env["STRESS_TERMINAL"] = str(fake)
    term.env.pop("TMUX", None)                                          # this session may run inside tmux: then w would ask which kind
    import pty
    import select
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
    proc = subprocess.Popen([sys.executable, "-m", "stress_test", *run_args(tmp_path)], stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path,
                            env=term.env, preexec_fn=lambda: (os.setsid(), fcntl.ioctl(0, termios.TIOCSCTTY, 0)))
    os.close(slave)
    out = b""

    def pump(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end and proc.poll() is None:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    return
    try:
        pump(6)
        assert "w  new window" in ANSI.sub("", out.decode("utf-8", "replace"))
        os.write(master, b"w")
        pump(8)
        text = ANSI.sub("", out.decode("utf-8", "replace"))
        assert proc.poll() == 0 and "shown in a new terminal window" in text, text[-500:]
        assert "--attach" in marker.read_text() and len(records(tmp_path)) == 1
    finally:
        stop_all(tmp_path)


def test_a_test_started_with_dash_b_can_be_joined_and_shows_the_live_screen(tmp_path):
    """-b used to write a plain log into the console, so joining it showed only lines; now its live screen is drawn into the console too."""
    starter = Terminal(run_args(tmp_path) + ["--background"], tmp_path)
    try:
        assert starter.wait(25) == 0, starter.text[-600:]
        assert "TEST RUNNING IN THE BACKGROUND" in starter.text and "--attach" in starter.text
        ids = records(tmp_path)
        assert len(ids) == 1
        time.sleep(3)
        join = Terminal(["--attach", ids[0]], tmp_path)
        join.env = starter.env
        join.pump(5)
        assert "Joining run" in join.text and "w  new window" in join.text and "TEST" in join.text.upper(), join.text[-700:]
        assert "╭" in join.text or "┏" in join.text                                  # a framed live screen, not plain log lines
        join.send("q")
        assert join.wait(10) == 0
    finally:
        stop_all(tmp_path)


# ---------------- key w in the dashboard, NODES and watch screens ------------------------------------------------------------------

def plan(kinds, preset=None):
    return lambda *a, **k: {"kinds": list(kinds), "preset": preset}


def test_dashboard_key_w_moves_the_screen_to_a_window_of_the_preset_kind(monkeypatch):
    from stress_test import dashboard as d, nodes_screen, notify
    from stress_test.kube import Kubectl
    opened = []
    monkeypatch.setattr(notify, "plan_window", plan(["tmux-window", "terminal"], "tmux-window"))
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: opened.append((argv, kw)) or True)
    screen = d.Dashboard(Kubectl(str(FAKE)), ["a", "b"], keep_hours=3)
    assert screen.handle("w") is False                                    # the screen ends here: it is in the window now
    argv, kw = opened[0]
    assert "--dashboard" in argv and argv[argv.index("--nodes") + 1] == "a,b" and argv[argv.index("--keep-probes") + 1] == "3"
    assert kw == {"keep_open": False, "kind": "tmux-window"}               # the window closes with the program
    nodes = nodes_screen.NodesScreen(Kubectl(str(FAKE)), None)
    assert nodes.handle("W") is False and "--nodes-view" in opened[1][0] and "--dashboard" not in opened[1][0]


def test_dashboard_key_w_asks_when_there_is_more_than_one_way_and_the_user_wants_to_be_asked(monkeypatch):
    from stress_test import dashboard as d, nodes_screen, notify
    from stress_test.kube import Kubectl
    opened = []
    monkeypatch.setattr(notify, "plan_window", plan(["tmux-window", "tmux-split", "terminal"]))
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: opened.append(kw["kind"]) or True)
    screen = d.Dashboard(Kubectl(str(FAKE)), None)
    assert screen.handle("w") is True and opened == []                    # the question is shown, the screen stays
    assert screen.state.window_pick == ["tmux-window", "tmux-split", "terminal"]
    drawn = []
    screen.canvas.draw = lambda frame: drawn.append("\n".join(frame))
    screen.draw()
    question = ANSI.sub("", drawn[-1])
    assert "NEW WINDOW" in question and "1  tmux window" in question and "2  tmux split" in question and "3  terminal window" in question
    assert "Esc  cancel" in question and "press the number, no Enter" in question                      # a framed question that cannot be missed
    assert screen.handle("2") is False and opened == ["tmux-split"] and screen.status == ""      # the next key chooses
    screen.handle("w")
    assert screen.handle("esc") is True and opened == ["tmux-split"]       # Esc cancels
    screen.handle("w")
    assert screen.handle("x") is True and len(opened) == 1                 # any other key cancels too
    nodes = nodes_screen.NodesScreen(Kubectl(str(FAKE)), None)
    nodes.handle("w")
    assert nodes.handle("3") is False and opened[-1] == "terminal"


def test_dashboard_key_w_without_any_possible_window_keeps_the_screen_and_says_why(monkeypatch):
    from stress_test import dashboard as d, notify
    from stress_test.kube import Kubectl
    monkeypatch.setattr(notify, "plan_window", plan([]))
    screen = d.Dashboard(Kubectl(str(FAKE)), None)
    assert screen.handle("w") is True and "No window can be made here" in screen.status
    monkeypatch.setattr(notify, "plan_window", plan(["terminal"], "terminal"))
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: False)
    assert screen.handle("w") is True and "could not be opened" in screen.status


def test_watch_key_w(monkeypatch):
    from stress_test import notify, watch
    seen = []
    monkeypatch.setattr(notify, "plan_window", plan(["terminal"], "terminal"))
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: seen.append((argv, kw)) or True)
    screen = watch.Screen()
    assert screen.handle("w") is False and seen[0][0][-2:] == ["--status", "--live"] and seen[0][1]["kind"] == "terminal"
    monkeypatch.setattr(notify, "plan_window", plan(["tmux-window", "terminal"]))
    assert screen.handle("w") is True and screen.state.window_pick == ["tmux-window", "terminal"]
    assert screen.handle("2") is False and seen[1][1]["kind"] == "terminal"
    monkeypatch.setattr(notify, "plan_window", plan([]))
    assert screen.handle("w") is True and "No window can be made here" in screen.state.message


def test_the_screens_open_in_a_window_with_dash_dash_test_window_and_the_setting(monkeypatch, capsys):
    import argparse
    from stress_test import cli, menu, notify
    seen = []
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: seen.append(argv) or True)
    args = argparse.Namespace(test_window=True, nodes="a,b", keep_probes=2)
    assert cli._screen_in_window(args, ["--dashboard"], "The dashboard") is True
    assert seen[0][3:] == ["--dashboard", "--nodes", "a,b", "--keep-probes", "2"] and "--test-window" not in seen[0]
    assert "new terminal window" in capsys.readouterr().out
    assert cli._screen_in_window(argparse.Namespace(test_window=False, nodes="", keep_probes=0), ["--dashboard"], "x") is False
    monkeypatch.setattr(notify, "open_window", lambda argv, **kw: False)
    assert cli._screen_in_window(args, ["--dashboard"], "The dashboard") is False                       # no window: it runs here
    assert "No terminal window could be opened" in capsys.readouterr().out
    window = {"view": "window"}
    assert "--test-window" in menu.apply_settings(["--dashboard"], window) and "--test-window" in menu.apply_settings(["--nodes-view"], window)
    assert "--test-window" in menu.apply_settings(["--status", "--live"], window)
    assert "--test-window" not in menu.apply_settings(["--status"], window) and "--test-window" not in menu.apply_settings(["--dashboard"], {"view": "terminal"})


def test_viewer_key_w_asks_when_there_is_more_than_one_way(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"frame\n")
    opened, out = [], io.BytesIO()
    script = Script([(None, 0), ("w", 1), ("3", 2)])
    result = viewer.watch(str(console), 1, out=out, keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None), clock=script.clock,
                          poll_s=0.0, wait_for_file_s=0, window=lambda kind: opened.append(kind) or True,
                          window_plan=lambda: {"kinds": ["tmux-window", "tmux-split", "terminal"], "preset": None})
    data = out.getvalue()
    assert result["result"] == "window" and opened == ["terminal"]
    assert b"NEW WINDOW" in data and b"1  tmux window" in data and data.index(b"?1049h") < data.index(b"?1049l")     # asked on the alternate screen
    script = Script([(None, 0), ("w", 1), ("esc", 2), ("q", 3)])
    opened.clear()
    result = viewer.watch(str(console), 1, out=io.BytesIO(), keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None),
                          clock=script.clock, poll_s=0.0, wait_for_file_s=0, window=lambda kind: opened.append(kind) or True,
                          window_plan=lambda: {"kinds": ["tmux-window", "terminal"], "preset": None})
    assert result["result"] == "background" and opened == []                                               # Esc cancelled, q handed it over


def test_viewer_key_w_uses_the_preset_without_asking(tmp_path):
    console = tmp_path / "w.console.txt"
    console.write_bytes(b"frame\n")
    opened, out = [], io.BytesIO()
    script = Script([(None, 0), ("w", 1)])
    result = viewer.watch(str(console), 1, out=out, keys=script.keys, size=lambda: (100, 30), finished=lambda: (False, None), clock=script.clock,
                          poll_s=0.0, wait_for_file_s=0, window=lambda kind: opened.append(kind) or True,
                          window_plan=lambda: {"kinds": ["tmux-window", "terminal"], "preset": "tmux-window"})
    assert result["result"] == "window" and opened == ["tmux-window"] and b"NEW WINDOW" not in out.getvalue()
