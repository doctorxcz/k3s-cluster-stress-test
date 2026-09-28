"""Interactive cooldown question (ask_cooldown) and --cooldown in build_config."""
import pytest

from stress_test import cli
from stress_test.models import COOLDOWN_DEFAULT, COOLDOWN_MAX


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


@pytest.mark.parametrize("answer, expected", [
    ("", COOLDOWN_DEFAULT), ("1", 60), ("2", 180), ("3", 300), ("4", COOLDOWN_MAX), ("0", 0)])
def test_ask_cooldown_menu(monkeypatch, answer, expected):
    _answers(monkeypatch, answer)
    assert cli.ask_cooldown() == expected


def test_ask_cooldown_custom_and_invalid(monkeypatch, capsys):
    _answers(monkeypatch, "9", "5", "20m", "90")
    assert cli.ask_cooldown() == 90
    out = capsys.readouterr().out
    assert "Invalid choice." in out and "The maximum is" in out


def _args(*extra):
    return cli.build_parser().parse_args(["--node", "n", "--profile", "classic", "--time", "10",
                                          "--max-temp", "85", "--cpu-load", "100",
                                          "--no-hdd", "--no-background", "--no-log",
                                          "--notes", "", *extra])


def test_build_config_asks_cooldown_when_not_given(monkeypatch):
    _answers(monkeypatch, "2")
    args = _args("--ram-pct", "10")
    assert cli.build_config(args, "n", master_mode=False).cooldown == 180


def test_build_config_uses_cooldown_flag_without_asking(monkeypatch):
    _answers(monkeypatch)                  # no question must come
    args = _args("--ram-pct", "10", "--cooldown", "0")
    assert cli.build_config(args, "n", master_mode=False).cooldown == 0


def test_build_config_non_interactive_default():
    args = _args("--non-interactive")
    assert cli.build_config(args, "n", master_mode=False).cooldown == COOLDOWN_DEFAULT


# --- --quick -------------------------------------------------------------------------------
def test_quick_fills_defaults_without_questions(monkeypatch):
    _answers(monkeypatch)                  # no question must come
    args = cli.build_parser().parse_args(["--quick", "--node", "n"])
    cli.apply_quick(args)
    cfg = cli.build_config(args, "n", master_mode=False)
    assert (cfg.duration, cfg.cpu_load, cfg.ram_pct, cfg.hdd, cfg.log, cfg.background,
            cfg.cooldown, cfg.notes, cfg.profile) == (
        cli.QUICK_DURATION, 100, None, False, True, False, COOLDOWN_DEFAULT, "quick", "classic")
    assert cli._choose_scope(args) == "single"


def test_quick_explicit_flags_win(monkeypatch):
    _answers(monkeypatch)
    args = cli.build_parser().parse_args(["-q", "--node", "n", "--time", "2m", "--ram-pct", "50",
                                          "--cooldown", "0", "--no-log"])
    cli.apply_quick(args)
    cfg = cli.build_config(args, "n", master_mode=False)
    assert (cfg.duration, cfg.ram_pct, cfg.cooldown, cfg.log) == (120, 50, 0, False)
    assert "RAM 50 %" in cli.quick_summary(cfg) and "no cooldown" in cli.quick_summary(cfg)
