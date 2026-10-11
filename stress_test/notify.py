"""Telling the user something outside the program: a desktop notification and a window of the terminal.

Used by the scheduled cluster check (a notification and a window with the report) and by the optional window for a running test.
Everything here is best effort: no display, no notifier or no terminal emulator simply means "not shown" - never an error.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Callable, Optional

# the terminal emulators we know, with the way each one runs a command: (program, arguments before the command)
EMULATORS = (
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("kgx", ["-e"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("foot", []),
    ("ghostty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("xterm", ["-e"]),
)


def has_display(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def desktop_notification_command(title: str, body: str, which: Callable = shutil.which, platform: Optional[str] = None,
                                 env: Optional[dict] = None) -> Optional[list]:
    """The command that shows a notification, None when this system has no way to show one."""
    platform = platform or sys.platform
    if platform == "darwin":
        if which("osascript"):
            script = f'display notification {_applescript(body)} with title {_applescript(title)}'
            return ["osascript", "-e", script]
        return None
    if which("notify-send") and has_display(env):
        return ["notify-send", "--app-name=k3s-stress-test", "--icon=utilities-system-monitor", title, body]
    return None


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify_desktop(title: str, body: str, run: Callable = subprocess.run, **kw) -> bool:
    """Shows a desktop notification; True when one was sent."""
    command = desktop_notification_command(title, body, **kw)
    if not command:
        return False
    try:
        run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=10, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


PASS_ENV = ("PATH", "KUBECONFIG", "KUBECTL", "HOME", "LANG", "LC_ALL", "NO_COLOR")
_NOT_PASSED = ("STRESS_VIEWER", "STRESS_FORCE_LIVE", "STRESS_VIEW_SIZE_FILE", "STRESS_VIEW_FRAME_FILE")     # belong to one detached test only


def with_environment(argv: list, env: Optional[dict] = None) -> list:
    """`env VAR=value ... argv`: a new window gets the environment of the terminal/tmux SERVER, not of this program - it would not find the
    package (PYTHONPATH), the kubeconfig or the settings of this run. Passes on what the program needs."""
    from .paths import PROJECT_ROOT
    env = os.environ if env is None else env
    pairs = {"PYTHONPATH": str(PROJECT_ROOT) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")}
    for key, value in env.items():
        if (key in PASS_ENV or key.startswith("STRESS_")) and key not in _NOT_PASSED and key != "STRESS_TERMINAL":
            pairs[key] = value
    return ["env", *[f"{k}={v}" for k, v in pairs.items()], *argv]


KIND_LABELS = {"tmux-window": "tmux window", "tmux-split": "tmux split", "terminal": "terminal window"}
KIND_SETTINGS = ("auto", "ask", "tmux-window", "tmux-split", "terminal")


def window_kinds(which: Callable = shutil.which, env: Optional[dict] = None) -> list:
    """The ways a new window can be made here: a tmux window and a tmux split (inside tmux), a window of a terminal emulator (with a display
    and a known emulator, or STRESS_TERMINAL)."""
    env = os.environ if env is None else env
    kinds = []
    if env.get("TMUX") and which("tmux"):
        kinds += ["tmux-window", "tmux-split"]
    if (env.get("STRESS_TERMINAL") or "").strip() or (has_display(env) and any(which(program) for program, _b in EMULATORS)):
        kinds.append("terminal")
    return kinds


def plan_window(which: Callable = shutil.which, env: Optional[dict] = None) -> dict:
    """{"kinds": [...], "preset": the kind to use WITHOUT asking or None = ask}. STRESS_WINDOW_KIND (the menu setting) is "auto" (default: the
    first possible kind - a tmux window inside tmux, else a terminal window), "ask" (ask when there is more than one way) or one kind."""
    env = os.environ if env is None else env
    kinds = window_kinds(which, env)
    wanted = (env.get("STRESS_WINDOW_KIND") or "auto").strip()
    if wanted in kinds:
        return {"kinds": kinds, "preset": wanted}
    if wanted == "ask" and len(kinds) > 1:
        return {"kinds": kinds, "preset": None}
    return {"kinds": kinds, "preset": kinds[0] if kinds else None}


def pick_prompt(kinds: list) -> str:
    """The one-line question of a screen: `Open in: 1 tmux window · 2 tmux split · 3 terminal window · Esc cancels`."""
    return "Open in: " + " · ".join(f"{i} {KIND_LABELS[k]}" for i, k in enumerate(kinds, 1)) + " · Esc cancels"


KIND_HELP = {"tmux-window": "a new window in this tmux session",
             "tmux-split": "next to this screen",
             "terminal": "a separate window - move it anywhere"}


def terminal_name(env: Optional[dict] = None, which: Callable = shutil.which) -> str:
    """Which terminal emulator a "terminal window" would be (STRESS_TERMINAL, else the first known one), '' when unknown."""
    env = os.environ if env is None else env
    chosen = (env.get("STRESS_TERMINAL") or "").strip()
    if chosen:
        return os.path.basename(shlex.split(chosen)[0]) if shlex.split(chosen) else ""
    for program, _before in EMULATORS:
        path = which(program)
        if path:
            real = os.path.basename(os.path.realpath(path))
            return real if program == "x-terminal-emulator" else program
    return ""


def pick_lines(kinds: list, on: bool, width: int, env: Optional[dict] = None, which: Callable = shutil.which) -> list:
    """The question of key w as a framed block that cannot be missed (the screens show it instead of their own content)."""
    from . import ui
    extra = []
    for i, kind in enumerate(kinds, 1):
        label = KIND_LABELS[kind] + (f" ({terminal_name(env, which)})" if kind == "terminal" and terminal_name(env, which) else "")
        extra.append("  " + ui.paint(f"{i}", ui.BOLD + ";" + ui.YELLOW, on) + "  " + ui.paint(label.ljust(28), ui.BOLD, on) + ui.paint(KIND_HELP[kind], ui.GREY, on))
    extra.append("  " + ui.paint("Esc", ui.BOLD + ";" + ui.YELLOW, on) + "  " + ui.paint("cancel - the screen stays here", ui.BOLD, on))
    extra.append("  " + ui.paint("⌨ press the number, no Enter needed", ui.GREY, on))
    return ui._question_frame(" 🪟 NEW WINDOW ", ui.YELLOW, "Where should this screen open? (it moves there and this terminal is free)", extra, on, width)


def kind_of_key(key: str, kinds: list) -> Optional[str]:
    """The kind a pressed key stands for (1, 2, 3 or the Czech number-row keys + ě š), None for any other key."""
    digits = {"+": "1", "ě": "2", "š": "3"}
    key = digits.get(key, key)
    return kinds[int(key) - 1] if key.isdigit() and 1 <= int(key) <= len(kinds) else None


def window_command(argv: list, which: Callable = shutil.which, env: Optional[dict] = None, keep_open: bool = True,
                   kind: Optional[str] = None) -> Optional[list]:
    """The command that opens a NEW window running `argv` - of the given kind, or (kind None) the default one: the user's STRESS_TERMINAL,
    else a tmux window inside tmux, else a terminal emulator; None when there is no way.
    With keep_open the window waits for Enter at the end so the report can be read; otherwise it closes with the program, but an error stays."""
    env = os.environ if env is None else env
    inner = " ".join(shlex.quote(a) for a in with_environment(argv, env))
    if keep_open:
        inner += '; echo; read -r -p "Enter closes this window " _'
    else:                                                     # the window closes with the program - but an ERROR stays on the screen
        inner += ' || { echo; echo "The program ended with an error (see above)."; read -r -p "Enter closes this window " _; }'
    shell = ["sh", "-c", inner]
    chosen = (env.get("STRESS_TERMINAL") or "").strip()
    if kind is None:
        kind = "terminal" if chosen else ("tmux-window" if env.get("TMUX") and which("tmux") else "terminal")
    if kind in ("tmux-window", "tmux-split"):
        if not (env.get("TMUX") and which("tmux")):
            return None
        from .paths import PROJECT_ROOT
        if kind == "tmux-split":
            return ["tmux", "split-window", "-h", "-c", str(PROJECT_ROOT), inner]
        return ["tmux", "new-window", "-c", str(PROJECT_ROOT), "-n", "k3s-stress", inner]
    if chosen:                                                 # the user's own choice, e.g. STRESS_TERMINAL="kitty" or "alacritty -e"
        parts = shlex.split(chosen)
        return [*parts, *shell] if parts else None
    if not has_display(env):
        return None
    for program, before in EMULATORS:
        if which(program):
            return [program, *before, *shell]
    return None


def open_window(argv: list, popen: Callable = subprocess.Popen, kind: Optional[str] = None, **kw) -> bool:
    """Opens a window with `argv`; True when it was started. Without a kind the user's preset (the menu setting) is used when there is one."""
    if kind is None:
        kind = plan_window(kw.get("which", shutil.which), kw.get("env"))["preset"]
    command = window_command(argv, kind=kind, **kw)
    if not command:
        return False
    try:
        popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False
