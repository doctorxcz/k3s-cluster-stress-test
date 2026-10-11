"""Desktop notification and terminal window: best effort, never an error."""
import subprocess

from stress_test import notify


def which_of(*names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


def test_notification_command_per_platform():
    cmd = notify.desktop_notification_command("T", "B", which_of("notify-send"), "linux", {"DISPLAY": ":0"})
    assert cmd[0] == "notify-send" and cmd[-2:] == ["T", "B"]
    assert notify.desktop_notification_command("T", "B", which_of("notify-send"), "linux", {}) is None          # no display
    assert notify.desktop_notification_command("T", "B", which_of(), "linux", {"DISPLAY": ":0"}) is None
    mac = notify.desktop_notification_command('say "hi"', "b\\c", which_of("osascript"), "darwin", {})
    assert mac[0] == "osascript" and '\\"hi\\"' in mac[2] and "b\\\\c" in mac[2]
    assert notify.desktop_notification_command("T", "B", which_of(), "darwin", {}) is None


def test_notify_desktop_sends_once_and_survives_errors():
    sent = []
    assert notify.notify_desktop("T", "B", run=lambda *a, **k: sent.append(a[0]), which=which_of("notify-send"), platform="linux", env={"DISPLAY": ":0"})
    assert sent and sent[0][0] == "notify-send"

    def boom(*a, **k):
        raise OSError("x")
    assert notify.notify_desktop("T", "B", run=boom, which=which_of("notify-send"), platform="linux", env={"DISPLAY": ":0"}) is False
    assert notify.notify_desktop("T", "B", run=boom, which=which_of(), platform="linux", env={}) is False


def test_window_command_prefers_tmux_then_known_emulators():
    argv = ["python3", "-m", "stress_test", "--doctor-last"]
    tm = notify.window_command(argv, which_of("tmux", "kitty"), {"TMUX": "/tmp/x,1,0", "DISPLAY": ":0"})
    assert tm[:3] == ["tmux", "new-window", "-c"] and tm[4:6] == ["-n", "k3s-stress"] and "stress_test" in tm[-1] and "Enter closes" in tm[-1]
    gt = notify.window_command(argv, which_of("gnome-terminal", "xterm"), {"DISPLAY": ":0"})
    assert gt[:2] == ["gnome-terminal", "--"] and gt[2:4] == ["sh", "-c"]
    assert notify.window_command(argv, which_of("kitty"), {"WAYLAND_DISPLAY": "wayland-0"})[0] == "kitty"
    assert notify.window_command(argv, which_of("gnome-terminal"), {}) is None                                   # ssh: no display
    assert notify.window_command(argv, which_of(), {"DISPLAY": ":0"}) is None
    closing = notify.window_command(argv, which_of("xterm"), {"DISPLAY": ":0"}, keep_open=False)[-1]
    assert "; echo; read" not in closing and "||" in closing                                                    # closes on success, waits on an error
    quoted = notify.window_command(["a b", "it's"], which_of("xterm"), {"DISPLAY": ":0"}, keep_open=False)[-1]
    assert "'a b'" in quoted


def test_open_window_starts_a_detached_process():
    started = []
    assert notify.open_window(["x"], popen=lambda *a, **k: started.append((a, k)), which=which_of("xterm"), env={"DISPLAY": ":0"})
    assert started[0][1]["start_new_session"] is True

    def boom(*a, **k):
        raise OSError("no")
    assert notify.open_window(["x"], popen=boom, which=which_of("xterm"), env={"DISPLAY": ":0"}) is False
    assert notify.open_window(["x"], popen=boom, which=which_of(), env={}) is False


def test_stress_terminal_overrides_the_choice():
    argv = ["python3", "-m", "stress_test"]
    chosen = notify.window_command(argv, which_of("tmux", "kitty"), {"STRESS_TERMINAL": "alacritty -e", "TMUX": "x", "DISPLAY": ":0"})
    assert chosen[:2] == ["alacritty", "-e"] and chosen[2:4] == ["sh", "-c"]
    assert notify.window_command(argv, which_of(), {"STRESS_TERMINAL": "  "}) is None


def test_a_new_window_gets_the_package_the_environment_and_keeps_an_error_on_the_screen():
    """The window's shell has the environment of the terminal / tmux SERVER: without PYTHONPATH `python -m stress_test` was 'No module named
    stress_test' and the window flashed and closed (found by the user on 2026-10-11)."""
    import os
    import subprocess
    import sys
    cmd = notify.window_command([sys.executable, "-m", "stress_test", "--doctor-last"], which_of(), {"DISPLAY": ":0", "STRESS_TERMINAL": "xterm -e",
                                "STRESS_VIEWER": "1", "STRESS_NO_LIVE": "1", "KUBECONFIG": "/kube", "PATH": "/bin"}, keep_open=False)
    inner = cmd[-1]
    assert "PYTHONPATH=" in inner and "KUBECONFIG=/kube" in inner and "STRESS_NO_LIVE=1" in inner
    assert "STRESS_VIEWER" not in inner and "STRESS_TERMINAL" not in inner                      # these belong to one test / the choice itself
    assert "ended with an error" in inner and "Enter closes" in inner                            # an error stays visible
    clean = subprocess.run(["env", "-i", f"HOME={os.environ['HOME']}", "PATH=/usr/bin:/bin", "sh", "-c", inner], cwd="/", stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    assert "No module named" not in clean.stdout + clean.stderr and clean.returncode in (0, 1)


def test_window_kinds_plan_prompt_and_keys():
    tmux_env = {"TMUX": "x", "DISPLAY": ":0"}
    assert notify.window_kinds(which_of("tmux", "xterm"), tmux_env) == ["tmux-window", "tmux-split", "terminal"]
    assert notify.window_kinds(which_of("tmux"), {"TMUX": "x"}) == ["tmux-window", "tmux-split"]
    assert notify.window_kinds(which_of("xterm"), {"DISPLAY": ":0"}) == ["terminal"]
    assert notify.window_kinds(which_of("xterm"), {}) == [] and notify.window_kinds(which_of("tmux"), {"TMUX": ""}) == []
    assert notify.window_kinds(which_of(), {"STRESS_TERMINAL": "kitty"}) == ["terminal"]
    # default "auto": the first possible kind, no question; "ask": ask when there is more than one way; the user's choice wins when possible
    assert notify.plan_window(which_of("tmux", "xterm"), tmux_env)["preset"] == "tmux-window"
    assert notify.plan_window(which_of("tmux", "xterm"), {**tmux_env, "STRESS_WINDOW_KIND": "ask"})["preset"] is None
    assert notify.plan_window(which_of("xterm"), {"DISPLAY": ":0", "STRESS_WINDOW_KIND": "ask"})["preset"] == "terminal"          # one way: no question
    assert notify.plan_window(which_of("xterm"), {"DISPLAY": ":0"}) == {"kinds": ["terminal"], "preset": "terminal"}
    assert notify.plan_window(which_of("tmux", "xterm"), {**tmux_env, "STRESS_WINDOW_KIND": "tmux-split"})["preset"] == "tmux-split"
    assert notify.plan_window(which_of("xterm"), {"DISPLAY": ":0", "STRESS_WINDOW_KIND": "tmux-split"})["preset"] == "terminal"     # not possible here
    assert notify.plan_window(which_of(), {}) == {"kinds": [], "preset": None}
    assert notify.plan_window(which_of("tmux"), {"TMUX": "x"})["preset"] == "tmux-window"                                       # in tmux: a tmux window
    kinds = ["tmux-window", "tmux-split", "terminal"]
    assert notify.pick_prompt(kinds) == "Open in: 1 tmux window · 2 tmux split · 3 terminal window · Esc cancels"
    assert [notify.kind_of_key(k, kinds) for k in ("1", "2", "3", "4", "x", "esc", "+", "ě", "š")] == [
        "tmux-window", "tmux-split", "terminal", None, None, None, "tmux-window", "tmux-split", "terminal"]


def test_window_command_per_kind():
    argv = ["python3", "-m", "stress_test", "--dashboard"]
    env = {"TMUX": "x", "DISPLAY": ":0"}
    window = notify.window_command(argv, which_of("tmux", "xterm"), env, keep_open=False, kind="tmux-window")
    split = notify.window_command(argv, which_of("tmux", "xterm"), env, keep_open=False, kind="tmux-split")
    term = notify.window_command(argv, which_of("tmux", "xterm"), env, keep_open=False, kind="terminal")
    assert window[:3] == ["tmux", "new-window", "-c"] and split[:2] == ["tmux", "split-window"] and "-h" in split
    assert term[0] == "xterm" and term[1:4] == ["-e", "sh", "-c"]
    assert notify.window_command(argv, which_of("xterm"), {"DISPLAY": ":0"}, kind="tmux-split") is None          # not in tmux
    assert notify.window_command(argv, which_of("tmux"), {"TMUX": "x"}, kind="terminal") is None                  # no display / emulator
    assert notify.window_command(argv, which_of("tmux", "xterm"), env, keep_open=False)[0] == "tmux"              # no kind: the old default
    seen = []
    assert notify.open_window(argv, popen=lambda c, **k: seen.append(c), which=which_of("tmux", "xterm"), env={**env, "STRESS_WINDOW_KIND": "terminal"})
    assert seen[0][0] == "xterm"                                                                                  # the preset is used without a kind


def test_the_window_question_is_a_framed_block_with_the_terminal_named():
    from stress_test import ui
    kinds = ["tmux-window", "tmux-split", "terminal"]
    lines = notify.pick_lines(kinds, False, 90, {"DISPLAY": ":0"}, which_of("x-terminal-emulator"))
    text = "\n".join(lines)
    assert "NEW WINDOW" in lines[0] and "Where should this screen open?" in text
    assert "1  tmux window" in text and "2  tmux split" in text and "3  terminal window (" in text and "Esc  cancel" in text
    assert all(ui.visible_len(line) == 90 for line in lines)
    assert "\x1b[" in "\n".join(notify.pick_lines(kinds, True, 90, {}, which_of()))
    assert notify.terminal_name({"STRESS_TERMINAL": "kitty -1"}, which_of()) == "kitty" and notify.terminal_name({}, which_of()) == ""
