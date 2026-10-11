"""The menus take a single key at once - no Enter (real pseudo terminal, fake kubectl)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stress_test import menu                                                    # noqa: E402
from test_keep_pty import _run_in_pty                                    # noqa: E402


def test_q_leaves_the_main_menu_without_enter(tmp_path):
    code, text = _run_in_pty([], tmp_path, ["q"])
    assert code == 0 and "K3S·STRESS" in text, text[-500:]


def test_d_opens_the_dashboard_at_once_and_q_comes_back_to_the_menu(tmp_path):
    code, text = _run_in_pty([], tmp_path, ["d", "q", "q"], wait=3.0)
    assert code == 0, text[-600:]
    assert "DASHBOARD" in text and "./stress.sh --dashboard" in text
    assert text.count("K3S·STRESS") >= 2                                   # the menu came back after the dashboard


def test_a_submenu_number_and_back_need_no_enter(tmp_path):
    # 8 = ADMIN, 0 = back, q = quit
    code, text = _run_in_pty([], tmp_path, ["8", "0", "q"], wait=3.0)
    assert code == 0 and "Management" in text.replace("MANAGEMENT", "Management"), text[-600:]
    assert text.count("K3S·STRESS") >= 2


def test_help_key_and_esc(tmp_path):
    code, text = _run_in_pty([], tmp_path, ["?", "0", "q"], wait=3.0)
    assert code == 0 and "HELP" in text, text[-600:]


def test_enter_alone_takes_the_default_which_is_quit(tmp_path):
    code, text = _run_in_pty([], tmp_path, ["\r"], wait=3.0)
    assert code == 0


def test_czech_keyboard_keys_work_as_digits(tmp_path):
    from stress_test import cli
    assert [cli.CZECH_DIGITS[k] for k in "+ěščřžýáíé"] == list("1234567890")
    # "ž" = 6 starts the FULL self-test submenu, "é" = 0 goes back, q quits
    code, text = _run_in_pty([], tmp_path, ["8", "é", "q"], wait=3.0)
    assert code == 0 and text.count("K3S·STRESS") >= 2, text[-500:]
    code, text = _run_in_pty([], tmp_path, ["+", "é", "q"], wait=3.0)                 # "+" = 1 = the CPU menu
    assert code == 0 and "TEST TYPE" in text.upper() or "CPU" in text, text[-500:]


def test_yes_no_questions_need_enter(monkeypatch):
    """Only the menus take a bare key; y/n goes through the line question (a typed answer is confirmed with Enter)."""
    from stress_test import cli
    calls = []
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: calls.append(prompt) or "y")
    monkeypatch.setattr(cli, "ask_key", lambda *a, **k: (_ for _ in ()).throw(AssertionError("y/n must not use the bare-key reader")))
    assert cli.ask_yes_no("Continue?") is True and calls == ["y/n"]


# ---------------- the y/n question as a frame ---------------------------------------------------------------------

def test_question_frame_splits_the_explanation_and_marks_the_default(capsys):
    from stress_test import ui
    assert ui.split_question("Run in the background? (survives closing the terminal)") == ("Run in the background?", "survives closing the terminal")
    assert ui.split_question("Continue with the test?") == ("Continue with the test?", "")
    plain = "\n".join(ui.question("Run in the background? (survives closing the terminal)", False, False, 60))
    assert "QUESTION" in plain and "survives closing the terminal" in plain and "n = no  ◀ default" in plain and "y = yes  ◀" not in plain
    assert "y = yes  ◀ default" in "\n".join(ui.question("Continue?", True, False, 60))
    assert all(ui.visible_len(line) == 60 for line in ui.question("Run it? (" + "long explanation " * 10 + ")", True, False, 60))
    ui.answered("Run in the background? (x)", True)
    assert "✔ Run in the background? → yes" in capsys.readouterr().out


def test_question_frame_has_no_colour_codes_without_colour():
    from stress_test import ui
    assert "\x1b[" not in "\n".join(ui.question("Continue?", True, False))
    assert "\x1b[" in "\n".join(ui.question("Continue?", True, True))


def test_ask_yes_no_shows_the_frame_and_the_answer(monkeypatch, capsys):
    from stress_test import cli
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: "n")
    assert cli.ask_yes_no("Start the test? (all nodes)", True) is False
    out = capsys.readouterr().out
    assert "QUESTION" in out and "all nodes" in out and "✔ Start the test? → no" in out


# ---------------- how to answer: the badge, the frame rows, the switch (2026-10-11) -------------------------------------------

def test_the_prompt_says_how_to_answer(monkeypatch):
    from stress_test import ui
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "120")
    assert ui.prompt("Choice", "q", key=True) == "stress@k3s:~$ Choice [q] · press a key ▸ "
    assert ui.prompt("Value", "3m", key=False) == "stress@k3s:~$ Value [3m] · type, then Enter ⏎ ▸ "
    assert ui.prompt("Old", "x") == "stress@k3s:~$ Old [x] ▸ "                         # callers that do not say keep the old look
    assert "[]" not in ui.prompt("Extras", "", key=False)                               # an empty default shows no brackets
    assert ui.prompt("Q", None, key=False) == "stress@k3s:~$ Q · type, then Enter ⏎ ▸ "


def test_the_badge_shrinks_on_a_narrow_window(monkeypatch):
    from stress_test import ui
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "34")
    text = ui.prompt("Choice", "q", key=True)
    assert text.endswith("▸ ") and ui.visible_len(text) <= 33 and "key" in text
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "20")
    last = ui.prompt("A very long question that does not fit", "x", key=False).splitlines()[-1]
    assert "Enter" not in last and last.endswith("▸ ")                                   # no room for the badge: it is dropped


def test_question_frames_end_with_how_to_answer():
    from stress_test import ui
    assert "type y or n, then press Enter ⏎" in "\n".join(ui.question("Go on?", True, False, 60))
    assert "type the answer, then press Enter ⏎" in "\n".join(ui.input_question("How many?", "3", False, 60))
    assert all(ui.visible_len(line) == 60 for line in ui.question("Go on?", True, False, 60) + ui.input_question("How many?", "3", False, 60))


def test_the_menu_keys_switch(monkeypatch):
    from stress_test import cli
    monkeypatch.setenv("STRESS_NO_KEYS", "menu")
    calls = []
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: calls.append(prompt) or "x")
    assert cli.ask_key("Choice", "q") == "x" and calls == ["Choice"]                   # Enter-everywhere mode falls back to the line question
    state = {}
    monkeypatch.setattr(menu, "save_state", lambda s: state.update(s))
    monkeypatch.delenv("STRESS_NO_KEYS")
    answers = iter(["7", "0"])
    menu.settings_screen(lambda prompt, default="": next(answers), state)
    assert state["settings"]["keys"] is False and os.environ.get("STRESS_NO_KEYS") == "menu"
    answers = iter(["7", "0"])
    menu.settings_screen(lambda prompt, default="": next(answers), state)
    assert state["settings"]["keys"] is True and "STRESS_NO_KEYS" not in os.environ


def test_help_explains_the_two_kinds_of_prompt():
    text = " ".join(t for _kind, t in menu.help_lines(200))
    assert "press a key" in text and "type, then Enter" in text


def test_in_a_terminal_the_menu_prompt_says_press_a_key_and_the_switch_needs_enter(tmp_path):
    code, text = _run_in_pty([], tmp_path, ["q"])
    assert code == 0 and "press a key ▸" in text
    code, text = _run_in_pty([], tmp_path, ["q"], env_extra={"STRESS_NO_KEYS": "1"}, wait=3.0)
    assert "type, then Enter ⏎ ▸" in text and code is not None


# ---------------- key A: join a running test -----------------------------------------------------------------------------------

def test_key_a_joins_the_only_running_test_or_picks_one(monkeypatch, capsys):
    from stress_test import background
    one = [{"run_id": "aaa111", "node": "w1", "title": "CPU test", "console": "/x/aaa111.console.txt", "pid": 1}]
    monkeypatch.setattr(background, "list_running", lambda: one)
    assert menu.join_choice(lambda p, d="": "") == "aaa111"                              # no question when there is one
    two = one + [{"run_id": "bbb222", "node": "w2", "title": "GPU test", "console": "/x/bbb222.console.txt", "pid": 2}]
    monkeypatch.setattr(background, "list_running", lambda: two)
    answers = iter(["2"])
    assert menu.join_choice(lambda p, d="": next(answers)) == "bbb222"
    assert menu.join_choice(lambda p, d="": "0") is None                                # back
    monkeypatch.setattr(background, "list_running", lambda: [{"run_id": "ccc", "node": "n", "pid": 3}])      # no console: nothing to join
    assert menu.join_choice(lambda p, d="": "") is None
    assert "No test is running that can be joined" in capsys.readouterr().out


def test_key_a_runs_attach_and_comes_back_to_the_menu(monkeypatch):
    from stress_test import background
    monkeypatch.setattr(background, "list_running", lambda: [{"run_id": "aaa111", "node": "w1", "console": "/x/c", "pid": 1}])
    ran = []
    answers = iter(["a", "q"])
    assert menu.run_menu(ask=lambda p, d="": next(answers), run=lambda o: ran.append(o) or 0, cluster=lambda: "c") == 0
    assert ran == [["--attach", "aaa111"]]


def test_the_keys_row_and_the_help_know_key_a():
    rows = "\n".join(menu.menu_lines("c", [], False, cols=80))
    assert "🔗 A" in rows
    assert "join a test that runs in the background" in " ".join(t for _k, t in menu.help_lines(200))
