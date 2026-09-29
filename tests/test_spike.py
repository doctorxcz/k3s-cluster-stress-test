"""Spike test: config validation, command, menu, build_config, subprocess args, log parsing."""
import pytest

from stress_test import cli, series
from stress_test.logparse import parse_log
from stress_test.manifests import build_spike_command, build_stress_command
from stress_test.models import (MASTER_CPU_CAP, PROFILE_SPIKE, SPIKE_LOW_PCT, StressConfig)
from stress_test.parallel import child_args


def _spike(**kw):
    base = dict(node="n", profile=PROFILE_SPIKE, spike_target=100, spike_low_time=5,
                spike_high_time=5, spike_cycles=3, duration=30)
    base.update(kw)
    return StressConfig(**base)


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


# ---------------- models ---------------------------------------------------------------------------------

def test_spike_config_valid():
    cfg = _spike()
    cfg.validate()
    assert cfg.spike and cfg.multi_stage and not cfg.stepped


@pytest.mark.parametrize("kw", [
    {"spike_target": 60}, {"spike_target": None}, {"spike_low_time": 0}, {"spike_high_time": 301},
    {"spike_cycles": 0}, {"duration": 31}, {"ram_pct": 10}, {"hdd": True},
])
def test_spike_config_rejects(kw):
    with pytest.raises(ValueError):
        _spike(**kw).validate()


def test_spike_master_cap():
    cfg = _spike(spike_target=100)
    msgs = cfg.apply_master_limits()
    assert cfg.spike_target == MASTER_CPU_CAP and any("spike target" in m for m in msgs)
    cfg.validate()                                    # the capped 70 % is still valid
    low = _spike(spike_target=50)
    assert not any("spike" in m for m in low.apply_master_limits()) and low.spike_target == 50


# ---------------- command --------------------------------------------------------------------------------

def test_build_spike_command():
    cmd = build_spike_command(75, 4, 2, 3)
    assert cmd.count("stress-ng") == 2                # two runs inside one loop
    assert f"--cpu-load {SPIKE_LOW_PCT}" in cmd and "--cpu-load 75" in cmd
    assert "--timeout 2s" in cmd and "--timeout 3s" in cmd
    assert f'STRESS-STAGE $i/8 {SPIKE_LOW_PCT}%' in cmd and 'STRESS-STAGE $i/8 75%' in cmd
    assert "-le 4" in cmd


def test_stress_command_dispatches_to_spike():
    cfg = _spike(spike_target=50, spike_cycles=3)
    assert build_stress_command(cfg, None) == build_spike_command(50, 3, 5, 5)


# ---------------- menu -----------------------------------------------------------------------------------

@pytest.mark.parametrize("answer, expected", [("", 100), ("1", 25), ("2", 50), ("3", 75), ("4", 100)])
def test_ask_spike_target(monkeypatch, answer, expected):
    _answers(monkeypatch, answer)
    assert cli.ask_spike_target() == expected


def test_ask_spike_target_invalid_then_valid(monkeypatch, capsys):
    _answers(monkeypatch, "9", "2")
    assert cli.ask_spike_target() == 50
    assert "Invalid choice." in capsys.readouterr().out


def test_ask_profile_spike(monkeypatch):
    _answers(monkeypatch, "7", "3")
    assert cli.ask_profile() == PROFILE_SPIKE


# ---------------- build_config ---------------------------------------------------------------------------

def _args(*extra):
    return cli.build_parser().parse_args(["--node", "n", "--profile", "spike", "--max-temp", "85",
                                          "--cooldown", "60", "--no-background", "--notes", "",
                                          "--non-interactive", *extra])


def test_build_config_spike_defaults():
    cfg = cli.build_config(_args(), "n", master_mode=False)
    assert cfg.spike and cfg.spike_target == 100 and cfg.log is True
    assert (cfg.spike_low_time, cfg.spike_high_time) == (5, 5)
    assert cfg.duration == 600 and cfg.spike_cycles == 60


def test_build_config_spike_rounds_to_whole_cycles(capsys):
    cfg = cli.build_config(_args("--time", "47", "--spike-low-time", "5", "--spike-high-time", "5"),
                           "n", master_mode=False)
    assert cfg.spike_cycles == 5 and cfg.duration == 50
    assert "Rounded" in capsys.readouterr().out
    cfg.validate()


def test_build_config_spike_time_shorter_than_one_cycle_gives_one_cycle():
    cfg = cli.build_config(_args("--time", "3"), "n", master_mode=False)
    assert cfg.spike_cycles == 1 and cfg.duration == 10


def test_build_config_spike_ignores_ram_and_disk(capsys):
    cfg = cli.build_config(_args("--ram-pct", "20", "--hdd"), "n", master_mode=False)
    assert cfg.ram_pct is None and cfg.hdd is False
    assert "only the CPU" in capsys.readouterr().out


def test_build_config_spike_asks_when_interactive(monkeypatch):
    args = cli.build_parser().parse_args(["--node", "n", "--profile", "spike", "--max-temp", "85",
                                          "--cooldown", "60", "--no-background", "--notes", ""])
    _answers(monkeypatch, "2", "2", "3", "20")        # target 50 %, low 2 s, high 3 s, total 20 s
    cfg = cli.build_config(args, "n", master_mode=False)
    assert (cfg.spike_target, cfg.spike_low_time, cfg.spike_high_time) == (50, 2, 3)
    assert cfg.spike_cycles == 4 and cfg.duration == 20


# ---------------- subprocess arguments (--parallel) ------------------------------------------------------

def test_child_args_spike_roundtrip():
    cfg = _spike(spike_target=50, spike_low_time=2, spike_high_time=3, spike_cycles=4,
                 duration=20, max_temp=80)
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    a = child_args(cfg, opts, "/l/w.log", 2)
    pairs = dict(zip(a, a[1:]))
    assert pairs["--profile"] == "spike" and pairs["--spike-target"] == "50"
    assert pairs["--spike-low-time"] == "2" and pairs["--spike-high-time"] == "3"
    assert pairs["--time"] == "20"
    parsed = cli.build_parser().parse_args(a)         # what the subprocess will parse
    child = cli.build_config(parsed, "w", master_mode=False)
    assert (child.spike_target, child.spike_cycles, child.duration) == (50, 4, 20)


# ---------------- log parsing ----------------------------------------------------------------------------

SPIKE_LOG = """=== KUBERNETES STRESS-NG LOG ===
Node: n1
Started: 2026-09-29 13:46:13
Test duration: 20 s (20 s)
Profile: spike - 2 cycles of 5 s at 10 % / 5 s at 100 %
Notes:
=== METRICS LOG (TIME | CPU/RAM | TEMPERATURES | CLOCK) ===
[13:46:37] ▶ Low 1/4: 10 % (5 s)
[13:46:37] CPU: 12%, RAM: 1171 MiB (10%) | Temp: CPU: 60°C, GPU: 60°C | Clock: 3100 MHz
[13:46:42] ▶ Spike! 2/4: 100 % (5 s)
[13:46:42] CPU: 98%, RAM: 1171 MiB (10%) | Temp: CPU: 66°C, GPU: 66°C | Clock: 3144 MHz
[13:46:47] ▶ Low 3/4: 10 % (5 s)
[13:46:52] ▶ Spike! 4/4: 100 % (5 s)
Stages (target → measured):
  1/4   10 % → ?     1439.7 bogo ops/s
  2/4  100 % → ?     1862.0 bogo ops/s
"""


def test_parse_log_reads_spike_stage_targets():
    run = parse_log(SPIKE_LOG, "spike.log")
    assert run.profile == "spike"
    assert run.stage_targets == [10, 100, 10, 100]
    assert run.stage_ops == {1: 1439.7, 2: 1862.0}
