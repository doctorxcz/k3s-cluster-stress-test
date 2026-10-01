"""Summary at the end of the test (pure functions: they get measured data and return lines of text)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .models import WARN_TEMP, ProbeData, Sample
from .parsing import format_duration

THROTTLE_DROP_PCT = 10.0     # clock drop under load from which throttling is suspected
FREQ_SPREAD_PCT = 15.0       # clock fluctuation worth mentioning
LOADED_CPU_PCT = 80.0        # from what % utilisation a measurement counts as "under load"
RECOVERY_MARGIN = 5          # °C above the idle temperature = "back to normal"
MIN_SLOW_S = 20.0            # the shortest measured cooldown from which °C/min is computed

_METRIC_RE = re.compile(
    r"stress-ng: metrc: \[\d+\]\s+([A-Za-z][\w-]*)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+"
    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$")


@dataclass(frozen=True)
class StressMetric:
    """One row of the stress-ng results table (--metrics-brief)."""

    name: str
    bogo_ops: int
    real_time: float
    ops_per_s: float          # bogo ops/s by real time


def parse_stressng_metrics(lines: Sequence[str]) -> list[StressMetric]:
    """Extracts the performance table (cpu, matrix, vm, ...) from the stress-ng output."""
    metrics = []
    for line in lines:
        match = _METRIC_RE.search(line.strip())
        if match:
            metrics.append(StressMetric(
                name=match.group(1), bogo_ops=int(match.group(2)),
                real_time=float(match.group(3)), ops_per_s=float(match.group(6))))
    return metrics


LABEL_WIDTH = 26


def row(label: str, value: str) -> str:
    """A summary row 'Label:  value' with aligned values."""
    text = label + ":"
    return text + " " * max(LABEL_WIDTH - len(text), 1) + value


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def seconds_above(samples: Sequence[Sample], limit: int) -> float:
    """How many seconds the CPU temperature was at the limit or above (by measurement times)."""
    total = 0.0
    for i, s in enumerate(samples):
        if s.cpu_temp is None or s.cpu_temp < limit:
            continue
        if i + 1 < len(samples):
            total += samples[i + 1].t - s.t
        elif i > 0:
            total += s.t - samples[i - 1].t          # last measurement: the same step as before
    return total


THERMAL_MIN_TEMP = 65     # °C: a clock drop while the CPU stays below this is not judged as thermal throttling


def detect_throttling(test: Sequence[Sample],
                      min_cpu_pct: float = LOADED_CPU_PCT) -> tuple[bool, str]:
    """Assesses whether the CPU clock under load did not drop. Returns (suspicion, text).

    Compares the average clock in the first and last third of the measurements under full load.
    It is an estimate from measured values, not proof.
    """
    loaded = [s for s in test if s.freq_mhz is not None
              and (s.cpu_pct is None or s.cpu_pct >= min_cpu_pct)]
    if len(loaded) < 6:
        return False, "too few measurements under load to judge"
    third = len(loaded) // 3
    first = _mean([s.freq_mhz for s in loaded[:third]])
    last = _mean([s.freq_mhz for s in loaded[-third:]])
    drop = (first - last) / first * 100 if first else 0.0
    freqs = [s.freq_mhz for s in loaded]
    spread = (max(freqs) - min(freqs)) / max(freqs) * 100 if max(freqs) else 0.0
    last_temp = next((s.cpu_temp for s in reversed(loaded) if s.cpu_temp is not None), None)
    temp_txt = f" at a temperature of {last_temp} °C" if last_temp is not None else ""
    temps = [s.cpu_temp for s in loaded if s.cpu_temp is not None]
    if drop >= THROTTLE_DROP_PCT and temps and max(temps) < THERMAL_MIN_TEMP:
        # a lower clock on a cool CPU is not thermal throttling: power saving, the governor, or the turbo budget
        return False, (f"clock dropped by {drop:.0f} % ({first:.0f} → {last:.0f} MHz), but the CPU stayed cool "
                       f"(max {max(temps)} °C) - not thermal: power saving / governor / turbo budget")
    if drop >= THROTTLE_DROP_PCT:
        return True, (f"SUSPECTED THROTTLING: clock dropped by {drop:.0f} % "
                      f"({first:.0f} → {last:.0f} MHz){temp_txt}")
    if spread >= FREQ_SPREAD_PCT:
        return False, (f"clock fluctuates ({min(freqs)}–{max(freqs)} MHz, {spread:.0f} %), "
                       f"but without a lasting drop ({drop:.1f} %)")
    return False, f"no signs of throttling (clock stable, change {drop:.1f} %)"


@dataclass(frozen=True)
class StageStats:
    """Results of one stage of the stepped test."""

    index: int
    target: int                       # target load in %
    n: int                            # number of measurements in the stage
    cpu_avg: Optional[float]          # actually measured utilisation
    temp_max: Optional[int]
    temp_avg: Optional[float]
    freq_avg: Optional[float]
    throttling: bool
    throttling_text: str
    ops: Optional[float] = None       # bogo ops/s of the cpu stressor in this stage


def stage_stats(test: Sequence[Sample], targets: Sequence[int],
                stage_metrics: Optional[dict[int, Sequence[StressMetric]]] = None
                ) -> list[StageStats]:
    """Indicators per stage (measurements are assigned by Sample.stage)."""
    result = []
    for index, target in enumerate(targets, 1):
        ss = [s for s in test if s.stage == index]
        temps = [s.cpu_temp for s in ss if s.cpu_temp is not None]
        freqs = [s.freq_mhz for s in ss if s.freq_mhz is not None]
        loads = [s.cpu_pct for s in ss if s.cpu_pct is not None]
        suspected, text = detect_throttling(ss, min_cpu_pct=target * 0.7)
        ops = None
        if stage_metrics:
            ops = next((m.ops_per_s for m in stage_metrics.get(index, ()) if m.name == "cpu"), None)
        result.append(StageStats(
            index=index, target=target, n=len(ss),
            cpu_avg=_mean(loads) if loads else None,
            temp_max=max(temps) if temps else None,
            temp_avg=_mean(temps) if temps else None,
            freq_avg=_mean(freqs) if freqs else None,
            throttling=suspected, throttling_text=text, ops=ops))
    return result


def _throttling(test: Sequence[Sample], targets: Optional[Sequence[int]],
                stage_metrics=None) -> tuple[bool, str, list[StageStats]]:
    """Throttling of the whole test; in the stepped test every stage is judged separately."""
    if not targets:
        suspected, text = detect_throttling(test) if test else (False, "no measurements")
        return suspected, text, []
    stages = stage_stats(test, targets, stage_metrics)
    bad = next((s for s in stages if s.throttling), None)
    if bad is not None:
        return True, f"in stage {bad.index}/{len(stages)} ({bad.target} %): {bad.throttling_text}", stages
    return False, "no signs of throttling in any stage", stages


@dataclass(frozen=True)
class CooldownPhases:
    """Two phases of cooling after the load stops.

    1) Drop: a fast fall right after stopping (from the last measurement under load to the first
       measurement after it). It reflects the temperature difference between chip and heatsink.
    2) Slow cooldown: the rest of the cooling (from the first to the last measurement after the load),
       when the whole heatsink with the fan cools down.
    """

    load_temp: int          # the last measurement under load
    jump_temp: int          # the first measurement after the load ended
    jump_s: float           # after how many seconds from the last measurement under load
    end_temp: int           # the last cooldown measurement
    slow_s: float           # length of the slow cooldown

    @property
    def jump_drop(self) -> int:
        return self.load_temp - self.jump_temp

    @property
    def slow_drop(self) -> int:
        return self.jump_temp - self.end_temp

    @property
    def slow_rate(self) -> Optional[float]:
        """°C/min of the slow cooldown (only when the measured time is at least MIN_SLOW_S)."""
        if self.slow_s < MIN_SLOW_S:
            return None
        return self.slow_drop / (self.slow_s / 60)


def cooldown_phases(test: Sequence[Sample], cool: Sequence[Sample]) -> Optional[CooldownPhases]:
    """Splits the cooldown into the drop and the slow cooldown; None when data is missing."""
    load = next((s for s in reversed(test) if s.cpu_temp is not None), None)
    temps = [s for s in cool if s.cpu_temp is not None]
    if load is None or len(temps) < 2:
        return None
    first, last = temps[0], temps[-1]
    if last.t <= first.t or first.t < load.t:
        return None
    return CooldownPhases(load_temp=load.cpu_temp, jump_temp=first.cpu_temp,
                          jump_s=first.t - load.t, end_temp=last.cpu_temp,
                          slow_s=last.t - first.t)


def recovery_time(test: Sequence[Sample], cool: Sequence[Sample],
                  base_temp: Optional[int]) -> tuple[Optional[bool], float]:
    """(reached?, seconds) of the return to idle temperature + RECOVERY_MARGIN.

    Reached is None when it cannot be judged (idle or cooldown missing); for
    an unreached return the seconds are the length of the measured cooldown.
    """
    if base_temp is None or not cool or not test:
        return None, 0.0
    span = cool[-1].t - test[-1].t
    target = base_temp + RECOVERY_MARGIN
    reached = next((s for s in cool if s.cpu_temp is not None and s.cpu_temp <= target), None)
    if reached is not None:
        return True, max(reached.t - test[-1].t, 0.0)
    return False, span


@dataclass(frozen=True)
class RunStats:
    """Computed indicators of one test (summary and comparison of two tests)."""

    n_test: int
    n_cool: int
    temp_min: Optional[int]
    temp_avg: Optional[float]
    temp_max: Optional[int]
    above_s: float
    freq_avg: Optional[float]
    freq_min: Optional[int]
    freq_max: Optional[int]
    throttling: bool
    throttling_text: str
    cpu_avg: Optional[float]
    phases: Optional[CooldownPhases]
    baseline_temp: Optional[int]
    recovery_reached: Optional[bool]
    recovery_s: float
    cool_span: float
    stages: tuple[StageStats, ...] = ()
    power_avg: Optional[float] = None       # W, CPU package (RAPL), test phase
    power_max: Optional[float] = None
    ping_n: int = 0                          # --net-watch: pings sent during the test
    ping_lost: int = 0
    ping_avg: Optional[float] = None         # ms
    ping_max: Optional[float] = None


def run_stats(samples: Sequence[Sample], base_temp: Optional[int],
              warn_temp: int = WARN_TEMP, stage_targets: Optional[Sequence[int]] = None,
              stage_metrics=None) -> RunStats:
    """Test indicators from measurements (the same numbers as in the summary at the end of the test)."""
    test = [s for s in samples if s.phase == "test"]
    cool = [s for s in samples if s.phase == "cooldown"]
    temps = [s.cpu_temp for s in test if s.cpu_temp is not None]
    freqs = [s.freq_mhz for s in test if s.freq_mhz is not None]
    loads = [s.cpu_pct for s in test if s.cpu_pct is not None]
    watts = [s.power_w for s in test if s.power_w is not None]
    pings = [s.ping_ms for s in test if s.ping_ms is not None]
    lost = sum(1 for s in test if s.ping_lost)
    suspected, text, stages = _throttling(test, stage_targets, stage_metrics)
    reached, recovery = recovery_time(test, cool, base_temp)
    return RunStats(
        n_test=len(test), n_cool=len(cool),
        temp_min=min(temps) if temps else None,
        temp_avg=_mean(temps) if temps else None,
        temp_max=max(temps) if temps else None,
        above_s=seconds_above(test, warn_temp),
        freq_avg=_mean(freqs) if freqs else None,
        freq_min=min(freqs) if freqs else None,
        freq_max=max(freqs) if freqs else None,
        throttling=suspected, throttling_text=text,
        cpu_avg=_mean(loads) if loads else None,
        phases=cooldown_phases(test, cool), baseline_temp=base_temp,
        recovery_reached=reached, recovery_s=recovery,
        cool_span=(cool[-1].t - test[-1].t) if (cool and test) else 0.0,
        stages=tuple(stages),
        power_avg=_mean(watts) if watts else None, power_max=max(watts) if watts else None,
        ping_n=len(pings) + lost, ping_lost=lost,
        ping_avg=_mean(pings) if pings else None, ping_max=max(pings) if pings else None)


def _cooldown_lines(test: Sequence[Sample], cool: Sequence[Sample],
                    baseline: Optional[ProbeData]) -> tuple[list[str], list[str]]:
    """(summary lines, notes) for the cooldown phase."""
    lines, notes = [], []
    end_temp = next((s.cpu_temp for s in reversed(cool) if s.cpu_temp is not None), None)
    start_temp = next((s.cpu_temp for s in reversed(test) if s.cpu_temp is not None), None)
    span = cool[-1].t - test[-1].t
    if start_temp is not None and end_temp is not None and span > 0:
        drop = start_temp - end_temp
        rate = drop / (span / 60)
        lines.append(row(f"Cooldown ({span:.0f} s)",
                         f"{start_temp} → {end_temp} °C ({-drop:+d} °C, {rate:.1f} °C/min)"))
    phases = cooldown_phases(test, cool)
    if phases is not None:
        lines.append(row("Drop after load stops",
                         f"{phases.load_temp} → {phases.jump_temp} °C in {phases.jump_s:.0f} s "
                         f"({-phases.jump_drop:+d} °C)"))
        if phases.slow_rate is not None:
            lines.append(row("Slow cooldown",
                             f"{phases.jump_temp} → {phases.end_temp} °C in {phases.slow_s:.0f} s "
                             f"({-phases.slow_drop:+d} °C, {phases.slow_rate:.1f} °C/min)"))
        else:
            lines.append(row("Slow cooldown",
                             f"short measurement ({phases.slow_s:.0f} s), rate cannot be estimated"))
    freq = next((s.freq_mhz for s in reversed(cool) if s.freq_mhz is not None), None)
    if freq is not None:
        lines.append(row("Clock after test", f"{freq} MHz"))
    base_temp = baseline.cpu_temp if baseline else None
    if base_temp is not None and span > 0:
        target = base_temp + RECOVERY_MARGIN
        reached = next((s for s in cool if s.cpu_temp is not None and s.cpu_temp <= target), None)
        if reached is not None:
            lines.append(row(f"Return to idle (≤ {target} °C)",
                             f"after {max(reached.t - test[-1].t, 0):.0f} s "
                             f"(idle before test {base_temp} °C + {RECOVERY_MARGIN})"))
        else:
            lines.append(row(f"Return to idle (≤ {target} °C)",
                             f"not reached in {span:.0f} s (idle before test {base_temp} °C)"))
            notes.append(f"Within {span:.0f} s after the load the CPU did not return to the temperature before the test. "
                         f"A longer cooldown may mean weaker cooling (indicative, it also depends "
                         f"on the ambient temperature).")
    return lines, notes


def build_summary(samples: Sequence[Sample], baseline: Optional[ProbeData],
                  metrics: Sequence[StressMetric], warn_temp: int = WARN_TEMP,
                  cooldown_requested: int = 0,
                  stage_targets: Optional[Sequence[int]] = None,
                  stage_metrics: Optional[dict[int, Sequence[StressMetric]]] = None
                  ) -> list[str]:
    """Lines of the test summary (shown in the terminal and saved to the log)."""
    test = [s for s in samples if s.phase == "test"]
    cool = [s for s in samples if s.phase == "cooldown"]
    out = ["=" * 52, "TEST SUMMARY", "=" * 52]
    if not test:
        out.append("No measurement could be obtained during the test.")
        out.append("=" * 52)
        return out

    counts = f"{len(test)} during the test" + (f", {len(cool)} during cooldown" if cool else "")
    out.append(row("Samples", counts))
    notes: list[str] = []

    temps = [s.cpu_temp for s in test if s.cpu_temp is not None]
    if temps:
        base = (f"   (idle before test {baseline.cpu_temp} °C)"
                if baseline and baseline.cpu_temp is not None else "")
        out.append(row("CPU temp (test)",
                       f"min {min(temps)} | avg {_mean(temps):.0f} | "
                       f"max {max(temps)} °C{base}"))
        above = seconds_above(test, warn_temp)
        out.append(row(f"Time above {warn_temp} °C", format_duration(round(above))))
        if max(temps) >= warn_temp:
            notes.append(f"CPU reached {max(temps)} °C (warning from {warn_temp} °C): check "
                         f"the cooling (dust, thermal paste, fan).")
    else:
        out.append(row("CPU temp (test)", "unavailable (the sensor returned no values)"))

    freqs = [s.freq_mhz for s in test if s.freq_mhz is not None]
    if freqs:
        out.append(row("CPU clock (test)",
                       f"avg {_mean(freqs):.0f} | min {min(freqs)} | max {max(freqs)} MHz"))
        suspected, text, stages = _throttling(test, stage_targets, stage_metrics)
        out.append(row("Throttling", text))
        if suspected:
            notes.append("The clock dropped under load: the processor most likely protected itself against overheating "
                         "or limited power supply.")

    loads = [s.cpu_pct for s in test if s.cpu_pct is not None]
    if loads:
        out.append(row("CPU load (test)",
                       f"avg {_mean(loads):.0f} % | max {max(loads):.0f} %"))
    watts = [s.power_w for s in test if s.power_w is not None]
    if watts:
        limits = ""
        if baseline and baseline.pl1_w:
            limits = f"   (limit PL1 {baseline.pl1_w:.0f} W" + (
                f", PL2 {baseline.pl2_w:.0f} W)" if baseline.pl2_w else ")")
        out.append(row("CPU power (test)", f"avg {_mean(watts):.1f} | max {max(watts):.1f} W{limits}"))
        if baseline and baseline.pl1_w and _mean(watts) >= 0.95 * baseline.pl1_w and freqs:
            notes.append(f"The average power ({_mean(watts):.0f} W) is at the PL1 limit ({baseline.pl1_w:.0f} W): "
                         f"the clock is held down by the power limit (BIOS/firmware), not necessarily by heat.")
    pings = [s.ping_ms for s in test if s.ping_ms is not None]
    lost = sum(1 for s in test if s.ping_lost)
    if pings or lost:
        idle = f"   (idle before test {baseline.ping_ms:.2f} ms)" if baseline and baseline.ping_ms is not None else ""
        avg = f"avg {_mean(pings):.2f} | max {max(pings):.2f} ms, " if pings else ""
        out.append(row("Network latency (test)", f"{avg}lost {lost} of {len(pings) + lost}{idle}"))
        if lost:
            notes.append(f"{lost} of {len(pings) + lost} pings to the watched node got no reply while the node was "
                         f"loaded: the load disturbs the network (or the node cannot keep up).")
        elif pings and baseline and baseline.ping_ms and _mean(pings) > 3 * baseline.ping_ms and _mean(pings) > 1.0:
            notes.append(f"Latency to the watched node rose from {baseline.ping_ms:.2f} ms (idle) to "
                         f"{_mean(pings):.2f} ms (average under load).")
    rams = [s for s in test if s.mem_used_mib is not None]
    if rams:
        peak = max(rams, key=lambda s: s.mem_used_mib)
        pct = f" ({peak.mem_used_pct:.0f} %)" if peak.mem_used_pct is not None else ""
        out.append(row("RAM usage (test)", f"max {peak.mem_used_mib} MiB{pct}"))

    if stage_targets:
        stages = stage_stats(test, stage_targets, stage_metrics)
        out.append("Stages (target → measured):")
        for s in stages:
            measured = f"{s.cpu_avg:.0f} %" if s.cpu_avg is not None else "?"
            bits = []
            if s.temp_max is not None:
                bits.append(f"max {s.temp_max} °C | avg {s.temp_avg:.0f} °C")
            if s.freq_avg is not None:
                bits.append(f"clock {s.freq_avg:.0f} MHz")
            if s.ops is not None:
                bits.append(f"{s.ops:.1f} bogo ops/s")
            out.append(f"  {s.index}/{len(stages)}  {s.target:>3} % → {measured:<5} "
                       + (" | ".join(bits) if bits else "no measurements"))
    elif metrics:
        parts = " | ".join(f"{m.name} {m.ops_per_s:.1f}" for m in metrics)
        out.append(row("stress-ng performance", f"{parts} bogo ops/s"))

    if cool:
        lines, cool_notes = _cooldown_lines(test, cool, baseline)
        out.extend(lines)
        notes.extend(cool_notes)
    elif cooldown_requested > 0:
        out.append(row("Cooldown", "not measured (cooldown was skipped)"))

    if notes:
        out.append("Notes:")
        out.extend(f"  - {n}" for n in notes)
    out.append("=" * 52)
    return out
