"""Reading logs and comparing two tests."""
import pytest

from stress_test.compare import (CompareError, compare_files, compare_runs,
                                 latest_logs_for_node, resolve_logs)
from stress_test.logparse import parse_log, read_log


def make_log(temps_test, temps_cool, node="dell-9020-sff-i7", started="2026-09-20 20:10:48",
             duration="5 min (300 s)", baseline=41, ops=(2646.7, 11155.6), freq=3591,
             clock=(20, 15, 0), step=5, notes="", summary=True, old_format=False):
    """Text of a result log in the English format (or an old one, test phase only)."""
    lines = ["=== KUBERNETES STRESS-NG LOG ===", f"Node: {node}",
             f"Started: {started}", f"Test duration: {duration}",
             "CPU load: 100%  RAM: no  disk: no", f"Notes: {notes}",
             "=== METRICS LOG (TIME | CPU/RAM | TEMPERATURES | CLOCK) ==="]
    t = clock[0] * 3600 + clock[1] * 60 + clock[2]

    def stamp(sec):
        sec %= 86400
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    for temp in temps_test:
        load = "CPU: 2000m (100%), RAM: 4913Mi (67%)" if old_format else "CPU: 100%, RAM: 4821 MiB (21%)"
        warn = " ⚠️  OVERHEATING!" if temp >= 80 else ""
        lines.append(f"[{stamp(t)}] {load} | Temp: CPU: {temp}°C, GPU: 51°C{warn} | Clock: {freq} MHz")
        if len(lines) % 3 == 0:
            lines.append(f"[{stamp(t)}] ⏱️   Remaining 38 s (elapsed 4 min 22 s of 5 min, 87 %)")
        t += step
    if temps_cool:
        lines.append(f"[{stamp(t)}] Cooldown: measuring for another 60 s")
    for temp in temps_cool:
        t += step
        lines.append(f"[cooldown] [{stamp(t)}] CPU: 3%, RAM: 4800 MiB (20%) | "
                     f"Temp: CPU: {temp}°C, GPU: 52°C | Clock: 3200 MHz")
    if summary:
        lines += ["====================================================", "TEST SUMMARY",
                  f"CPU temp (test):          min 52 | avg 75 | max {max(temps_test)} °C   "
                  f"(idle before test {baseline} °C)",
                  f"stress-ng performance:    cpu {ops[0]} | matrix {ops[1]} bogo ops/s"]
    return "\n".join(lines) + "\n"


HOT = [78, 80, 82, 83, 82, 83, 84, 83, 82, 82, 83, 84, 82]
HOT_COOL = [62, 61, 60, 60, 58, 58, 57, 56, 56, 56, 55, 55]
COOL_RUN = [66, 68, 70, 71, 72, 72, 73, 72, 72, 73, 74, 72, 72]
COOL_COOL = [52, 51, 50, 49, 48, 48, 47, 47, 46, 46, 45, 45]


# ---------------- reading a log ---------------------------------------------------------------

def test_parse_log_reads_header_samples_baseline_and_metrics():
    run = parse_log(make_log(HOT, HOT_COOL, notes="before cleaning"), "a.log")
    assert run.node == "dell-9020-sff-i7" and run.started == "2026-09-20 20:10:48"
    assert run.duration_text == "5 min (300 s)" and run.notes == "before cleaning"
    assert run.baseline_temp == 41
    assert [(m.name, m.ops_per_s) for m in run.metrics] == [("cpu", 2646.7), ("matrix", 11155.6)]
    assert len(run.samples) == 25 and run.has_cooldown
    test = [s for s in run.samples if s.phase == "test"]
    cool = [s for s in run.samples if s.phase == "cooldown"]
    assert len(test) == 13 and len(cool) == 12
    assert test[0].t == 0 and test[1].t == 5 and test[0].cpu_pct == 100.0
    assert test[0].freq_mhz == 3591 and test[0].cpu_temp == 78 and test[0].mem_used_mib == 4821
    assert cool[0].cpu_temp == 62 and cool[0].cpu_pct == 3.0


def test_parse_log_ignores_remaining_time_and_other_lines():
    run = parse_log(make_log(HOT, HOT_COOL))
    assert all(s.cpu_temp is not None for s in run.samples)               # ⏱️ lines are not counted


def test_parse_log_old_format_without_cooldown_and_summary():
    text = make_log([70, 71, 72, 73], [], old_format=True, summary=False)
    run = parse_log(text)
    assert len(run.samples) == 4 and not run.has_cooldown
    assert run.samples[0].cpu_pct == 100.0 and run.samples[0].mem_used_mib == 4913
    assert run.baseline_temp is None and run.metrics == []


def test_parse_log_handles_midnight_rollover():
    run = parse_log(make_log([50, 51, 52, 53], [], clock=(23, 59, 50), summary=False))
    assert [s.t for s in run.samples] == [0, 5, 10, 15]


def test_parse_log_rejects_foreign_text():
    with pytest.raises(ValueError, match="is not a result log"):
        parse_log("some other file\n")


def test_read_log_from_file(tmp_path):
    f = tmp_path / "a.log"
    f.write_text(make_log(HOT, HOT_COOL), encoding="utf-8")
    assert read_log(f).node == "dell-9020-sff-i7"


# ---------------- comparison -------------------------------------------------------------------

def _runs(**kw_b):
    a = parse_log(make_log(HOT, HOT_COOL), "a.log")
    kw = dict(temps_test=COOL_RUN, temps_cool=COOL_COOL, baseline=38,
              started="2026-09-21 18:00:00")
    kw.update(kw_b)
    b = parse_log(make_log(**kw), "b.log")
    return a, b


def test_compare_marks_improvements():
    a, b = _runs()
    text = "\n".join(compare_runs(a, b, warn_temp=80))
    assert "TEST COMPARISON" in text and "A: a.log" in text and "B: b.log" in text
    max_line = next(l for l in text.splitlines() if l.startswith("CPU temp max:"))
    assert "84 °C" in max_line and "74 °C" in max_line and "-10 °C (better)" in max_line
    above = next(l for l in text.splitlines() if l.startswith("Time above 80 °C:"))
    assert "(better)" in above and above.rstrip().endswith("(better)")
    assert "Drop after load off:" in text and "Slow cooldown:" in text
    assert "Total:" in text and "× worse" in text
    assert "0× worse" in text                                             # nothing got worse


def test_compare_marks_worse_when_hotter_and_slower():
    a, b = _runs(temps_test=[86] * 13, temps_cool=HOT_COOL, ops=(2000.0, 9000.0))
    text = "\n".join(compare_runs(a, b, warn_temp=80))
    hot = next(l for l in text.splitlines() if l.startswith("CPU temp max:"))
    assert "(worse)" in hot
    cpu = next(l for l in text.splitlines() if l.startswith("Performance cpu (bogo ops/s):"))
    assert "2646.7" in cpu and "2000.0" in cpu and "-24.4 %" in cpu and "(worse)" in cpu


def test_compare_small_changes_are_not_rated():
    a, b = _runs(temps_test=[t - 1 for t in HOT], temps_cool=HOT_COOL, baseline=41,
                 ops=(2650.0, 11160.0))
    text = "\n".join(compare_runs(a, b, warn_temp=80))
    hot = next(l for l in text.splitlines() if l.startswith("CPU temp max:"))
    assert "-1 °C" in hot and "(better)" not in hot and "(worse)" not in hot
    assert "Total: no significant change." in text


def test_compare_warns_about_incomparable_runs():
    a, b = _runs(node="hp-g2-celeron", baseline=30, temps_cool=[], ops=(600.0, 1400.0))
    text = "\n".join(compare_runs(a, b, warn_temp=80))
    assert "Watch out when comparing:" in text
    assert "different nodes" in text and "The idle temperature differs" in text
    assert "lacks cooldown measurements" in text
    assert "no data" in text                                              # cooldown B


def test_compare_warns_about_different_stressors_and_missing_baseline():
    a = parse_log(make_log(HOT, HOT_COOL), "a.log")
    old = parse_log(make_log(HOT, [], old_format=True, summary=False), "b.log")
    text = "\n".join(compare_runs(a, old, warn_temp=80))
    assert "lacks the idle temperature" in text and "lacks cooldown measurements" in text


def test_compare_files_reports_broken_file(tmp_path):
    good = tmp_path / "a.log"
    good.write_text(make_log(HOT, HOT_COOL), encoding="utf-8")
    bad = tmp_path / "b.log"
    bad.write_text("nothing\n", encoding="utf-8")
    with pytest.raises(CompareError, match="is not a result log"):
        compare_files(good, bad)
    with pytest.raises(CompareError):
        compare_files(good, tmp_path / "missing.log")


# ---------------- finding logs -------------------------------------------------------------------

def _touch(dirpath, *names):
    for n in names:
        (dirpath / n).write_text(make_log(HOT, HOT_COOL), encoding="utf-8")


def test_latest_logs_for_node_sorted_by_timestamp(tmp_path):
    _touch(tmp_path, "dell-9020-sff-i7-300s-2026-09-20_20-10-48.log",
           "dell-9020-sff-i7-60s-2026-09-19_10-00-00.log",
           "dell-9020-sff-i7-300s-2026-09-21_18-00-00.log",
           "hp-g2-celeron-120s-2026-09-22_10-00-00.log",
           "dell-9020-sff-i7-extra-300s-2026-09-23_10-00-00.log")
    found = latest_logs_for_node("dell-9020-sff-i7", tmp_path)
    assert [f.name for f in found] == ["dell-9020-sff-i7-300s-2026-09-20_20-10-48.log",
                                       "dell-9020-sff-i7-300s-2026-09-21_18-00-00.log"]


def test_resolve_logs_by_node_by_names_and_errors(tmp_path):
    _touch(tmp_path, "n-60s-2026-09-20_10-00-00.log", "n-60s-2026-09-21_10-00-00.log")
    a, b = resolve_logs(["n"], tmp_path)
    assert a.name.endswith("09-20_10-00-00.log") and b.name.endswith("09-21_10-00-00.log")
    a, b = resolve_logs(["n-60s-2026-09-21_10-00-00", "n-60s-2026-09-20_10-00-00.log"], tmp_path)
    assert a.name.endswith("09-21_10-00-00.log")                          # the order stays as given
    with pytest.raises(CompareError, match="only 0 log"):
        resolve_logs(["other-node"], tmp_path)
    with pytest.raises(CompareError, match="not found"):
        resolve_logs(["n", "nothing.log"], tmp_path)
    with pytest.raises(CompareError, match="Give two logs"):
        resolve_logs(["a", "b", "c"], tmp_path)
    with pytest.raises(CompareError, match="Give two logs"):
        resolve_logs([str(tmp_path / "n-60s-2026-09-20_10-00-00.log")], tmp_path)


# ---------------- real log of the dell from version 1.4.0 --------------------------------------------------

REAL_DELL_LOG = """=== KUBERNETES STRESS-NG LOG ===
Node: dell-9020-sff-i7
Started: 2026-09-20 20:10:48
Test duration: 5 min (300 s)
Notes: test
=== METRICS LOG (TIME | CPU/RAM | TEMPERATURES | CLOCK) ===
[20:15:06] CPU: 100%, RAM: 4821 MiB (21%) | Temp: CPU: 82°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3591 MHz
[20:15:11] CPU: 100%, RAM: 4823 MiB (21%) | Temp: CPU: 82°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3591 MHz
[20:15:17] CPU: 100%, RAM: 4822 MiB (21%) | Temp: CPU: 83°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3591 MHz
[20:15:17] ⏱️   Remaining 53 s (elapsed 4 min 7 s of 5 min, 82 %)
[20:15:43] CPU: 100%, RAM: 4820 MiB (21%) | Temp: CPU: 82°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3583 MHz
[20:16:04] CPU: 100%, RAM: 4821 MiB (21%) | Temp: CPU: 84°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3591 MHz
[20:16:09] CPU: 100%, RAM: 4824 MiB (21%) | Temp: CPU: 82°C, GPU: 51°C ⚠️  OVERHEATING! | Clock: 3591 MHz
[20:16:09] ✅ Test completed. Actual run time: 5 min 1 s.
[20:16:09] Cooldown: measuring for another 60 s
[cooldown] [20:16:15] CPU: 5%, RAM: 4797 MiB (20%) | Temp: CPU: 62°C, GPU: 51°C | Clock: 3207 MHz
[cooldown] [20:16:20] CPU: 3%, RAM: 4805 MiB (21%) | Temp: CPU: 61°C, GPU: 51°C | Clock: 3246 MHz
[cooldown] [20:16:56] CPU: 3%, RAM: 4800 MiB (20%) | Temp: CPU: 56°C, GPU: 52°C | Clock: 3192 MHz
[cooldown] [20:17:11] CPU: 14%, RAM: 4804 MiB (21%) | Temp: CPU: 55°C, GPU: 52°C | Clock: 3617 MHz
====================================================
TEST SUMMARY
====================================================
Samples:                  58 during the test, 12 during cooldown
CPU temp (test):          min 52 | avg 75 | max 84 °C   (idle before test 41 °C)
stress-ng performance:    cpu 2646.7 | matrix 11155.6 bogo ops/s
Cooldown (61 s):          82 → 55 °C (-27 °C, 26.5 °C/min)
"""


def test_real_dell_log_from_1_4_0_is_readable_and_gives_both_cooling_metrics():
    from stress_test.summary import cooldown_phases
    run = parse_log(REAL_DELL_LOG, "dell.log")
    assert run.node == "dell-9020-sff-i7" and run.baseline_temp == 41
    assert [(m.name, m.ops_per_s) for m in run.metrics] == [("cpu", 2646.7), ("matrix", 11155.6)]
    test = [s for s in run.samples if s.phase == "test"]
    cool = [s for s in run.samples if s.phase == "cooldown"]
    assert (len(test), len(cool)) == (6, 4)
    p = cooldown_phases(test, cool)
    assert (p.load_temp, p.jump_temp, p.end_temp) == (82, 62, 55)
    assert p.jump_s == 6 and p.slow_s == 56
    assert round(p.slow_rate, 1) == 7.5                                   # 7 °C in 56 s


# ---------------- stepped test in the log and in the comparison --------------------------------------------------

def make_stepped_log(stage_temps, targets=(25, 50, 75, 100), per_stage=4, node="dell",
                     started="2026-09-20 20:10:48", baseline=40, cool=(60, 58, 56, 55, 54, 53)):
    lines = ["=== KUBERNETES STRESS-NG LOG ===", f"Node: {node}", f"Started: {started}",
             "Test duration: 12 min (720 s)",
             "Profile: stepped " + "/".join(str(t) for t in targets) + " % for 3 min",
             "CPU load: gradually 25/50/75/100 %", "Notes: ",
             "=== METRICS LOG (TIME | CPU/RAM | TEMPERATURES | CLOCK) ==="]
    t = 20 * 3600
    for i, (target, temp) in enumerate(zip(targets, stage_temps), 1):
        lines.append(f"[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}] ▶ Stage {i}/{len(targets)}: "
                     f"{target} % (3 min)")
        for _ in range(per_stage):
            lines.append(f"[{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}] CPU: {target + 1}%, "
                         f"RAM: 4800 MiB (20%) | Temp: CPU: {temp}°C | Clock: 3591 MHz")
            t += 5
    for temp in cool:
        t += 5
        lines.append(f"[cooldown] [{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}] CPU: 3%, "
                     f"RAM: 4800 MiB (20%) | Temp: CPU: {temp}°C | Clock: 3200 MHz")
    lines += ["====", "TEST SUMMARY",
              f"CPU temp (test):          min 47 | avg 60 | max {max(stage_temps)} °C   "
              f"(idle before test {baseline} °C)"]
    return "\n".join(lines) + "\n"


def test_parse_stepped_log_reads_profile_stages_and_stage_of_each_sample():
    run = parse_log(make_stepped_log([47, 58, 70, 84]), "s.log")
    assert run.profile == "stepped" and run.stage_targets == [25, 50, 75, 100]
    test = [s for s in run.samples if s.phase == "test"]
    assert len(test) == 16 and [s.stage for s in test] == [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4
    assert all(s.stage == 0 for s in run.samples if s.phase == "cooldown")
    assert run.has_cooldown and run.baseline_temp == 40


def test_classic_log_has_classic_profile():
    run = parse_log(make_log(HOT, HOT_COOL))
    assert run.profile == "classic" and run.stage_targets == []
    assert all(s.stage == 0 for s in run.samples)


def test_compare_two_stepped_logs_shows_per_stage_rows_and_marks():
    a = parse_log(make_stepped_log([47, 58, 70, 84]), "a.log")
    b = parse_log(make_stepped_log([45, 52, 60, 70], started="2026-09-21 18:00:00"), "b.log")
    text = "\n".join(compare_runs(a, b, warn_temp=80))
    assert "stepped 25/50/75/100 %" in text
    row4 = next(l for l in text.splitlines() if l.startswith("Stage 4/4 (100 %) max:"))
    assert "84 °C" in row4 and "70 °C" in row4 and "-14 °C (better)" in row4
    row1 = next(l for l in text.splitlines() if l.startswith("Stage 1/4 (25 %) max:"))
    assert "-2 °C (better)" in row1
    assert "Watch out when comparing" not in text or "different profile" not in text


def test_compare_warns_when_profiles_or_steps_differ():
    stepped = parse_log(make_stepped_log([47, 58, 70, 84]), "s.log")
    classic = parse_log(make_log(HOT, HOT_COOL), "c.log")
    text = "\n".join(compare_runs(classic, stepped, warn_temp=80))
    assert "different profile (classic and stepped)" in text and "classic" in text and "stepped" in text
    other = parse_log(make_stepped_log([47, 58, 70], targets=(25, 50, 70)), "o.log")
    text2 = "\n".join(compare_runs(stepped, other, warn_temp=80))
    assert "different stages" in text2


# ---------------- node concurrency in the log ---------------------------------------------------------------------

def test_concurrent_line_is_parsed_and_compare_warns():
    solo = parse_log(make_log(HOT, HOT_COOL), "a.log")
    text = make_log(HOT, HOT_COOL).replace("Notes:", "Concurrency: yes (2 nodes at once)\nNotes:")
    par = parse_log(text, "b.log")
    assert solo.concurrent == 1 and par.concurrent == 2
    out = "\n".join(compare_runs(solo, par, warn_temp=80))
    assert "Parallel nodes:" in out and "yes (2 at once)" in out
    assert "different node parallelism (1 and 2)" in out
    same = "\n".join(compare_runs(par, par, warn_temp=80))
    assert "different node parallelism" not in same


def test_stage_rows_in_summary_give_ops_per_stage():
    text = make_stepped_log([47, 58, 70, 84]).replace(
        "TEST SUMMARY", "TEST SUMMARY\n  3/4   75 % → 76 %  max 70 °C | clock 3591 MHz | 487.5 bogo ops/s")
    assert parse_log(text).stage_ops == {3: 487.5}
