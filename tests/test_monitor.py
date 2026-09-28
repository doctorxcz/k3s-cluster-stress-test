from stress_test.models import ProbeData
from stress_test.monitor import (Monitor, OverheatGuard, format_reading,
                                 format_remaining)
from stress_test.parsing import cpu_percent, parse_probe_output


# ---------------- overheating guard ------------------------------------------------------

def test_guard_needs_consecutive_hot_readings():
    g = OverheatGuard(85, consecutive=2)
    assert g.update(90) is False        # the first hot reading
    assert g.update(70) is False        # it cooled down -> the counter resets
    assert g.update(86) is False
    assert g.update(85) is True         # two in a row (the limit is inclusive)


def test_guard_without_sensor_never_trips():
    g = OverheatGuard(85)
    for _ in range(10):
        assert g.update(None) is False


# ---------------- CPU % and RAM from the probe ----------------------------------------------------

def test_cpu_percent_from_counter_delta():
    assert cpu_percent((1000, 900), (2000, 1000)) == 90.0     # 1000 ticks, of which 100 idle
    assert cpu_percent((1000, 900), (2000, 1900)) == 0.0      # all idle
    assert cpu_percent((1000, 0), (2000, 0)) == 100.0


def test_cpu_percent_handles_missing_and_bad_data():
    assert cpu_percent(None, (1, 1)) is None
    assert cpu_percent((1, 1), None) is None
    assert cpu_percent((1000, 500), (1000, 500)) is None      # no difference
    assert cpu_percent((2000, 500), (1000, 500)) is None      # the counter went backwards (restart)


def test_probe_parses_cpu_stat_and_memory():
    data = parse_probe_output(
        "temp coretemp 40000\nfreq 3000\ncpu_stat 123456 100000\n"
        "mem_total_kb 8388608\nmem_available_kb 6291456\n")
    assert data.cpu_stat == (123456, 100000)
    assert data.mem_total_mib == 8192 and data.mem_available_mib == 6144
    assert data.mem_used_mib == 2048 and data.mem_used_pct == 25.0
    assert ProbeData().cpu_stat is None and ProbeData().mem_used_mib is None


# ---------------- line format ------------------------------------------------------------

def _probe(**kw):
    base = dict(temps={"CPU": 40, "NVMe": 45}, freq_mhz=3100,
                mem_total_mib=8000, mem_available_mib=6000)
    base.update(kw)
    return ProbeData(**base)


def test_format_reading_normal():
    line = format_reading(71.4, _probe(), now="12:00:00")
    assert line == ("[12:00:00] CPU: 71%, RAM: 2000 MiB (25%) | "
                    "Temp: CPU: 40°C, NVMe: 45°C | Clock: 3100 MHz")


def test_format_reading_warning_and_missing_data():
    line = format_reading(None, ProbeData({"CPU": 82}, None), now="12:00:00")
    assert "OVERHEATING" in line and "CPU: ?, RAM: ?" in line and "? MHz" in line
    assert "none" in format_reading(None, ProbeData(), now="x")


# ---------------- remaining time ---------------------------------------------------------------

def test_format_remaining_middle():
    assert format_remaining(80, 300, now="12:00:00") == (
        "[12:00:00] ⏱️  Remaining 3 min 40 s (elapsed 1 min 20 s of 5 min, 27 %)")


def test_format_remaining_hours():
    line = format_remaining(600, 5400, now="x")
    assert "Remaining 1 h 20 min" in line and "11 %" in line


def test_format_remaining_finished():
    line = format_remaining(305, 300, now="x")
    assert "Test time is up" in line and "Remaining" not in line


# ---------------- Monitor thread (without a cluster) -------------------------------------------------

class _ScriptedKube:
    """The probe returns the given outputs one by one (the last one repeats)."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def exec(self, pod, script):
        i = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[i]


def _out(total, idle, temp=40, freq=3000):
    return (f"temp coretemp {temp * 1000}\nfreq {freq}\ncpu_stat {total} {idle}\n"
            f"mem_total_kb 8192000\nmem_available_kb 6144000\n")


def _monitor(kube, clock, every=3, total=300, initial=None, guard_limit=85):
    lines, info, aborted = [], [], []
    mon = Monitor(kube, "n", OverheatGuard(guard_limit), emit=lines.append,
                  on_abort=lambda: aborted.append(1), total_seconds=total,
                  remaining_every=every, emit_info=info.append, clock=clock,
                  initial=initial)
    return mon, lines, info, aborted


def test_monitor_computes_cpu_percent_between_ticks_and_stores_samples():
    t = {"now": 0.0}
    kube = _ScriptedKube([_out(2000, 1100), _out(3000, 1100)])   # 1st tick 80 %, 2nd tick 100 %
    initial = ProbeData(cpu_total=1000, cpu_idle=900)            # counters before the test
    mon, lines, _, _ = _monitor(kube, lambda: t["now"], initial=initial)
    t["now"] = 5.0
    mon._tick()
    t["now"] = 10.0
    mon._tick()
    assert [round(s.cpu_pct) for s in mon.samples] == [80, 100]   # 80 % a 100 %
    assert mon.samples[0].t == 5.0 and mon.samples[1].phase == "test"
    assert mon.samples[1].mem_used_mib == 2000
    assert "CPU: 100%" in lines[1] and "RAM: 2000 MiB (25%)" in lines[1]


def test_monitor_first_tick_without_baseline_has_no_cpu_percent():
    kube = _ScriptedKube([_out(2000, 1800)])
    mon, lines, _, _ = _monitor(kube, lambda: 0.0)
    mon._tick()
    assert mon.samples[0].cpu_pct is None and "CPU: ?" in lines[0]


def test_monitor_prints_remaining_every_third_reading_in_test_phase_only():
    t = {"now": 0.0}
    mon, lines, info, _ = _monitor(_ScriptedKube([_out(1000, 500)]), lambda: t["now"])
    for i in range(1, 7):
        t["now"] = i * 5.0
        mon._tick()
    assert len(lines) == 6 and len(info) == 2
    assert "Remaining 4 min 45 s" in info[0] and "Remaining 4 min 30 s" in info[1]
    mon.phase = "cooldown"                    # after the test the time to the end is not printed
    for _ in range(6):
        mon._tick()
    assert len(info) == 2


def test_monitor_remaining_disabled_with_zero():
    mon, lines, info, _ = _monitor(_ScriptedKube([_out(1000, 500)]), lambda: 0.0, every=0)
    for _ in range(6):
        mon._tick()
    assert len(lines) == 6 and info == []


def test_monitor_phase_is_recorded_per_sample():
    mon, _, _, _ = _monitor(_ScriptedKube([_out(1000, 500)]), lambda: 0.0)
    mon._tick()
    mon.phase = "cooldown"
    mon._tick()
    assert [s.phase for s in mon.samples] == ["test", "cooldown"]


def test_monitor_overheat_aborts_once_and_keeps_measuring():
    kube = _ScriptedKube([_out(1000, 500, temp=92)])
    mon, _, _, aborted = _monitor(kube, lambda: 0.0)
    mon._tick()
    assert not mon.aborted                     # one hot reading is not enough
    mon._tick()
    assert mon.aborted and mon.abort_temp == 92 and aborted == [1]
    mon._tick()
    assert aborted == [1]                      # the second time it does not delete the load anymore
    assert len(mon.samples) == 3               # measuring continues (because of the cooldown)


def test_monitor_guard_is_off_in_cooldown():
    kube = _ScriptedKube([_out(1000, 500, temp=95)])
    mon, _, _, aborted = _monitor(kube, lambda: 0.0)
    mon.phase = "cooldown"
    for _ in range(4):
        mon._tick()
    assert not mon.aborted and aborted == []   # after the test overheating is not handled


def test_monitor_survives_probe_failure():
    from stress_test.kube import KubectlError

    class Broken:
        def exec(self, pod, script):
            raise KubectlError("boom")
    mon, lines, _, _ = _monitor(Broken(), lambda: 0.0)
    mon._tick()
    assert "CPU: ?, RAM: ?" in lines[0] and mon.samples[0].cpu_temp is None


def test_monitor_tags_sample_with_stage_at_tick_start_not_after_the_probe():
    """The stage marker can arrive while the probe is running: the reading still belongs to the old stage."""
    holder = {}

    class Racing:
        def exec(self, pod, script):
            holder["mon"].stage = 2                     # the marker of stage 2 arrives in the middle of the reading
            return _out(1000, 500)

    mon, _, _, _ = _monitor(Racing(), lambda: 0.0)
    holder["mon"] = mon
    mon.stage = 1
    mon._tick()
    assert mon.samples[0].stage == 1
    mon._tick()
    assert mon.samples[1].stage == 2                     # the next reading is already in stage 2


def test_monitor_stage_is_zero_outside_the_test_phase():
    mon, _, _, _ = _monitor(_ScriptedKube([_out(1000, 500)]), lambda: 0.0)
    mon.stage = 3
    mon.phase = "cooldown"
    mon._tick()
    assert mon.samples[0].stage == 0 and mon.samples[0].phase == "cooldown"
