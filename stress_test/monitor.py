"""Measurement during the test (background thread) and the overheating guard."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .kube import Kubectl, KubectlError
from .manifests import PROBE_SCRIPT
from .models import PROBE_POD, WARN_TEMP, ProbeData, Sample
from .parsing import cpu_percent, format_duration, parse_probe_output

log = logging.getLogger(__name__)


class OverheatGuard:
    """Decides when to stop the test: the limit must be exceeded N readings in a row."""

    def __init__(self, limit: int, consecutive: int = 2) -> None:
        self.limit = limit
        self.consecutive = consecutive
        self._hot = 0

    @property
    def hot_count(self) -> int:
        return self._hot

    def update(self, cpu_temp: Optional[int]) -> bool:
        """Records a reading; returns True if it is time to stop the test."""
        if cpu_temp is None:          # without a sensor nothing can be judged
            return False
        if cpu_temp >= self.limit:
            self._hot += 1
        else:
            self._hot = 0
        return self._hot >= self.consecutive


def format_reading(cpu_pct: Optional[float], probe: ProbeData,
                   warn_temp: int = WARN_TEMP, now: Optional[str] = None) -> str:
    """One line of output/log: time | CPU, RAM | temperatures | clock.

    CPU % is computed from the difference of the /proc/stat counters between two readings, RAM from
    MemTotal and MemAvailable. Both are read by the probe directly from the node (no delay
    of the metrics server).
    """
    stamp = now or time.strftime("%H:%M:%S")
    cpu = f"{cpu_pct:.0f}%" if cpu_pct is not None else "?"
    if probe.mem_used_mib is not None:
        ram = f"{probe.mem_used_mib} MiB ({probe.mem_used_pct:.0f}%)"
    else:
        ram = "?"
    temps = ", ".join(f"{k}: {v}°C" for k, v in probe.temps.items()) or "none"
    if probe.cpu_temp is not None and probe.cpu_temp >= warn_temp:
        temps += " ⚠️ OVERHEATING!"
    freq = f"{probe.freq_mhz} MHz" if probe.freq_mhz is not None else "? MHz"
    return f"[{stamp}] CPU: {cpu}, RAM: {ram} | Temp: {temps} | Clock: {freq}"


def format_remaining(elapsed_s: float, total_s: int,
                     now: Optional[str] = None) -> str:
    """A progress line: how much is left, how much has elapsed and how many % are done."""
    stamp = now or time.strftime("%H:%M:%S")
    elapsed = max(int(elapsed_s), 0)
    remaining = max(total_s - elapsed, 0)
    percent = min(100, round(100 * elapsed / total_s)) if total_s > 0 else 100
    if remaining == 0:
        return (f"[{stamp}] ⏱️  Test time is up ({format_duration(total_s)}), "
                f"stress-ng is just finishing.")
    return (f"[{stamp}] ⏱️  Remaining {format_duration(remaining)} "
            f"(elapsed {format_duration(elapsed)} of {format_duration(total_s)}, "
            f"{percent} %)")


class Monitor(threading.Thread):
    """Every `interval` seconds reads the probe, prints a line and watches for overheating.

    All readings are stored in `samples` (for the summary at the end). `phase` says
    in which phase we are measuring: "test" (load running) or "cooldown" (after the load ends).
    The overheating guard and the remaining-time lines apply only in the "test" phase.
    """

    def __init__(self, kube: Kubectl, node: str, guard: OverheatGuard,
                 emit: Callable[[str], None], on_abort: Callable[[], None],
                 interval: float = 5.0, warn_temp: int = WARN_TEMP,
                 total_seconds: int = 0, remaining_every: int = 3,
                 probe_pod: str = PROBE_POD,
                 emit_info: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 initial: Optional[ProbeData] = None) -> None:
        super().__init__(daemon=True)
        self.kube = kube
        self.node = node
        self.guard = guard
        self.emit = emit
        self.on_abort = on_abort
        self.interval = interval
        self.warn_temp = warn_temp
        self.probe_pod = probe_pod
        self.total_seconds = total_seconds
        self.remaining_every = remaining_every   # 0 = do not print
        self.emit_info = emit_info or emit
        self._clock = clock
        self._t0 = clock()                       # start of measuring = start of the test
        self._ticks = 0
        self._prev_cpu = initial.cpu_stat if initial else None   # base for CPU %
        self.phase = "test"
        self.stage = 0                           # stage number of the stepped test (0 = none)
        self.guard_active = True
        self.samples: list[Sample] = []
        self.aborted = False
        self.abort_temp: Optional[int] = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self.interval)

    def final_sample(self) -> None:
        """One last reading (from the main thread, after the monitor thread has finished)."""
        if not self.is_alive():
            self._tick()

    def _tick(self) -> None:
        phase, stage = self.phase, self.stage     # state at the start of the reading (the probe takes a while)
        try:
            probe = parse_probe_output(self.kube.exec(self.probe_pod, PROBE_SCRIPT))
        except KubectlError as exc:
            log.warning("reading the probe failed: %s", exc)
            probe = ProbeData()
        current = probe.cpu_stat
        cpu_pct = cpu_percent(self._prev_cpu, current)
        if current is not None:
            self._prev_cpu = current
        self._ticks += 1
        self.samples.append(Sample(
            t=self._clock() - self._t0, phase=phase, cpu_temp=probe.cpu_temp,
            freq_mhz=probe.freq_mhz, cpu_pct=cpu_pct,
            mem_used_mib=probe.mem_used_mib, mem_used_pct=probe.mem_used_pct,
            stage=stage if phase == "test" else 0))
        self.emit(format_reading(cpu_pct, probe, self.warn_temp))
        if (phase == "test" and self.remaining_every > 0 and self.total_seconds > 0
                and self._ticks % self.remaining_every == 0):
            self.emit_info(format_remaining(self._clock() - self._t0,
                                            self.total_seconds))
        tripped = (phase == "test" and self.guard_active
                   and self.guard.update(probe.cpu_temp))
        log.debug("sample #%d (%s): cpu=%s%% ram=%s MiB temps=%s clock=%s | "
                  "guard: hot in a row=%d/%d", self._ticks, phase,
                  None if cpu_pct is None else round(cpu_pct, 1), probe.mem_used_mib,
                  probe.temps, probe.freq_mhz, self.guard.hot_count,
                  self.guard.consecutive)
        if tripped:
            log.error("OVERHEATING: CPU %s°C >= limit %s°C, deleting the stress pod",
                      probe.cpu_temp, self.guard.limit)
            self.aborted = True
            self.abort_temp = probe.cpu_temp
            self.guard_active = False       # once is enough; measuring continues (cooldown)
            self.on_abort()
