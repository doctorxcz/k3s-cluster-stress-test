"""Concurrent test: API guard, reading progress from the log, table, subprocess arguments."""
import pytest

from stress_test import parallel, series
from stress_test.models import NodeInfo, NodeWorkload, StressConfig
from stress_test.parallel import (ApiGuard, LiveInfo, LiveTable, child_args, describe_state,
                                  format_row, outcome_from_log, tail_status)
from test_compare import make_log, make_stepped_log


def node(name, master=False):
    return NodeInfo(name=name, ready=True, is_control_plane=master, allocatable_mem_mib=8000,
                    capacity_cpu="8", os_image="x", kernel="1", architecture="amd64", runtime="r")


# ---------------- API response guard ------------------------------------------------------------

def test_api_guard_needs_consecutive_bad_readings():
    g = ApiGuard(limit=3.0, consecutive=2)
    assert g.record(True, 0.4) is False
    assert g.record(True, 3.5) is False                 # one slow response is not enough
    assert g.record(True, 0.5) is False and g.bad == 0  # recovery resets the counter
    assert g.record(False, 0.1) is False                # an outage counts the same as slowness
    assert g.record(True, 5.0) is True                  # the second bad one in a row -> stop
    assert g.last_seconds == 5.0


def test_api_guard_limit_is_inclusive_for_ok_response():
    g = ApiGuard(limit=3.0)
    assert g.record(True, 3.0) is False and g.bad == 0


def test_api_guard_check_uses_readyz_and_handles_failures():
    class Kube:
        def __init__(self, fail): self.fail, self.calls = fail, []

        def run(self, *args, timeout=None):
            self.calls.append((args, timeout))
            if self.fail:
                from stress_test.kube import KubectlError
                raise KubectlError("unavailable")
            return "ok"
    ok, bad = Kube(False), Kube(True)
    g = ApiGuard(limit=3.0, consecutive=1)
    assert g.check(ok) is False and ok.calls[0][0] == ("get", "--raw", "/readyz")
    assert ok.calls[0][1] >= 9.0                                        # timeout > limit
    assert ApiGuard(limit=3.0, consecutive=1).check(bad) is True        # outage = bad


# ---------------- progress from the end of a log ---------------------------------------------------------------

def test_tail_status_classic_and_cooling(tmp_path):
    f = tmp_path / "a.log"
    f.write_text(make_log([70, 72, 74], [66, 64], summary=False), encoding="utf-8")
    info = tail_status(str(f))
    assert info.has_sample and info.cooling and info.temp == 64 and info.stage is None
    f.write_text(make_log([70, 72, 74], [], summary=False), encoding="utf-8")
    info = tail_status(str(f))
    assert not info.cooling and info.temp == 74 and info.cpu == 100.0 and info.freq == 3591


def test_tail_status_stepped_reports_current_stage(tmp_path):
    f = tmp_path / "s.log"
    f.write_text(make_stepped_log([47, 58, 70, 84], cool=()), encoding="utf-8")
    info = tail_status(str(f))
    assert info.stage == (4, 4, 100) and info.temp == 84 and not info.cooling


def test_tail_status_missing_or_empty_file(tmp_path):
    assert tail_status(str(tmp_path / "nothing.log")) == LiveInfo()
    (tmp_path / "e.log").write_text("=== KUBERNETES STRESS-NG LOG ===\n", encoding="utf-8")
    assert tail_status(str(tmp_path / "e.log")).has_sample is False


def test_describe_state_variants():
    assert describe_state(LiveInfo(), None) == "preparing"
    assert describe_state(LiveInfo(has_sample=True), None) == "load"
    assert describe_state(LiveInfo(has_sample=True, cooling=True), None) == "cooldown"
    assert describe_state(LiveInfo(has_sample=True, stage=(3, 4, 75)), None) == "▶ Stage 3/4 (75 %)"
    assert describe_state(LiveInfo(), 0) == "done: OK"
    assert describe_state(LiveInfo(), 3) == "OVERHEATED" and describe_state(LiveInfo(), 130) == "INTERRUPTED"
    assert describe_state(LiveInfo(), 4) == "PREMATURE" and describe_state(LiveInfo(), 1) == "ERROR"


def test_format_row():
    info = LiveInfo(cpu=74.4, temp=71, freq=3591, stage=(3, 4, 75), has_sample=True)
    row = format_row("worker-1", info, None, 400)
    assert row.startswith("worker-1") and "▶ Stage 3/4 (75 %)" in row
    assert "CPU   74 %" in row and "71 °C" in row and "3591 MHz" in row and "left ~6 min 40 s" in row
    done = format_row("g2", LiveInfo(), 0, 100)
    assert "done: OK" in done and "left" not in done and "CPU      -" in done


def test_live_table_non_tty_prints_at_most_once_per_interval():
    out = []
    table = LiveTable(out.append, tty=False, interval=30.0)
    table.update(["a", "b"], now=100.0)
    table.update(["a", "b"], now=110.0)                      # too early
    assert sum("node status:" in line for line in out) == 1 and "  a" in out and "  b" in out
    table.update(["a", "b"], now=131.0)
    assert sum("node status:" in line for line in out) == 2
    table.update(["a", "b"], now=132.0, force=True)          # forced print
    assert sum("node status:" in line for line in out) == 3


def test_live_table_tty_redraws_in_place(capsys):
    table = LiveTable(print, tty=True)
    table.update(["a", "b"], now=1.0)
    table.update(["a", "c"], now=4.0)
    text = capsys.readouterr().out
    assert "\x1b[2A" in text and "\x1b[2K" in text          # the second redraw jumps 2 lines up
    assert text.count("\n") == 4


# ---------------- subprocesses ---------------------------------------------------------------------------

def _opts(**kw):
    base = dict(log_dir=None, interval=5.0, remaining_every=3)
    base.update(kw)
    return series.SeriesOptions(**base)


def test_child_args_classic():
    cfg = StressConfig(node="w1", duration=300, cpu_load=100, ram_pct=50, hdd=True, max_temp=82,
                       cooldown=45, notes="test")
    a = child_args(cfg, _opts(allow_no_sensor=True, hw_privileged=True, skip_hw=True), "/l/w1.log", 2)
    assert a[:4] == ["--node", "w1", "--yes", "--non-interactive"] and "--no-background" in a
    pairs = dict(zip(a[::1], a[1::1]))
    assert pairs["--log-file"] == "/l/w1.log" and pairs["--time"] == "300"
    assert pairs["--max-temp"] == "82" and pairs["--cooldown"] == "45" and pairs["--concurrent"] == "2"
    assert pairs["--profile"] == "classic" and pairs["--cpu-load"] == "100" and pairs["--ram-pct"] == "50"
    assert pairs["--notes"] == "test" and pairs["--interval"] == "5.0"
    assert "--hdd" in a and "--allow-no-sensor" in a and "--hw-privileged" in a and "--no-hw" in a


def test_child_args_stepped_has_no_time_ram_or_disk():
    cfg = StressConfig(node="w1", profile="stepped", steps=(25, 50, 100), step_time=120, duration=360)
    a = child_args(cfg, _opts(), "/l/w1.log", 3)
    pairs = dict(zip(a, a[1:]))
    assert pairs["--profile"] == "stepped" and pairs["--steps"] == "25,50,100"
    assert pairs["--step-time"] == "120"
    assert "--time" not in a and "--ram-pct" not in a and "--hdd" not in a and "--cpu-load" not in a


def test_child_args_are_accepted_by_the_real_parser():
    from stress_test.cli import build_parser
    cfg = StressConfig(node="w1", duration=300, notes="")
    ns = build_parser().parse_args(child_args(cfg, _opts(), "/l/w1.log", 2))
    assert ns.node == "w1" and ns.log_file == "/l/w1.log" and ns.concurrent == 2
    assert ns.yes and ns.non_interactive and ns.background is False and ns.notes == ""
    st = StressConfig(node="w2", profile="stepped", duration=720)
    ns2 = build_parser().parse_args(child_args(st, _opts(), "/l/w2.log", 2))
    assert ns2.profile == "stepped" and ns2.steps == (25, 50, 75, 100) and ns2.step_time == 180


# ---------------- node result from a log -----------------------------------------------------------------

def test_outcome_from_classic_log(tmp_path):
    f = tmp_path / "w1.log"
    f.write_text(make_log([70, 72, 74, 80, 82], [66, 60, 55, 52], node="w1"), encoding="utf-8")
    cfg = StressConfig(node="w1", duration=300)
    o = parallel.outcome_from_log(node("w1"), cfg, str(f), 0)
    assert o.status == "OK" and o.stats.temp_max == 82 and o.log_path == str(f)
    assert [(m.name, m.ops_per_s) for m in o.metrics] == [("cpu", 2646.7), ("matrix", 11155.6)]


def test_outcome_from_stepped_log_uses_last_stage_ops(tmp_path):
    text = make_stepped_log([47, 58, 70, 84], node="w1").replace(
        "TEST SUMMARY", "TEST SUMMARY\n  1/4   25 % → 26 %  max 47 °C | clock 3300 MHz | 162.5 bogo ops/s\n"
        "  4/4  100 % → 99 %  max 84 °C | clock 3591 MHz | 650.0 bogo ops/s")
    f = tmp_path / "w1.log"
    f.write_text(text, encoding="utf-8")
    cfg = StressConfig(node="w1", profile="stepped", duration=720)
    o = outcome_from_log(node("w1"), cfg, str(f), 0)
    assert [m.ops_per_s for m in o.metrics] == [650.0]
    assert [s.target for s in o.stats.stages] == [25, 50, 75, 100]


def test_outcome_from_missing_log_keeps_the_exit_code(tmp_path):
    o = outcome_from_log(node("w1"), StressConfig(node="w1"), str(tmp_path / "nothing.log"), 1)
    assert o.status == "ERROR" and o.stats is None and "could not read the log" in o.reason


# ---------------- plan and estimate for concurrency --------------------------------------------------------------

def test_estimate_total_parallel_is_longest_worker_plus_master():
    template = StressConfig(node="*", duration=300, cooldown=60, log=True)
    nodes = [node("w1"), node("w2"), node("m1", master=True)]
    each = 90 + 300 + 60
    assert series.estimate_total(template, nodes, parallel=False) == 3 * each
    assert series.estimate_total(template, nodes, parallel=True) == 2 * each      # at once + master
    assert series.estimate_total(template, [node("w1"), node("w2")], parallel=True) == each


def test_plan_lines_parallel_mentions_power_and_concurrency():
    template = StressConfig(node="*", duration=300, cooldown=60, log=True)
    text = "\n".join(series.plan_lines(template, [node("w1"), node("w2"), node("m1", master=True)],
                                      {"w1": NodeWorkload((), 2)}, parallel=True))
    assert "2 workers AT ONCE, master alone afterwards" in text and "power draw from the socket" in text
    assert "workers at once)" in text and "Estimate in total: about 15 min" in text          # 450 + 450 = 900 s
    seq = "\n".join(series.plan_lines(template, [node("w1"), node("w2")], parallel=False))
    assert "one after another" in seq and "AT ONCE" not in seq and "socket" not in seq
    single = "\n".join(series.plan_lines(template, [node("w1")], parallel=True))
    assert "Test of 1 node." in single and "AT ONCE" not in single                        # one worker
