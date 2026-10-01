"""Main menu v2: framed screen, help pages, R repeat, S settings, status rows, state file."""
import json
import re

import pytest

from stress_test import menu, ui
from test_menu import scripted

REAL_LAST_TEMPERATURE = menu.last_temperature           # the fixture replaces it in the tests
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """The real .logs/menu-state.json must never be touched; STRESS_NO_LIVE is restored after the test."""
    monkeypatch.setenv("STRESS_TEST_MENU_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("STRESS_NO_LIVE", "x")
    monkeypatch.delenv("STRESS_NO_LIVE")
    monkeypatch.setattr(menu, "last_temperature", lambda log_dir=None: None)
    return tmp_path / "state.json"


def run(answers, ran=None, **kw):
    ran = [] if ran is None else ran
    code = menu.run_menu(ask=scripted(*answers), run=lambda o: ran.append(o) or 0, cluster=lambda: "cluster: t", **kw)
    return code, ran


# ---------------- screen -----------------------------------------------------------------------------------

@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("width", [40, 60, 76])
def test_menu_lines_rows_have_equal_visible_width(on, width):
    status = ["🟡 a test is running now (2 pods of the tool) - 6 ▸ ADMIN shows it",
              "🔁 last: ./stress.sh --profile classic --net-watch · 2026-09-30 10:00"]
    lines = menu.menu_lines("cluster: 4 nodes (3 workers + 1 master), all Ready", status, on, width)
    assert {ui.visible_len(x) for x in lines} == {width}
    text = ANSI.sub("", "\n".join(lines))
    assert "TESTS" in text and "CLUSTER" in text and "FULL" in text and "K3S·STRESS" in text


def test_menu_lines_show_all_entries_and_keys():
    text = "\n".join(menu.menu_lines("c", [], False, 76))
    for key, name, _hint in menu.MENU_ITEMS:
        assert f"{key} ▸ " in text and name in text
    for key, label, icon in menu.KEYS:
        assert f"{icon} {key} ▸ {label}" in text


def test_menu_box_direct_matches_menu_lines():
    sections = [("A", [("1", "⚡", "CPU", "x" * 200)])]
    box = ui.menu_box("1.0", ["row"], sections, menu.KEYS, False, 50)
    assert {ui.visible_len(x) for x in box} == {50}


# ---------------- help ------------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [40, 60, 76])
def test_help_lines_fit_and_never_split_urls(width):
    lines = menu.help_lines(width)
    assert lines and lines[0][0] == "T"
    for kind, text in lines:
        assert ui.visible_len(text) <= width - 4 or text.strip() in (menu.DOCS_URL, menu.HELPDESK_URL), (width, text)
    texts = [t for _k, t in lines]
    assert menu.DOCS_URL in texts and menu.HELPDESK_URL in texts                 # each URL on one line, whole
    assert not any("github.com" in t and t.strip() not in (menu.DOCS_URL, menu.HELPDESK_URL)
                   and not t.strip().startswith("Full README") for t in texts)


def test_help_covers_every_menu_entry():
    text = "\n".join(t for _k, t in menu.help_lines(76))
    for _key, name, _hint in menu.MENU_ITEMS:
        assert name in text


def test_show_help_paginates_small_terminal(capsys):
    ask = scripted(*[""] * 30)
    menu.show_help(ask, size=(60, 14))
    out = ANSI.sub("", capsys.readouterr().out)
    pages = re.findall(r"HELP (\d+)/(\d+)", out)
    assert len(pages) > 1 and pages[-1][0] == pages[-1][1]
    for line in out.splitlines():
        if line.startswith(("┃", "┏", "┣", "┗")):
            assert ui.visible_len(line) <= 59
    assert len(ask.prompts) == len(pages)
    # no page ends with a lonely title: every page box is at most rows-6+header
    boxes = out.split("┏")[1:]
    assert all(len(b.splitlines()) <= 14 for b in boxes)


def test_show_help_zero_goes_back_early(capsys):
    ask = scripted("0")
    menu.show_help(ask, size=(60, 14))
    assert len(ask.prompts) == 1 and capsys.readouterr().out.count("HELP 1/") == 1


def test_question_mark_opens_help_and_returns(capsys):
    code, ran = run(["?", "", "", "0"])
    out = ANSI.sub("", capsys.readouterr().out)
    assert code == 0 and ran == [] and "HELP 1/" in out


# ---------------- R repeat ---------------------------------------------------------------------------------

def test_repeat_with_nothing_warns(capsys):
    code, ran = run(["R", "0"])
    assert ran == [] and "Nothing to repeat yet." in capsys.readouterr().out


def test_repeat_runs_last_options_exactly():
    code, ran = run(["1", "2", "s", "", "r", "", "0"])
    assert ran == [["--profile", "stepped", "--smart"]] * 2


def test_repeat_ignores_later_setting_changes_only_for_new_actions():
    code, ran = run(["5", "", "", "R", "", "0"])
    assert ran == [["--quick"], ["--quick"]]


# ---------------- S settings -------------------------------------------------------------------------------

def test_settings_persist_and_apply(isolated_state):
    code, ran = run(["S", "1", "worker-7", "2", "80", "0", "5", "", "", "0"])
    assert ran == [["--quick", "--node", "worker-7", "--max-temp", "80"]]
    saved = json.loads(isolated_state.read_text())
    assert saved["settings"] == {"node": "worker-7", "max_temp": 80, "live": True}
    # a new menu session reads them again
    code, ran = run(["1", "1", "", "", "0"])
    assert ran == [["--profile", "classic", "--node", "worker-7", "--max-temp", "80"]]


def test_apply_settings_only_for_profile_and_quick():
    cfg = {"node": "n1", "max_temp": 75, "live": True}
    assert menu.apply_settings(["--quick"], cfg) == ["--quick", "--node", "n1", "--max-temp", "75"]
    assert menu.apply_settings(["--profile", "net"], cfg) == ["--profile", "net", "--node", "n1", "--max-temp", "75"]
    for other in (["--net-matrix"], ["--status"], ["--list-nodes"], ["--self-test"], ["--compare", "a"],
                  ["--stop", "x"], ["--export-log", "a.log"]):
        assert menu.apply_settings(other, cfg) == other
    own = ["--profile", "disk", "--node", "mine", "--max-temp", "60"]
    assert menu.apply_settings(own, cfg) == own                          # own values win
    assert menu.apply_settings(["--quick"], {"node": "", "max_temp": 0, "live": True}) == ["--quick"]


def test_settings_repeat_does_not_add_twice():
    code, ran = run(["S", "1", "n1", "0", "5", "", "", "R", "", "0"])
    assert ran[0] == ran[1] == ["--quick", "--node", "n1"]


def test_settings_invalid_values_and_keys(isolated_state, capsys):
    code, ran = run(["S", "2", "500", "2", "abc", "9", "1", "  ", "0", "0"])
    out = capsys.readouterr().out
    assert "Invalid choice." in out
    assert menu.settings_of(json.loads(isolated_state.read_text()))["max_temp"] == 0
    assert menu.settings_of(json.loads(isolated_state.read_text()))["node"] == ""


def test_settings_live_toggle_and_reset(isolated_state, monkeypatch):
    import os
    run(["S", "3", "0", "0"])
    assert menu.settings_of(json.loads(isolated_state.read_text()))["live"] is False
    assert os.environ.get("STRESS_NO_LIVE") == "menu"
    run(["S", "4", "0", "0"])
    assert menu.settings_of(json.loads(isolated_state.read_text())) == {"node": "", "max_temp": 0, "live": True}
    assert "STRESS_NO_LIVE" not in os.environ


def test_settings_temperature_bounds(isolated_state):
    run(["S", "2", "40", "0", "0"])
    assert json.loads(isolated_state.read_text())["settings"]["max_temp"] == 40
    run(["S", "2", "39", "0", "0"])
    assert json.loads(isolated_state.read_text())["settings"]["max_temp"] == 40
    run(["S", "2", "0", "0", "0"])
    assert json.loads(isolated_state.read_text())["settings"]["max_temp"] == 0


# ---------------- state file -------------------------------------------------------------------------------

def test_state_helpers_roundtrip_and_bad_file(isolated_state):
    assert menu.load_state() == {}
    menu.save_state({"a": 1})
    assert menu.load_state() == {"a": 1}
    isolated_state.write_text("not json")
    assert menu.load_state() == {}
    isolated_state.write_text("[1, 2]")
    assert menu.load_state() == {}
    assert menu.state_path() == isolated_state


def test_injected_run_does_not_persist_last(isolated_state):
    run(["5", "", "", "0"])
    assert not isolated_state.exists()


def test_settings_of_defaults():
    assert menu.settings_of({}) == {"node": "", "max_temp": 0, "live": True}
    assert menu.settings_of({"settings": {"node": "x"}})["node"] == "x"


# ---------------- keys and invalid input ---------------------------------------------------------------------

@pytest.mark.parametrize("key, answers, expected", [
    ("1", ["1", ""], ["--profile", "classic"]),
    ("3", ["2", ""], ["--profile", "disk", "--smart"]),
    ("4", ["2"], ["--net-matrix"]),
    ("5", [""], ["--quick"]),
    ("7", ["3", "a.log"], ["--export-log", "a.log"]),
    ("8", ["3"], ["--list-nodes"]),
    ("6", [], ["--self-test"]),
])
def test_keys_1_to_8_build_the_same_options(key, answers, expected):
    assert menu.build_action(key, scripted(*answers)) == expected


@pytest.mark.parametrize("bad", ["11", "x", "!", "-1"])
def test_invalid_keys_warn_and_continue(bad, capsys):
    code, ran = run([bad, "0"])
    assert ran == [] and "Invalid choice." in capsys.readouterr().out


def test_quit_keys():
    for k in ("0", "q", "Q", ""):
        assert run([k])[0] == 0


# ---------------- status rows -------------------------------------------------------------------------------

class Pod:
    def __init__(self, phase):
        self.phase = phase


class StatusKube:
    def __init__(self, pods):
        self.pods = pods

    def list_tool_pods(self):
        return self.pods


def test_status_rows_running_and_last(monkeypatch):
    monkeypatch.setattr(menu, "last_temperature", lambda log_dir=None: ("w1", 71))
    state = {"last": {"options": ["--quick"], "when": "2026-09-30 10:00"}}
    rows = menu.status_rows(state, StatusKube([Pod("Running"), Pod("Pending"), Pod("Succeeded")]))
    text = "\n".join(rows)
    assert "a test is running now (2 pods" in text and "max 71 °C" in text and "(w1)" in text
    assert "./stress.sh --quick" in text and "2026-09-30 10:00" in text
    assert all(ui.visible_len(r) < 120 for r in rows)


def test_status_rows_quiet_cluster_and_no_history():
    assert menu.status_rows({}, StatusKube([Pod("Succeeded"), Pod("Failed")])) == []


def test_status_rows_survive_broken_kube(monkeypatch):
    class Broken:
        def list_tool_pods(self):
            raise RuntimeError("no cluster")
    assert menu.status_rows({"last": {"options": ["--status"], "when": "t"}}, Broken()) and True
    monkeypatch.setattr(menu, "last_temperature", lambda log_dir=None: (_ for _ in ()).throw(OSError("x")))
    assert menu.status_rows({}, Broken()) == []


def test_status_rows_coloured_do_not_break_width():
    rows = menu.status_rows({"last": {"options": ["--quick"], "when": "t"}}, StatusKube([Pod("Running")]), on=True)
    lines = menu.menu_lines("c", rows, True, 76)
    assert {ui.visible_len(x) for x in lines} == {76}


def test_status_callback_is_used_by_the_loop(capsys):
    code, ran = run(["0"], status=lambda state: ["EXTRA-ROW-XYZ"])
    assert "EXTRA-ROW-XYZ" in capsys.readouterr().out


def test_last_temperature_with_no_logs(tmp_path):
    assert REAL_LAST_TEMPERATURE(tmp_path) is None


def _scan(*names_and_status):
    from stress_test.gpuscan import NodeScan, classify
    out = []
    for name, gpus in names_and_status:
        out.append(classify(NodeScan(name, False, True, gpus, [("NVIDIA", "Quadro P620")] if gpus else [], True)))
    return out


@pytest.fixture(autouse=True)
def _no_real_gpu_scan(monkeypatch):
    from stress_test import menu
    monkeypatch.setattr(menu, "default_gpu_scanner", lambda: _scan(("only-gpu-node", 1), ("plain", 0)))


def test_gpu_dialog_collects_every_setting():
    from stress_test import menu
    answers = iter(["5", "78", "80", "d", "30", "my/img:1", "y", "w"])       # "5" = custom length in the first menu; the others follow the dialog
    seconds = iter(["90"])
    def ask(prompt, default=""):
        if prompt.startswith("Test length"):
            return next(seconds)
        return next(answers)
    opts = menu.gpu_dialog(ask)
    assert opts[:2] == ["--node", "only-gpu-node"] and "gpu" in opts and opts[opts.index("--time") + 1] == "90"
    assert opts[opts.index("--gpu-max-temp") + 1] == "78" and opts[opts.index("--gpu-mem-pct") + 1] == "80"
    assert "--gpu-double" in opts and opts[opts.index("--cooldown") + 1] == "30"
    assert opts[opts.index("--gpu-image") + 1] == "my/img:1" and "--net-watch" in opts and "--gpu-prepull" in opts


def test_gpu_dialog_defaults_and_back():
    from stress_test import menu
    def ask(prompt, default=""):
        return "2" if prompt == "Choice" else default
    opts = menu.gpu_dialog(ask)       # one GPU node: chosen automatically, no node question
    assert opts[:2] == ["--node", "only-gpu-node"] and opts[opts.index("--time") + 1] == "120" and opts[opts.index("--gpu-max-temp") + 1] == "80"
    assert "--gpu-double" not in opts and "--gpu-image" not in opts
    assert "--gpu-prepull" in opts                       # the default answer is yes
    assert menu.gpu_dialog(lambda p, d="": "0") is None


@pytest.mark.parametrize("cols", [50, 80, 120, 160, 200])
def test_gpu_item_is_listed_in_every_width_mode(cols):
    from stress_test import menu
    text = "\n".join(menu.menu_lines("cluster: x", [], False, cols=cols))
    assert "GPU" in text, text


def test_menu_accepts_gpu_choice_and_runs_the_gpu_dialog(capsys):
    code, ran = run(["2", "2"] + [""] * 8 + ["0"])          # 2 min preset, every other question left at its default
    out = capsys.readouterr().out
    assert "Invalid choice." not in out and ran and ran[0][:4] == ["--node", "only-gpu-node", "--profile", "gpu"]
