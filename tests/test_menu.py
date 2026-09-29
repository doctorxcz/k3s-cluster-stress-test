"""Main menu of a bare ./stress.sh: choices -> option lists, the loop, the header, end-to-end with the fake kubectl."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stress_test import cli, menu
from stress_test.models import NodeInfo
from stress_test.net import normalize_extras

ROOT = Path(__file__).parent.parent
FAKE = Path(__file__).parent / "fake_kubectl.py"


def scripted(*answers):
    """An `ask` replacement returning the answers one by one (records the prompts)."""
    it = iter(answers)
    prompts = []

    def ask(prompt, default=None):
        prompts.append(prompt)
        try:
            value = next(it)
        except StopIteration as exc:
            raise EOFError from exc
        return value if value != "" else (default or "")
    ask.prompts = prompts
    return ask


# ---------------- when the menu appears -------------------------------------------------------------------

def test_enabled_only_for_a_bare_terminal_run(monkeypatch):
    monkeypatch.delenv("STRESS_NO_MENU", raising=False)
    assert menu.enabled([], stdin_is_tty=True)
    assert not menu.enabled([], stdin_is_tty=False)                 # a pipe / cron
    assert not menu.enabled(["--node", "x"], stdin_is_tty=True)     # any option = the old behaviour
    monkeypatch.setenv("STRESS_NO_MENU", "1")
    assert not menu.enabled([], stdin_is_tty=True)


# ---------------- header -----------------------------------------------------------------------------------

class FakeKube:
    def __init__(self, nodes):
        self.nodes = nodes

    def list_node_names(self):
        return [n.name for n in self.nodes]

    def get_node(self, name):
        return next(n for n in self.nodes if n.name == name)


def _node(name, master=False, ready=True):
    return NodeInfo(name=name, ready=ready, is_control_plane=master, allocatable_mem_mib=1000)


def test_cluster_line_counts_and_flags_problems():
    ok = FakeKube([_node("a"), _node("b"), _node("m", master=True)])
    assert menu.cluster_line(ok) == "cluster: 3 nodes (2 workers + 1 master), all Ready"
    bad = FakeKube([_node("a", ready=False), _node("m", master=True)])
    assert "NOT Ready: a" in menu.cluster_line(bad)


def test_cluster_line_survives_a_broken_cluster():
    class Broken:
        def list_node_names(self):
            raise RuntimeError("boom")
    assert "not reachable" in menu.cluster_line(Broken())


# ---------------- choices ----------------------------------------------------------------------------------

@pytest.mark.parametrize("answers, expected", [
    (("1", "1", ""), ["--profile", "classic"]),
    (("1", "2", "s"), ["--profile", "stepped", "--smart"]),
    (("1", "3", "sw"), ["--profile", "spike", "--smart", "--net-watch"]),
    (("2", "1", ""), ["--profile", "disk"]),
    (("2", "2", "w"), ["--profile", "disk", "--smart", "--net-watch"]),
    (("3", "1"), ["--profile", "net"]),
    (("3", "2"), ["--net-matrix"]),
    (("4", "w"), ["--quick", "--net-watch"]),
    (("5", "1", "n1 n2"), ["--compare", "n1", "n2"]),
    (("5", "1", "dell-9020"), ["--compare", "dell-9020"]),
    (("5", "2", "dell-9020"), ["--set-baseline", "dell-9020"]),
    (("5", "3", "some.log"), ["--export-log", "some.log"]),
    (("6", "1"), ["--status"]),
    (("6", "2", "abc123"), ["--stop", "abc123"]),
    (("6", "3"), ["--list-nodes"]),
])
def test_build_action(answers, expected):
    ask = scripted(*answers[1:])
    assert menu.build_action(answers[0], ask) == expected


@pytest.mark.parametrize("choice, answers", [
    ("1", ("0",)), ("2", ("",)), ("3", ("0",)), ("5", ("0",)), ("6", ("0",)),
    ("5", ("1", "")), ("5", ("2", "")), ("6", ("2", "")), ("9", ()),
])
def test_build_action_backs_out(choice, answers):
    assert menu.build_action(choice, scripted(*answers)) is None


def test_submenu_rejects_bad_numbers_then_accepts():
    ask = scripted("7", "x", "2")
    assert menu.build_action("3", ask) == ["--net-matrix"]


def test_command_text():
    assert menu.command_text(["--profile", "disk", "--smart"]) == "./stress.sh --profile disk --smart"


# ---------------- the loop -----------------------------------------------------------------------------------

def test_loop_runs_actions_and_returns_to_the_menu(capsys):
    ran = []
    ask = scripted("1", "1", "w", "", "6", "3", "", "0")      # CPU classic +watch, Enter, list nodes, Enter, quit
    code = menu.run_menu(ask=ask, run=lambda o: ran.append(o) or 0, cluster=lambda: "cluster: test")
    assert code == 0
    assert ran == [["--profile", "classic", "--net-watch"], ["--list-nodes"]]
    out = capsys.readouterr().out
    assert out.count("KUBERNETES STRESS TEST") == 3 and "cluster: test" in out       # the menu came back after each run
    assert "▶ ./stress.sh --profile classic --net-watch" in out


def test_loop_reports_exit_code_and_survives_errors(capsys):
    def run(options):
        if options == ["--status"]:
            return 3
        raise SystemExit(2)
    ask = scripted("6", "1", "", "6", "3", "", "0")
    assert menu.run_menu(ask=ask, run=run, cluster=lambda: "c") == 0
    assert "(exit code 3)" in capsys.readouterr().out


def test_loop_invalid_choice_and_end_of_input(capsys):
    ask = scripted("9", "")                                    # invalid, then the input ends
    assert menu.run_menu(ask=ask, run=lambda o: 0, cluster=lambda: "c") == 0
    assert "Invalid choice." in capsys.readouterr().out


def test_loop_quit_letters():
    assert menu.run_menu(ask=scripted("q"), run=lambda o: 0, cluster=lambda: "c") == 0


# ---------------- interactive network questions ------------------------------------------------------------

def test_ask_net_mode_and_extra(monkeypatch):
    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert cli.ask_net_mode() == "pod"
    answers = iter(["bogus", "dns, mtr"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert cli.ask_net_extra() == "dns,mtr"
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    assert cli.ask_net_extra() == ""


def test_build_config_net_asks_mode_extra_and_time(monkeypatch):
    answers = iter(["2", "dns,service", "5"])                  # pod network, extras, 5 s
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    args = cli.build_parser().parse_args(["--node", "n", "--profile", "net", "--max-temp", "85", "--cooldown", "60",
                                          "--no-background", "--notes", "", "--no-log"])
    cfg = cli.build_config(args, "n", master_mode=False)
    assert (cfg.net_mode, cfg.net_extra, cfg.net_time) == ("pod", normalize_extras(["dns", "service"]), 5)
    cfg.validate()


# ---------------- end to end -------------------------------------------------------------------------------

def _env(tmp_path):
    return dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT),
                STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))


def test_menu_end_to_end_lists_nodes_and_quits(tmp_path):
    code = "from stress_test import menu; raise SystemExit(menu.run_menu())"
    res = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=_env(tmp_path), text=True,
                         input="6\n3\n\n0\n", capture_output=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cluster: 1 nodes (1 workers + 0 master), all Ready" in res.stdout
    assert "▶ ./stress.sh --list-nodes" in res.stdout and "fake-node" in res.stdout
    assert res.stdout.count("KUBERNETES STRESS TEST") == 2


def test_bare_run_without_a_terminal_keeps_the_old_behaviour(tmp_path):
    res = subprocess.run([sys.executable, "-m", "stress_test"], cwd=tmp_path, env=_env(tmp_path), text=True,
                         input="", capture_output=True, timeout=60)
    assert "Choice" not in res.stdout and "[6] Management" not in res.stdout       # no menu on a pipe
