"""Reading result logs (logs/*.log) back into data, for comparing two tests.

A log is a text file. Measurements are reassembled from lines
`[HH:MM:SS] CPU: ..., RAM: ... | Temp: ... | Clock: ... MHz` (cooldown phase
lines have the prefix `[cooldown] `). This works for logs written by this English build
(with cooldown and summary); logs without cooldown data are read too, just
without cooldown and idle temperature data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import Sample
from .summary import StressMetric

HEADER = "=== KUBERNETES STRESS-NG LOG ==="

_LINE_RE = re.compile(r"^(?P<cool>\[cooldown\]\s+)?\[(?P<h>\d\d):(?P<m>\d\d):(?P<s>\d\d)\]\s+"
                      r"CPU:\s*(?P<load>.*?)\|\s*Temp:\s*(?P<temps>.*?)\|\s*Clock:\s*(?P<freq>\S+)\s*MHz")
_CPU_PCT_RE = re.compile(r"(?:^|\s)(\d+)%|\((\d+)%\)")
_RAM_RE = re.compile(r"RAM:\s*(\d+)\s*Mi(?:B)?\s*\((\d+)%\)")
_CPU_TEMP_RE = re.compile(r"CPU:\s*(\d+)\s*°C")
_STAGE_RE = re.compile(r"▶ Stage (\d+)/(\d+):\s*(\d+)\s*%")
_CONCURRENT_RE = re.compile(r"^Concurrency:\s*yes\s*\((\d+)")
_STAGE_ROW_RE = re.compile(r"^(\d+)/(\d+)\s+\d+\s*%\s*→.*?([\d.]+)\s*bogo ops/s\s*$")
_BASELINE_RE = re.compile(r"idle before test\s+(\d+)\s*°C")
_METRICS_RE = re.compile(r"^stress-ng performance:\s+(.+?)\s+bogo ops/s")


@dataclass
class RunData:
    """Everything that can be learned from the log about one test."""

    path: str = ""
    node: str = "?"
    started: str = ""
    duration_text: str = ""
    notes: str = ""
    concurrent: int = 1                          # how many nodes were tested at once (the Concurrency: line)
    stage_ops: dict[int, float] = field(default_factory=dict)   # bogo ops/s of cpu per stage
    profile: str = "classic"                     # "classic" or "stepped" (the Profile: line)
    stage_targets: list[int] = field(default_factory=list)   # stage targets in %, in order
    baseline_temp: Optional[int] = None          # idle before the test (from the summary in the log)
    metrics: list[StressMetric] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)

    @property
    def has_cooldown(self) -> bool:
        return any(s.phase == "cooldown" for s in self.samples)


def _parse_sample_line(line: str) -> Optional[tuple[int, Sample]]:
    """(seconds since midnight, Sample with t=0) from a measurement line, otherwise None."""
    match = _LINE_RE.match(line.strip())
    if not match:
        return None
    clock = int(match["h"]) * 3600 + int(match["m"]) * 60 + int(match["s"])
    load = match["load"]
    cpu_pct = None
    pct = _CPU_PCT_RE.search(load.split(", RAM:")[0])
    if pct:
        cpu_pct = float(pct.group(1) or pct.group(2))
    ram = _RAM_RE.search("RAM:" + load.split("RAM:", 1)[1]) if "RAM:" in load else None
    temp = _CPU_TEMP_RE.search(match["temps"])
    freq = int(match["freq"]) if match["freq"].isdigit() else None
    return clock, Sample(
        t=0.0, phase="cooldown" if match["cool"] else "test",
        cpu_temp=int(temp.group(1)) if temp else None, freq_mhz=freq, cpu_pct=cpu_pct,
        mem_used_mib=int(ram.group(1)) if ram else None,
        mem_used_pct=float(ram.group(2)) if ram else None)


def _parse_metrics(text: str) -> list[StressMetric]:
    """'cpu 2646.7 | matrix 11155.6' -> metrics (name and bogo ops/s only)."""
    metrics = []
    for part in text.split("|"):
        pieces = part.split()
        if len(pieces) == 2:
            try:
                metrics.append(StressMetric(pieces[0], 0, 0.0, float(pieces[1])))
            except ValueError:
                continue
    return metrics


def parse_log(text: str, path: str = "") -> RunData:
    """Assembles the test data from the log text. ValueError if it is not a log of this tool."""
    lines = text.splitlines()
    if not any(line.strip() == HEADER for line in lines[:5]):
        raise ValueError("This is not a result log of this tool (header missing).")
    run = RunData(path=path)
    raw: list[tuple[int, Sample, int]] = []
    current_stage = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Node:") and run.node == "?":
            run.node = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Started:") and not run.started:
            run.started = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Test duration:") and not run.duration_text:
            run.duration_text = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Notes:") and not run.notes:
            run.notes = stripped.split(":", 1)[1].strip()
        elif _CONCURRENT_RE.match(stripped):
            run.concurrent = int(_CONCURRENT_RE.match(stripped).group(1))
        elif _STAGE_ROW_RE.match(stripped):
            row = _STAGE_ROW_RE.match(stripped)
            run.stage_ops[int(row.group(1))] = float(row.group(3))
        elif stripped.startswith("Profile:"):
            run.profile = "stepped" if "stepped" in stripped.lower() else "classic"
        stage = _STAGE_RE.search(stripped)
        if stage:
            current_stage = int(stage.group(1))
            while len(run.stage_targets) < current_stage:
                run.stage_targets.append(0)
            run.stage_targets[current_stage - 1] = int(stage.group(3))
            continue
        parsed = _parse_sample_line(stripped)
        if parsed:
            clock, sample = parsed
            raw.append((clock, sample, current_stage if sample.phase == "test" else 0))
            continue
        base = _BASELINE_RE.search(stripped)
        if base and run.baseline_temp is None:
            run.baseline_temp = int(base.group(1))
        metrics = _METRICS_RE.match(stripped)
        if metrics and not run.metrics:
            run.metrics = _parse_metrics(metrics.group(1))
    if raw:
        day, previous, first = 0, raw[0][0], raw[0][0]
        for clock, sample, stage_no in raw:
            if clock < previous - 43200:          # midnight rollover
                day += 86400
            previous = clock
            offset = (clock + day) - first
            run.samples.append(Sample(
                t=float(offset), phase=sample.phase, cpu_temp=sample.cpu_temp,
                freq_mhz=sample.freq_mhz, cpu_pct=sample.cpu_pct,
                mem_used_mib=sample.mem_used_mib, mem_used_pct=sample.mem_used_pct,
                stage=stage_no))
    return run


def read_log(path) -> RunData:
    """Reads and parses a log file (ValueError, OSError on problems)."""
    file = Path(path)
    return parse_log(file.read_text(encoding="utf-8", errors="replace"), str(file))
