"""A test in the foreground that can be sent to the background with one key.

How: a test started in the terminal does not run in this process. It runs detached (the same mechanism as `-b`), writing its output - the
live frames included - to a console file, and this process only WATCHES: it replays that file on the terminal. Pressing `q` simply stops
watching, so the test goes on exactly as if it had been started with `-b`. Ctrl-C asks (stop the test, or keep it running); a second Ctrl-C
within a second stops it at once. `--attach ID` joins a running test again (it starts from the newest frame).

The detached process has no terminal, so the watcher keeps the size of its terminal in a small file (`<console>.size`) that the live screens
read (`ui.view_size`), and the live screens note where their newest frame starts in `<console>.frame`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import background, ui

ENV_KEYS = ("STRESS_VIEWER", "STRESS_FORCE_LIVE", "STRESS_VIEW_SIZE_FILE", "STRESS_VIEW_FRAME_FILE")
DOUBLE_CTRL_C_S = 1.0
_MOVE = re.compile(rb"^\x1b\[\d+A\x1b\[J")            # how every frame of a live block starts: up N lines, clear below


def attach_dir() -> Path:
    from .paths import HIDDEN_LOGS, make_private_dir
    folder = HIDDEN_LOGS / "attach"
    make_private_dir(folder)
    return folder


def prune(folder: Path, days: int = 7, now: Optional[float] = None) -> int:
    """Removes the console files of tests that ended more than `days` days ago; returns how many files went."""
    limit = (now or time.time()) - days * 86400
    removed = 0
    try:
        for path in folder.iterdir():
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def console_for(run_id: str) -> str:
    folder = attach_dir()
    prune(folder)
    return str(folder / f"{run_id}.console.txt")


def wanted(args=None, stdin_tty: Optional[bool] = None, stdout_tty: Optional[bool] = None) -> bool:
    """A foreground test runs detached with a viewer only on a real terminal, in an interactive run, where fork exists, and when the user
    has not asked for the classic behaviour (--no-detach, STRESS_NO_ATTACH, SETTINGS)."""
    if os.environ.get("STRESS_NO_ATTACH") or not background.supported():
        return False
    if args is not None and (getattr(args, "non_interactive", False) or getattr(args, "no_detach", False)):
        return False
    stdin_tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
    stdout_tty = sys.stdout.isatty() if stdout_tty is None else stdout_tty
    return bool(stdin_tty and stdout_tty and os.environ.get("TERM", "") not in ("", "dumb"))


def terminal_size() -> tuple:
    try:
        size = os.get_terminal_size(1)
        return size.columns, size.lines
    except (OSError, ValueError):
        return 80, 24


def write_size(console: str, size: Optional[tuple] = None) -> tuple:
    cols, lines = size or terminal_size()
    path = console + ".size"
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cols": cols, "lines": lines}, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return cols, lines


@contextlib.contextmanager
def child_environment(console: str, live: bool = True):
    """The environment the detached process inherits (live frames forced on, the size and frame files); the caller - the parent that
    stays - gets its own environment back afterwards."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    write_size(console)
    os.environ["STRESS_VIEWER"] = "1"
    os.environ["STRESS_VIEW_SIZE_FILE"] = console + ".size"
    os.environ["STRESS_VIEW_FRAME_FILE"] = console + ".frame"
    if live and not os.environ.get("STRESS_NO_LIVE"):
        os.environ["STRESS_FORCE_LIVE"] = "1"
    try:
        yield
    finally:
        pass


def restore_environment(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def saved_environment() -> dict:
    return {k: os.environ.get(k) for k in ENV_KEYS}


def strip_first_move(chunk: bytes) -> bytes:
    """A viewer that joins in the middle starts at a frame: its first 'move up N lines' would climb into the terminal's old content."""
    return _MOVE.sub(b"", chunk, count=1)


def read_key(timeout: float, fd: int = 0) -> Optional[str]:
    """One key as text ('' = nothing within the timeout); arrows and the like are returned as 'esc-seq' and ignored by the caller."""
    ready, _w, _x = select.select([fd], [], [], timeout)
    if not ready:
        return None
    data = os.read(fd, 16)
    if not data:
        return "eof"
    text = data.decode("utf-8", "ignore")
    if text == "\x1b":
        return "esc"
    if text.startswith("\x1b"):
        return "seq"
    return text[:1]


def question_lines(on: bool, width: int) -> list:
    extra = ["  " + ui.paint("s", ui.BOLD + ";" + ui.YELLOW, on) + " = stop the test now (the pods are deleted)",
             "  " + ui.paint("q / Enter", ui.BOLD + ";" + ui.GREEN, on) + " = keep it running in the background and give the terminal back",
             "  " + ui.paint("Esc / c", ui.BOLD + ";" + ui.GREEN, on) + " = go on watching",
             ui.paint("  Ctrl-C again within a second stops it at once.", ui.GREY, on)]
    return ui._question_frame(" ⏸ THE TEST IS RUNNING ", ui.YELLOW, "What now? (the test is not affected while you decide)", extra, on, width)


def _ask_window_kind(out, keys: Callable, kinds: list, clock: Callable[[], float], size: tuple) -> Optional[str]:
    """The question of key w on the alternate screen: 1 2 3 chooses, Esc / anything else / 30 s cancels (None)."""
    from . import notify
    from . import ui
    out.write(b"\x1b[?1049h\x1b[2J\x1b[H" + ("\n".join(notify.pick_lines(kinds, ui.color_enabled(), max(40, min(size[0] - 1, 100)))) + "\n").encode("utf-8"))
    out.flush()
    kind = None
    deadline = clock() + 30
    while clock() < deadline:
        try:
            key = keys(0.2)
        except KeyboardInterrupt:
            break
        if key is None:
            continue
        kind = notify.kind_of_key(key, kinds)
        break
    out.write(b"\x1b[?1049l")
    out.flush()
    return kind


def watch(console: str, pid: int, out=None, keys: Callable = read_key, size: Callable = terminal_size,
          finished: Optional[Callable] = None, kill: Callable = os.kill, clock: Callable[[], float] = time.monotonic,
          start: int = 0, skip_move: bool = False, wait_for_file_s: float = 10.0, poll_s: float = 0.05,
          window: Optional[Callable[[Optional[str]], bool]] = None, pause: Callable[[float], None] = time.sleep,
          window_plan: Optional[Callable[[], dict]] = None) -> dict:
    """Replays the console file on the terminal until the test ends or the user lets go.

    Returns {"result": "done" | "background" | "window" | "stopped", "code": exit code or None}. Key w: `window(kind)` opens a new window with
    the live view and this terminal lets go ("window"); `window_plan()` says which kinds are possible and which is preset (more than one
    and no preset: a question on the alternate screen); when no window can be opened a notice is shown for a moment and the watch goes on. `finished()` -> (True, code) once the process is gone
    (default: the child's waitpid). The terminal state (cbreak) is the caller's business.
    """
    out = out or sys.stdout.buffer
    if finished is None:
        def finished():
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                try:
                    os.kill(pid, 0)
                    return False, None
                except OSError:
                    return True, None
            if not done:
                return False, None
            return True, (os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status)
    on = ui.color_enabled()
    deadline = clock() + wait_for_file_s
    while not os.path.exists(console) and clock() < deadline:
        time.sleep(0.05)
    fh = open(console, "rb")
    try:
        fh.seek(start)
        pending_move = skip_move
        held: list = []                                       # output that arrives while the question is shown
        asking = False
        asked_at = 0.0
        stopping = False
        last_size = None
        result = None
        while True:
            chunk = fh.read(65536)
            if chunk:
                if pending_move:
                    chunk, pending_move = strip_first_move(chunk), False
                if asking:
                    held.append(chunk)
                else:
                    out.write(chunk)
                    out.flush()
            current = size()
            if current != last_size:
                last_size = write_size(console, current)
            try:
                key = keys(0.0 if chunk else poll_s)
            except KeyboardInterrupt:
                key = "ctrl-c"
            except OSError:
                key = None
            if key == "eof":
                key = "esc" if asking else "q"
            action = None
            if key == "ctrl-c":
                now = clock()
                if stopping:
                    pass
                elif asking:
                    action = "stop" if now - asked_at <= DOUBLE_CTRL_C_S else "resume"
                else:
                    asking, asked_at = True, now
                    width = max(40, min(current[0] - 1, 90))
                    out.write(b"\x1b[?1049h\x1b[2J\x1b[H" + "\n".join(question_lines(on, width)).encode("utf-8") + b"\n")
                    out.flush()
            elif asking:
                action = {"s": "stop", "S": "stop", "q": "background", "Q": "background", "\r": "background", "\n": "background",
                          "esc": "resume", "c": "resume", "C": "resume"}.get(key or "")
            elif key in ("q", "Q") and not stopping:
                action = "background"
            elif key in ("w", "W") and window is not None and not stopping:
                plan = (window_plan or (lambda: {"kinds": ["terminal"], "preset": "terminal"}))()
                kind = plan.get("preset")
                if plan.get("kinds") and kind is None:                 # more than one way: ask (on the alternate screen, the test is not disturbed)
                    kind = _ask_window_kind(out, keys, plan["kinds"], clock, current)
                if kind and window(kind):
                    result = "window"
                    break
                if kind or not plan.get("kinds"):                      # it could not be done: say so for a moment
                    out.write(b"\x1b[?1049h\x1b[2J\x1b[H" + "No window could be opened here (not in tmux, no display, no known terminal emulator).\n"
                              "STRESS_TERMINAL=\"kitty\" (or another emulator) chooses one. Going on watching ...\n".encode("utf-8"))
                    out.flush()
                    pause(2.5)
                    out.write(b"\x1b[?1049l")
                    out.flush()
            if action and asking:                                 # leave the question: the screen is as it was, then what arrived meanwhile
                out.write(b"\x1b[?1049l")
                for part in held:
                    out.write(part)
                held.clear()
                out.flush()
                asking = False
            if action == "stop":
                try:
                    kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                stopping = True
            elif action == "background":
                result = "background"
                break
            done, code = finished()
            if done:
                while True:                                     # what the process wrote last
                    rest = fh.read(65536)
                    if not rest:
                        break
                    if asking:
                        held.append(rest)
                    else:
                        out.write(rest)
                if asking:
                    out.write(b"\x1b[?1049l")
                    for part in held:
                        out.write(part)
                out.flush()
                return {"result": "stopped" if stopping else "done", "code": code}
        return {"result": result, "code": None}
    finally:
        fh.close()


@contextlib.contextmanager
def raw_keys(fd: int = 0):
    """cbreak: keys arrive at once and are not echoed (Ctrl-C still raises KeyboardInterrupt)."""
    import termios
    import tty
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError):
        yield
        return
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _plan() -> dict:
    from . import notify
    return notify.plan_window()


def open_window(run_id: str, opener: Optional[Callable] = None, kind: Optional[str] = None) -> bool:
    """Opens a new window that joins the test (--attach); False when there is no way to open one."""
    from . import notify
    argv = [sys.executable, "-m", "stress_test", "--attach", run_id, "--attach-wait", "8"]
    return (opener or notify.open_window)(argv, kind=kind) if opener is None else opener(argv)


def background_message(run_id: str, console: str, pid: int, window: bool = False) -> list:
    head = (f"🪟 The test (run id {run_id}, PID {pid}) is shown in a new terminal window; this terminal is free."
            if window else f"🚀 The test goes on in the background (run id {run_id}, PID {pid}); closing the terminal does not matter.")
    return [head,
            f"   join it again   ./stress.sh --attach {run_id}",
            f"   live status     ./stress.sh --status --live   (key W in the menu)",
            f"   stop it         ./stress.sh --stop {run_id}",
            f"   its output      tail -f {console}"]


def run_attached(console: str, pid: int, run_id: str, out=None) -> dict:
    """The parent's side of a test that was started detached: watch it; returns watch()'s result. Prints what to do after `q`."""
    with raw_keys():
        outcome = watch(console, pid, out=out, window=lambda kind: open_window(run_id, kind=kind), window_plan=_plan)
    if outcome["result"] in ("background", "window"):
        for line in background_message(run_id, console, pid, window=outcome["result"] == "window"):
            ui.emit(line)
    elif outcome["result"] == "stopped":
        ui.emit("🛑 The test was stopped.")
    return outcome


def attach(target: str, wait_s: float = 0.0) -> int:
    """`--attach ID|node|PID`: join a test that runs in the background."""
    matches = []
    for _attempt in range(int(wait_s / 0.25) + 1):                 # a window opened by the test itself may be a moment faster than its record
        matches = [r for r in background.list_running() if target in (r["run_id"], r.get("node"), str(r["pid"]))]
        if matches:
            break
        time.sleep(0.25)
    if not matches:
        ui.emit(f"❌ No running test matches '{target}'. ./stress.sh --status lists them.")
        return 1
    if len(matches) > 1:
        ui.emit(f"❌ '{target}' matches more than one test ({', '.join(r['run_id'] for r in matches)}); give the run id.")
        return 1
    record = matches[0]
    console = record.get("console") or ""
    if not console or not os.path.exists(console):
        ui.emit("❌ This test has no console file to replay (it was started in a way that writes none). ./stress.sh --status --live shows it.")
        return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        ui.emit("❌ Joining a test needs a terminal. tail -f " + console + " follows its output.")
        return 1
    start, skip = 0, False
    try:
        start, skip = int(Path(console + ".frame").read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        pass
    ui.emit(f"{ui.ICON_STAGE} Joining run {record['run_id']} ({record.get('title') or record.get('node', '?')}) - q leaves it running.")
    pid = int(record["pid"])
    outcome = None
    with raw_keys():
        outcome = watch(console, pid, start=start, skip_move=skip, finished=lambda: (not _alive(pid), None),
                        window=lambda kind: open_window(record["run_id"], kind=kind), window_plan=_plan)
    if outcome["result"] in ("background", "window"):
        for line in background_message(record["run_id"], console, pid, window=outcome["result"] == "window"):
            ui.emit(line)
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
