"""Questions as frames: warnings (self-test confirmations) and values (numbers, times) - A and B of the question redesign."""
import argparse

from stress_test import cli, menu, selftest, ui


def test_warning_frame_is_red_and_titled_warning():
    plain = "\n".join(ui.question("Really?", False, False, 60, warning=True))
    assert "▲ WARNING" in plain and "QUESTION" not in plain
    coloured = "\n".join(ui.question("Really?", False, True, 60, warning=True))
    assert ui.RED in coloured                                              # the title is red
    assert all(ui.visible_len(line) == 60 for line in ui.question("Really?", False, False, 60, warning=True))


def test_input_frame_shows_default_hint_and_keeps_the_width():
    lines = ui.input_question("Duration of one stage (e.g. 90, 3m)", "3m", False, 60, "seconds or with a unit")
    text = "\n".join(lines)
    assert "Duration of one stage" in text and "e.g. 90, 3m" in text and "default  3m" in text and "seconds or with a unit" in text
    assert all(ui.visible_len(line) == 60 for line in lines)
    assert "default" not in "\n".join(ui.input_question("Word", None, False, 60))


def test_ask_int_and_duration_show_a_frame_then_the_value(monkeypatch, capsys):
    answers = iter(["x", "42"])
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: next(answers))
    assert cli.ask_int("How many % of FREE RAM to allocate", 80) == 42
    out = capsys.readouterr().out
    assert "QUESTION" in out and "default  80" in out and "Enter a whole number" in out and "✔ How many % of FREE RAM to allocate → 42" in out
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: "2m")
    assert cli.ask_duration("Duration of one stage (e.g. 90, 3m)", "3m") == 120
    out = capsys.readouterr().out
    assert "default  3m" in out and "✔ Duration of one stage → " in out


def test_menu_number_and_seconds_questions_use_the_frame(capsys):
    assert menu._ask_number(lambda p, d="": "7", "Parallel jobs", 4, 1, 16) == 7
    assert menu._ask_seconds(lambda p, d="": "90", "Length", "60", 10, 600) == 90
    out = capsys.readouterr().out
    assert out.count("QUESTION") == 2 and "a whole number 1-16" in out and "10-600 s" in out


def test_selftest_confirmations_are_warning_frames(capsys):
    args = argparse.Namespace(yes=False, self_test_ack=False, non_interactive=False)
    answers = iter(["y", "START"])
    assert selftest.confirm(args, lambda p, d="": next(answers), [], "standard", []) is True
    out = capsys.readouterr().out
    assert out.count("▲ WARNING") >= 2 and "I understand: the cluster will be fully loaded" in out
    assert "Second confirmation: type START to begin" in out
    args = argparse.Namespace(yes=False, self_test_ack=False, non_interactive=False)
    assert selftest.confirm(args, lambda p, d="": "n", [], "standard", []) is False


# ---------------- the two medium findings of the pty exploration (2026-10-11) ----------------------------------------------

def test_ask_int_checks_the_range_at_the_question(monkeypatch, capsys):
    answers = iter(["5000000", "10", "x", "80"])
    asked = []
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: asked.append(prompt) or next(answers))
    assert cli.ask_int("CPU temperature for automatic stop (60–95 °C)", 85, 60, 95) == 80
    out = capsys.readouterr().out
    assert out.count("The value must be 60-95.") == 2 and "Enter a whole number." in out and "a whole number 60-95" in out
    assert len(asked) == 4                                                # it kept asking until a valid value
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: "7")
    assert cli.ask_int("Anything", 3) == 7                                # without a range any whole number is fine
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: "0")
    answers2 = iter(["0", "5"])
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: next(answers2))
    assert cli.ask_int("At least one", 1, 1, None) == 5


def test_settings_default_node_must_be_a_known_node(monkeypatch, tmp_path):
    state = {}
    monkeypatch.setattr(menu, "_NODES_CACHE", [("worker-1", False, True), ("master-1", True, True)])
    monkeypatch.setattr(menu, "save_state", lambda s: state.update(s))

    def run(*answers):
        it = iter(answers)
        menu.settings_screen(lambda prompt, default="": next(it), state)
    run("1", "-1", "0")
    assert (state.get("settings") or {}).get("node", "") == ""            # "-1" is not a node: refused
    run("1", "worker-1", "0")
    assert state["settings"]["node"] == "worker-1"
    run("1", "2", "0")
    assert state["settings"]["node"] == "master-1"                        # a number from the list
    run("1", "", "0")
    assert state["settings"]["node"] == ""                                # Enter = ask every time (the default shows the old value, so type it empty)


def test_a_range_error_cannot_be_reached_late_in_the_cpu_flow(monkeypatch, capsys):
    """The temperature limit is refused at its own question, before the later questions are asked."""
    asked = []
    answers = iter(["5000000", "85"])
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: asked.append(prompt) or next(answers))
    assert cli.ask_int(f"CPU temperature for automatic stop ({cli.MIN_MAX_TEMP}–{cli.MAX_MAX_TEMP} °C)", cli.DEFAULT_MAX_TEMP,
                       cli.MIN_MAX_TEMP, cli.MAX_MAX_TEMP) == 85
    assert asked == ["CPU temperature for automatic stop", "CPU temperature for automatic stop"]


def test_a_saved_default_node_that_is_not_a_node_name_is_ignored():
    assert "--node" not in menu.apply_settings(["--profile", "classic"], {"node": "-1", "max_temp": 0})
    assert menu.apply_settings(["--profile", "classic"], {"node": "worker-1", "max_temp": 0})[-2:] == ["--node", "worker-1"]
