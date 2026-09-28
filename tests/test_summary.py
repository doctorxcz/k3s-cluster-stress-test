"""Summary at the end of the test (pure functions, synthetic data)."""
from stress_test.models import ProbeData, Sample
from stress_test.summary import (build_summary, detect_throttling,
                                 parse_stressng_metrics, row, seconds_above)

METRIC_LINES = [
    "stress-ng: metrc: [541] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s",
    "stress-ng: metrc: [541]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)",
    "stress-ng: metrc: [541] cpu               79080    120.01    112.28      0.21       658.93         702.96",
    "stress-ng: metrc: [541] matrix           175024    120.00    111.74      0.15      1458.51        1564.25",
    "stress-ng: metrc: [541] vm                12112    120.41      0.54      1.99       100.59        4793.77",
    "stress-ng: info:  [541] passed: 5: cpu (2) matrix (2) vm (1)",
]


def S(t, temp=None, freq=None, cpu=100.0, phase="test", mem=4000, mem_pct=50.0):
    return Sample(t=t, phase=phase, cpu_temp=temp, freq_mhz=freq, cpu_pct=cpu,
                  mem_used_mib=mem, mem_used_pct=mem_pct)


def test_parse_stressng_metrics_from_real_output():
    metrics = parse_stressng_metrics(METRIC_LINES)
    assert [(m.name, m.bogo_ops, m.ops_per_s) for m in metrics] == [
        ("cpu", 79080, 658.93), ("matrix", 175024, 1458.51), ("vm", 12112, 100.59)]
    assert parse_stressng_metrics([]) == []
    assert parse_stressng_metrics(["stress-ng: running..."]) == []


def test_seconds_above_uses_sample_times():
    samples = [S(0, 70), S(5, 82), S(10, 84), S(15, 79), S(20, 81)]
    # 82 from t=5 to 10 (5 s) + 84 from 10 to 15 (5 s) + the last 81 = the same step (5 s)
    assert seconds_above(samples, 80) == 15
    assert seconds_above(samples, 90) == 0
    assert seconds_above([], 80) == 0


def test_throttling_detected_when_frequency_drops():
    test = [S(i * 5, 60 + i, 3000 - i * 150) for i in range(12)]      # 3000 -> 1350
    suspected, text = detect_throttling(test)
    assert suspected and "SUSPECTED THROTTLING" in text and "MHz" in text


def test_no_throttling_when_frequency_is_stable():
    test = [S(i * 5, 45 + (i % 3), 1600) for i in range(12)]
    suspected, text = detect_throttling(test)
    assert not suspected and "no signs of throttling" in text


def test_throttling_ignores_samples_without_load():
    idle = [S(i * 5, 40, 1600, cpu=5.0) for i in range(6)]           # ramp-up, no load
    load = [S(30 + i * 5, 60, 3300, cpu=100.0) for i in range(12)]
    suspected, _ = detect_throttling(idle + load)
    assert not suspected                                             # an increase is not a drop


def test_throttling_needs_enough_measurements():
    suspected, text = detect_throttling([S(0, 50, 3000), S(5, 60, 1000)])
    assert not suspected and "too few measurements" in text


def test_summary_full_with_cooldown_and_recovery():
    baseline = ProbeData({"CPU": 39}, 1600)
    test = [S(i * 5, 40 + i, 1600, cpu=100.0) for i in range(12)]         # 40 -> 51 °C
    cool = [S(60 + i * 5, 51 - i * 3, 1600, cpu=4.0, phase="cooldown") for i in range(1, 11)]
    lines = build_summary(test + cool, baseline, parse_stressng_metrics(METRIC_LINES),
                          warn_temp=80, cooldown_requested=60)
    text = "\n".join(lines)
    assert "TEST SUMMARY" in text
    assert row("Samples", "12 during the test, 10 during cooldown") in text
    assert "min 40 | avg 46 | max 51 °C" in text and "idle before test 39 °C" in text
    assert row("Time above 80 °C", "0 s") in text
    assert "avg 1600 | min 1600 | max 1600 MHz" in text
    assert "no signs of throttling" in text
    assert "cpu 658.9 | matrix 1458.5 | vm 100.6 bogo ops/s" in text
    assert "51 → 21 °C" in text and "-30 °C" in text                     # cooldown
    assert row("Clock after test", "1600 MHz") in text
    assert "Return to idle (≤ 44 °C): " in text and "after " in text
    assert "Notes:" not in text                                       # nothing to warn about


def test_summary_warns_when_hot_and_when_not_back_to_idle():
    baseline = ProbeData({"CPU": 40}, 3000)
    test = [S(i * 5, 78 + i, 3000) for i in range(8)]                     # up to 85 °C
    cool = [S(40 + i * 5, 84 - i, 3000, cpu=3.0, phase="cooldown") for i in range(1, 6)]
    text = "\n".join(build_summary(test + cool, baseline, [], warn_temp=80,
                                   cooldown_requested=60))
    assert "max 85 °C" in text and "check the cooling" in text
    assert "not reached" in text and "did not return to the temperature before the test" in text


def test_summary_marks_skipped_cooldown():
    test = [S(i * 5, 45, 1600) for i in range(5)]
    text = "\n".join(build_summary(test, ProbeData({"CPU": 40}, 1600), [], cooldown_requested=60))
    assert "not measured (cooldown was skipped)" in text
    text0 = "\n".join(build_summary(test, None, [], cooldown_requested=0))
    assert "Cooldown" not in text0


def test_summary_without_sensor_and_without_data():
    no_temp = [S(i * 5, None, None) for i in range(4)]
    text = "\n".join(build_summary(no_temp, None, []))
    assert row("CPU temp (test)", "unavailable") in text
    assert "No measurement could be obtained during the test." in "\n".join(build_summary([], None, []))


def test_row_aligns_values_and_keeps_a_space_for_long_labels():
    assert row("A", "x") == "A:" + " " * 24 + "x"
    long = row("Return to idle (≤ 44 °C) and even more", "value")
    assert long.endswith(": value")


# ---------------- two cooldown phases (drop and slow cooldown) ------------------------------

from stress_test.summary import cooldown_phases, recovery_time, run_stats


def _dell_like():
    """Shape of a real dell test: 82 °C under load, 62 °C after the stop, 55 °C after a minute."""
    test = [S(i * 5, 82 + (i % 2), 3591) for i in range(13)]             # last t = 60, 82 °C
    test[-1] = S(60, 82, 3591)
    cool_temps = [62, 61, 60, 60, 58, 58, 57, 56, 56, 56, 55, 55]
    cool = [S(66 + i * 5, t, 3200, cpu=3.0, phase="cooldown") for i, t in enumerate(cool_temps)]
    return test, cool


def test_cooldown_phases_jump_and_slow():
    test, cool = _dell_like()
    p = cooldown_phases(test, cool)
    assert (p.load_temp, p.jump_temp, p.end_temp) == (82, 62, 55)
    assert p.jump_s == 6 and p.jump_drop == 20
    assert p.slow_s == 55 and p.slow_drop == 7
    assert round(p.slow_rate, 1) == 7.6                                   # 7 °C in 55 s


def test_cooldown_phases_need_enough_data():
    test, cool = _dell_like()
    assert cooldown_phases(test, cool[:1]) is None                        # only one reading
    assert cooldown_phases([], cool) is None
    short = [S(66, 62, phase="cooldown"), S(71, 61, phase="cooldown")]     # 5 s of cooldown
    p = cooldown_phases(test, short)
    assert p is not None and p.slow_rate is None                          # too little for °C/min


def test_summary_shows_jump_and_slow_cooling_rows():
    test, cool = _dell_like()
    text = "\n".join(build_summary(test + cool, ProbeData({"CPU": 41}, 3300), [],
                                   warn_temp=80, cooldown_requested=60))
    assert row("Drop after load stops", "82 → 62 °C in 6 s (-20 °C)") in text
    assert row("Slow cooldown", "62 → 55 °C in 55 s (-7 °C, 7.6 °C/min)") in text


def test_summary_slow_cooling_short_measurement_has_no_rate():
    test, cool = _dell_like()
    text = "\n".join(build_summary(test + cool[:3], None, [], cooldown_requested=60))
    assert "Drop after load stops:" in text
    assert "short measurement (10 s), rate cannot be estimated" in text


def test_recovery_time_and_run_stats():
    test, cool = _dell_like()
    reached, secs = recovery_time(test, cool, 41)                          # target ≤ 46, not reached
    assert reached is False and secs == cool[-1].t - test[-1].t
    reached, secs = recovery_time(test, cool, 52)                          # target ≤ 57: 57 at the 8th reading
    assert reached is True and secs == 66 + 6 * 5 - 60
    assert recovery_time(test, cool, None) == (None, 0.0)
    stats = run_stats(test + cool, 41, warn_temp=80)
    assert stats.temp_max == 83 and stats.n_test == 13 and stats.n_cool == 12
    assert stats.phases.jump_drop == 20 and stats.recovery_reached is False
    assert stats.above_s > 0 and stats.throttling is False


# ---------------- summary per stage (stepped test) -------------------------------------------------

from stress_test.summary import stage_stats


def _stage_samples():
    """4 stages of 8 readings: temperature and clock grow with the load, the last stage has a clock drop."""
    samples, t = [], 0
    plan = [(1, 25, 47, 3300), (2, 50, 58, 3500), (3, 75, 70, 3591), (4, 100, 84, 3591)]
    for stage, pct, temp, freq in plan:
        for i in range(8):
            f = freq - (i * 60 if stage == 4 else 0)                    # 100 %: 3591 -> 3171
            samples.append(Sample(t=t, phase="test", cpu_temp=temp, freq_mhz=f,
                                  cpu_pct=pct + 1.0, stage=stage))
            t += 5
    return samples


def test_stage_stats_per_stage_values():
    from stress_test.summary import StressMetric
    metrics = {1: [StressMetric("cpu", 1, 1.0, 700.1)], 4: [StressMetric("cpu", 1, 1.0, 2646.7)]}
    stats = stage_stats(_stage_samples(), (25, 50, 75, 100), metrics)
    assert [(s.index, s.target, s.n, s.temp_max) for s in stats] == [
        (1, 25, 8, 47), (2, 50, 8, 58), (3, 75, 8, 70), (4, 100, 8, 84)]
    assert stats[0].cpu_avg == 26.0 and stats[0].ops == 700.1 and stats[1].ops is None
    assert [s.throttling for s in stats] == [False, False, False, True]  # only stage 4 drops


def test_summary_shows_stages_and_throttling_stage():
    text = "\n".join(build_summary(_stage_samples(), ProbeData({"CPU": 40}, 3300), [],
                                   warn_temp=80, cooldown_requested=0,
                                   stage_targets=(25, 50, 75, 100)))
    assert "Stages (target → measured):" in text
    assert "  1/4   25 % → 26 %" in text and "  4/4  100 % → 101 %" in text
    assert "max 47 °C" in text and "max 84 °C" in text
    assert "SUSPECTED THROTTLING" in text and "in stage 4/4 (100 %)" in text
    assert "stress-ng performance" not in text                              # ops are per stage


def test_summary_stages_without_throttling():
    samples = [Sample(t=i * 5, phase="test", cpu_temp=50, freq_mhz=3000, cpu_pct=26.0, stage=1)
               for i in range(8)]
    text = "\n".join(build_summary(samples, None, [], stage_targets=(25,)))
    assert "no signs of throttling in any stage" in text
