"""Comparison of two tests (--compare): side-by-side table, changes and warnings."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from .logparse import RunData, read_log
from .models import WARN_TEMP
from .parsing import format_duration
from .summary import RECOVERY_MARGIN, RunStats, run_stats

TEMP_DELTA = 2          # °C: a smaller change is not rated as better/worse
ABOVE_DELTA = 10        # s
OPS_DELTA_PCT = 2.0     # %
RECOVERY_DELTA = 5      # s

_COL_LABEL, _COL_A, _COL_B = 28, 28, 28
WIDTH = 98
_LOG_STAMP_RE = re.compile(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.log$")


class CompareError(ValueError):
    """The logs could not be found or read."""


# --- finding logs --------------------------------------------------------------------

def _find_log(value: str, log_dir: Path) -> Path:
    for candidate in (Path(value).expanduser(), log_dir / value, log_dir / f"{value}.log"):
        if candidate.is_file():
            return candidate
    raise CompareError(f"Log '{value}' not found (nor in the folder {log_dir}).")


def latest_logs_for_node(node: str, log_dir: Path, count: int = 2) -> list[Path]:
    """The newest logs of a node (older first), by the timestamp in the name."""
    pattern = re.compile(rf"^{re.escape(node)}-\d+s-(\d{{4}}-\d\d-\d\d_\d\d-\d\d-\d\d)\.log$")
    found = []
    if log_dir.is_dir():
        for file in log_dir.iterdir():
            match = pattern.match(file.name)
            if match:
                found.append((match.group(1), file))
    found.sort()
    return [file for _, file in found[-count:]]


def resolve_logs(values: Sequence[str], log_dir: Path) -> tuple[Path, Path]:
    """Two logs from the arguments: either two files/names, or one node name (the two newest)."""
    if len(values) == 2:
        return _find_log(values[0], log_dir), _find_log(values[1], log_dir)
    if len(values) == 1:
        if Path(values[0]).expanduser().is_file():
            raise CompareError("Give two logs (older and newer), or just a node name.")
        logs = latest_logs_for_node(values[0], log_dir)
        if len(logs) < 2:
            raise CompareError(f"For node '{values[0]}' the folder {log_dir} has only "
                               f"{len(logs)} log(s), two are needed for a comparison.")
        return logs[0], logs[1]
    raise CompareError("Give two logs (older and newer), or just a node name.")


# --- formatting ------------------------------------------------------------------------

def _temp(value: Optional[float]) -> str:
    return f"{value:.0f} °C" if value is not None else "?"


def _delta(a: Optional[float], b: Optional[float], unit: str = "°C", digits: int = 0) -> str:
    if a is None or b is None:
        return ""
    diff = round(b - a, digits)
    return f"0 {unit}" if diff == 0 else f"{diff:+.{digits}f} {unit}"


def _line(label: str, a: str, b: str, change: str = "") -> str:
    text = label + ":" if label else ""
    left = text + " " * max(_COL_LABEL - len(text), 1)
    return (left + a.ljust(_COL_A) + b.ljust(_COL_B) + change).rstrip()


def _verdict(diff: Optional[float], threshold: float, lower_is_better: bool) -> str:
    """'(better)' / '(worse)' / '' according to the change of B against A."""
    if diff is None or abs(diff) < threshold:
        return ""
    better = diff < 0 if lower_is_better else diff > 0
    return " (better)" if better else " (worse)"


def _phases_text(stats: RunStats) -> tuple[str, str]:
    p = stats.phases
    if p is None:
        return "no data", "no data"
    jump = f"{p.load_temp} → {p.jump_temp} °C in {p.jump_s:.0f} s ({-p.jump_drop:+d})"
    slow = (f"{p.slow_rate:.1f} °C/min ({-p.slow_drop:+d} in {p.slow_s:.0f} s)"
            if p.slow_rate is not None else "short measurement")
    return jump, slow


def _recovery_text(stats: RunStats) -> str:
    if stats.recovery_reached is None:
        return "no data"
    if stats.recovery_reached:
        return f"after {stats.recovery_s:.0f} s"
    return f"not reached in {stats.recovery_s:.0f} s"


def _ops(run: RunData) -> dict[str, float]:
    return {m.name: m.ops_per_s for m in run.metrics}


def _stats(run: RunData, warn_temp: int) -> RunStats:
    targets = run.stage_targets if run.profile == "stepped" and run.stage_targets else None
    return run_stats(run.samples, run.baseline_temp, warn_temp, stage_targets=targets)


def _concurrent_text(run: RunData) -> str:
    return f"yes ({run.concurrent} at once)" if run.concurrent > 1 else "no"


def _profile_text(run: RunData) -> str:
    if run.profile == "stepped" and run.stage_targets:
        return "stepped " + "/".join(str(t) for t in run.stage_targets) + " %"
    return "stepped" if run.profile == "stepped" else "classic"


# --- main function --------------------------------------------------------------------------

def compare_runs(a: RunData, b: RunData, warn_temp: int = WARN_TEMP) -> list[str]:
    """Comparison lines of two tests (A = older, B = newer)."""
    sa, sb = _stats(a, warn_temp), _stats(b, warn_temp)
    better = worse = 0
    out = ["=" * WIDTH, "TEST COMPARISON", "=" * WIDTH,
           f"A: {a.path or '?'}", f"B: {b.path or '?'}", "",
           _line("", "A (older)", "B (newer)", "change B vs A"),
           "-" * WIDTH]

    def add(label, ta, tb, change="", verdict=""):
        nonlocal better, worse
        out.append(_line(label, ta, tb, change + verdict))
        better += verdict == " (better)"
        worse += verdict == " (worse)"

    add("Node", a.node, b.node)
    add("Started", a.started or "?", b.started or "?")
    add("Test duration", a.duration_text or "?", b.duration_text or "?")
    add("Profile", _profile_text(a), _profile_text(b))
    add("Parallel nodes", _concurrent_text(a), _concurrent_text(b))
    if a.notes or b.notes:
        add("Note", a.notes or "-", b.notes or "-")
    add("Idle before test", _temp(sa.baseline_temp), _temp(sb.baseline_temp),
        _delta(sa.baseline_temp, sb.baseline_temp))
    out.append("")

    dmax = None if sa.temp_max is None or sb.temp_max is None else sb.temp_max - sa.temp_max
    add("CPU temp max", _temp(sa.temp_max), _temp(sb.temp_max),
        _delta(sa.temp_max, sb.temp_max), _verdict(dmax, TEMP_DELTA, True))
    davg = None if sa.temp_avg is None or sb.temp_avg is None else sb.temp_avg - sa.temp_avg
    add("CPU temp avg", _temp(sa.temp_avg), _temp(sb.temp_avg),
        _delta(sa.temp_avg, sb.temp_avg), _verdict(davg, TEMP_DELTA, True))
    for x, y in zip(sa.stages, sb.stages):        # stepped test: temperature per stage
        if x.target == y.target and x.temp_max is not None and y.temp_max is not None:
            add(f"Stage {x.index}/{len(sa.stages)} ({x.target} %) max",
                _temp(x.temp_max), _temp(y.temp_max), _delta(x.temp_max, y.temp_max),
                _verdict(y.temp_max - x.temp_max, TEMP_DELTA, True))
    dabove = sb.above_s - sa.above_s
    add(f"Time above {warn_temp} °C", format_duration(round(sa.above_s)),
        format_duration(round(sb.above_s)),
        f"{'-' if dabove < 0 else '+'}{format_duration(round(abs(dabove)))}" if dabove else "0 s",
        _verdict(dabove, ABOVE_DELTA, True))
    fa = "?" if sa.freq_avg is None else f"{sa.freq_avg:.0f} MHz (min {sa.freq_min})"
    fb = "?" if sb.freq_avg is None else f"{sb.freq_avg:.0f} MHz (min {sb.freq_min})"
    add("CPU clock avg", fa, fb, _delta(sa.freq_avg, sb.freq_avg, "MHz"))
    thr = {True: "SUSPECTED", False: "no"}
    add("Throttling", thr[sa.throttling], thr[sb.throttling], "",
        " (better)" if sa.throttling and not sb.throttling
        else " (worse)" if sb.throttling and not sa.throttling else "")
    add("CPU load avg", "?" if sa.cpu_avg is None else f"{sa.cpu_avg:.0f} %",
        "?" if sb.cpu_avg is None else f"{sb.cpu_avg:.0f} %",
        _delta(sa.cpu_avg, sb.cpu_avg, "%"))

    opa, opb = _ops(a), _ops(b)
    for name in list(dict.fromkeys([*opa, *opb])):
        va, vb = opa.get(name), opb.get(name)
        pct = None if not va or vb is None else (vb - va) / va * 100
        add(f"Performance {name} (bogo ops/s)", "-" if va is None else f"{va:.1f}",
            "-" if vb is None else f"{vb:.1f}",
            "" if pct is None else f"{pct:+.1f} %", _verdict(pct, OPS_DELTA_PCT, False))
    out.append("")

    ja, slow_a = _phases_text(sa)
    jb, slow_b = _phases_text(sb)
    pa, pb = sa.phases, sb.phases
    add("Drop after load off", ja, jb,
        "" if not (pa and pb) else _delta(-pa.jump_drop, -pb.jump_drop))
    add("Slow cooldown", slow_a, slow_b,
        "" if not (pa and pb and pa.slow_rate is not None and pb.slow_rate is not None)
        else _delta(pa.slow_rate, pb.slow_rate, "°C/min", 1))
    verdict = ""
    change = ""
    if sa.recovery_reached is not None and sb.recovery_reached is not None:
        if sa.recovery_reached and sb.recovery_reached:
            diff = sb.recovery_s - sa.recovery_s
            change = f"{diff:+.0f} s"
            verdict = _verdict(diff, RECOVERY_DELTA, True)
        elif sb.recovery_reached and not sa.recovery_reached:
            verdict = " (better)"
        elif sa.recovery_reached and not sb.recovery_reached:
            verdict = " (worse)"
    add(f"Return to idle (+{RECOVERY_MARGIN} °C)", _recovery_text(sa), _recovery_text(sb),
        change, verdict)
    out.append("-" * WIDTH)

    if better or worse:
        out.append(f"Total: {better}× better, {worse}× worse (the rest without a significant change).")
    else:
        out.append("Total: no significant change.")

    warnings = _warnings(a, b, sa, sb)
    if warnings:
        out.append("")
        out.append("Watch out when comparing:")
        out.extend(f"  - {w}" for w in warnings)
    out.append("")
    out.append("How to read: 'Drop after load off' is the fast drop right after the load ends (the difference between "
               "chip and heatsink),")
    out.append("'Slow cooldown' is the rest of the whole heatsink cooling down. Changes smaller than "
               f"{TEMP_DELTA} °C, {OPS_DELTA_PCT:.0f} % of performance or {ABOVE_DELTA} s are not rated.")
    out.append("=" * WIDTH)
    return out


def _warnings(a: RunData, b: RunData, sa: RunStats, sb: RunStats) -> list[str]:
    notes = []
    if a.concurrent != b.concurrent:
        notes.append(f"The tests ran with a different node parallelism ({a.concurrent} and {b.concurrent}): "
                     f"the load of the other machines changes the ambient temperature and power draw.")
    if a.profile != b.profile:
        notes.append("The tests have a different profile (classic and stepped), performance and temperatures are not "
                     "directly comparable.")
    elif a.profile == "stepped" and a.stage_targets != b.stage_targets:
        notes.append("The stepped tests have different stages, only the same ones are compared.")
    if a.node != b.node:
        notes.append(f"The tests are from different nodes ({a.node} and {b.node}), the comparison is only indicative.")
    if not sa.n_test or not sb.n_test:
        notes.append("There are no measurements in one of the logs.")
    if sa.baseline_temp is not None and sb.baseline_temp is not None \
            and abs(sa.baseline_temp - sb.baseline_temp) >= 5:
        notes.append(f"The idle temperature differs ({sa.baseline_temp} and {sb.baseline_temp} °C): "
                     f"different ambient temperature or residual heat.")
    if sa.n_test and sb.n_test and abs(sa.n_test - sb.n_test) > 0.2 * max(sa.n_test, sb.n_test):
        notes.append("The tests lasted for different lengths of time, the maximum and average temperature are not exactly comparable.")
    if set(_ops(a)) != set(_ops(b)) and (a.metrics or b.metrics):
        notes.append("The tests had a different load (different stressors), performance is not directly comparable.")
    if not a.has_cooldown or not b.has_cooldown:
        notes.append("One log lacks cooldown measurements (older version or --cooldown 0).")
    if sa.baseline_temp is None or sb.baseline_temp is None:
        notes.append("One log lacks the idle temperature before the test.")
    return notes


def compare_files(a_path, b_path, warn_temp: int = WARN_TEMP) -> list[str]:
    """Loads two logs and returns the comparison lines (CompareError on problems)."""
    runs = []
    for path in (a_path, b_path):
        try:
            runs.append(read_log(path))
        except (OSError, ValueError) as exc:
            raise CompareError(f"{path}: {exc}") from exc
    return compare_runs(runs[0], runs[1], warn_temp)
