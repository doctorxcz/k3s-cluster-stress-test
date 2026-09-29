"""Baseline of a node: a saved "golden" result and the check of a new test against it (regression)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .compare import CompareError, _find_log, latest_logs_for_node
from .export import run_to_dict
from .logparse import RunData, read_log
from .parsing import is_node_name
from .paths import make_private_dir, open_private

BASELINE_DIRNAME = "baselines"
TEMP_REGRESSION = 5        # °C: a hotter maximum than the baseline by this much is a warning
FREQ_REGRESSION_PCT = 5.0  # %: a lower average clock than the baseline by this much is a warning
DISK_MBS_REGRESSION_PCT = 10.0  # %: lower disk throughput than the baseline by this much is a warning
DISK_LAT_REGRESSION_PCT = 25.0  # %: higher p99 disk latency than the baseline by this much is a warning
NET_MBPS_REGRESSION_PCT = 10.0   # %: lower TCP throughput than the baseline by this much is a warning
NET_LAT_REGRESSION_PCT = 50.0   # %: higher ping latency than the baseline by this much is a warning
POWER_REGRESSION_PCT = 10.0  # %: higher average power than the baseline by this much is a warning
OPS_REGRESSION_PCT = 5.0   # %: lower bogo ops/s than the baseline by this much is a warning


def baseline_dir(log_dir) -> Path:
    return Path(log_dir) / BASELINE_DIRNAME


def baseline_path(log_dir, node: str) -> Path:
    """logs/baselines/<node>.json - the node name comes from a log file (untrusted), so it must be a real node name."""
    if not is_node_name(node):
        raise ValueError(f"'{node[:60]}' is not a valid node name, the baseline path is refused.")
    return baseline_dir(log_dir) / f"{node}.json"


def resolve_log(value: str, log_dir: Path) -> Path:
    """One log from a path/name, or the newest log of a node."""
    try:
        return _find_log(value, log_dir)
    except CompareError:
        logs = latest_logs_for_node(value, log_dir, 1)
        if not logs:
            raise
        return logs[0]


def save_baseline(run: RunData, log_dir) -> Path:
    """Saves the test as the baseline of its node (replaces the previous one). ValueError if unusable."""
    if not any(s.phase == "test" for s in run.samples):
        raise ValueError("The log has no measurements, it cannot be a baseline.")
    if run.node in ("", "?"):
        raise ValueError("The log does not say which node it is from.")
    data = run_to_dict(run)
    data["baseline_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = baseline_path(log_dir, run.node)
    make_private_dir(path.parent)
    with open_private(path, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load_baseline(log_dir, node: str) -> Optional[dict]:
    """The saved baseline of a node, or None (missing / unreadable file)."""
    try:
        data = json.loads(baseline_path(log_dir, node).read_text(encoding="utf-8"))
    except (OSError, ValueError):                 # includes an invalid node name
        return None
    return data if isinstance(data, dict) else None


def _pct_change(new: float, old: float) -> float:
    return (new - old) / old * 100.0


def _performance(data: dict) -> dict[str, float]:
    """bogo ops/s by name: whole-test stressors, or (stepped/spike) the mean per target load."""
    ops = {str(k): v for k, v in data.get("metrics", {}).items()}
    targets, per_stage = data.get("stage_targets", []), data.get("stage_ops", {})
    groups: dict[int, list[float]] = {}
    for index, value in per_stage.items():
        i = int(index)
        if 0 < i <= len(targets):
            groups.setdefault(targets[i - 1], []).append(value)
    for target, values in sorted(groups.items()):
        ops[f"cpu @ {target} %"] = sum(values) / len(values)
    return ops


def check_against_baseline(run: RunData, base: dict) -> dict:
    """Compares a test with the baseline: {"status": ok|regression|incomparable, "warnings": [...], "lines": [...]}."""
    cur = run_to_dict(run)
    lines: list[str] = []
    warnings: list[str] = []
    name = base.get("source_log") or "baseline"
    if cur["profile"] != base.get("profile"):
        lines.append(f"Baseline ({name}) has profile {base.get('profile')}, this test {cur['profile']}: not comparable.")
        return {"status": "incomparable", "warnings": [], "lines": lines, "baseline": name}
    if cur["stage_targets"] != base.get("stage_targets", []):
        lines.append(f"Baseline ({name}) has different stages: not comparable.")
        return {"status": "incomparable", "warnings": [], "lines": lines, "baseline": name}

    cs, bs = cur["stats"], base.get("stats", {})
    if cs["temp_max"] is not None and bs.get("temp_max") is not None:
        delta = cs["temp_max"] - bs["temp_max"]
        lines.append(f"Max temperature:   {cs['temp_max']} °C vs {bs['temp_max']} °C  ({delta:+d} °C)")
        if delta >= TEMP_REGRESSION:
            warnings.append(f"hotter than baseline by {delta} °C")
    if cs["freq_avg"] and bs.get("freq_avg"):
        change = _pct_change(cs["freq_avg"], bs["freq_avg"])
        lines.append(f"Average clock:     {cs['freq_avg']:.0f} vs {bs['freq_avg']:.0f} MHz  ({change:+.1f} %)")
        if change <= -FREQ_REGRESSION_PCT:
            warnings.append(f"average clock lower by {-change:.1f} %")
    base_ops = _performance(base)
    for stressor, ops in _performance(cur).items():
        if base_ops.get(stressor):
            change = _pct_change(ops, base_ops[stressor])
            lines.append(f"Performance {stressor}: {ops:.0f} vs {base_ops[stressor]:.0f} bogo ops/s  ({change:+.1f} %)")
            if change <= -OPS_REGRESSION_PCT:
                warnings.append(f"{stressor} performance lower by {-change:.1f} %")
    base_disk = {r["name"]: r for r in base.get("disk_results", [])}
    for r in cur.get("disk_results", []):
        old = base_disk.get(r["name"])
        if not old or not old.get("mb_s"):
            continue
        change = _pct_change(r["mb_s"], old["mb_s"])
        lines.append(f"Disk {r['name']}: {r['mb_s']:.1f} vs {old['mb_s']:.1f} MB/s  ({change:+.1f} %)")
        if change <= -DISK_MBS_REGRESSION_PCT:
            warnings.append(f"disk {r['name']} throughput lower by {-change:.1f} %")
        if r.get("lat_p99_ms") and old.get("lat_p99_ms"):
            lat = _pct_change(r["lat_p99_ms"], old["lat_p99_ms"])
            if lat >= DISK_LAT_REGRESSION_PCT:
                warnings.append(f"disk {r['name']} p99 latency higher by {lat:.0f} %")
    base_net = {r["name"]: r for r in base.get("net_results", [])}
    for r in cur.get("net_results", []):
        old = base_net.get(r["name"])
        if not old:
            continue
        if r["name"].startswith("tcp") and r.get("mbps") and old.get("mbps"):
            change = _pct_change(r["mbps"], old["mbps"])
            lines.append(f"Network {r['name']}: {r['mbps']:.0f} vs {old['mbps']:.0f} Mbit/s  ({change:+.1f} %)")
            if change <= -NET_MBPS_REGRESSION_PCT:
                warnings.append(f"network {r['name']} throughput lower by {-change:.1f} %")
        elif r["name"] == "ping" and r.get("avg_ms") and old.get("avg_ms"):
            change = _pct_change(r["avg_ms"], old["avg_ms"])
            lines.append(f"Ping: {r['avg_ms']:.2f} vs {old['avg_ms']:.2f} ms  ({change:+.0f} %)")
            if change >= NET_LAT_REGRESSION_PCT and r["avg_ms"] - old["avg_ms"] > 0.2:
                warnings.append(f"ping latency higher by {change:.0f} %")
            if r.get("loss_pct", 0) > old.get("loss_pct", 0):
                warnings.append(f"ping loss rose to {r['loss_pct']:g} %")
        elif r["name"] == "udp" and r.get("loss_pct", 0) > old.get("loss_pct", 0) + 1:
            warnings.append(f"UDP loss rose to {r['loss_pct']:g} %")
    if cs.get("power_avg") and bs.get("power_avg"):
        change = _pct_change(cs["power_avg"], bs["power_avg"])
        lines.append(f"Average power:     {cs['power_avg']:.1f} vs {bs['power_avg']:.1f} W  ({change:+.1f} %)")
        if change >= POWER_REGRESSION_PCT:
            warnings.append(f"power draw higher by {change:.1f} %")
    if cs["throttling"] and not bs.get("throttling"):
        warnings.append("throttling appeared (the baseline had none)")
    lines.append(f"Throttling:        {'yes' if cs['throttling'] else 'no'} (baseline: {'yes' if bs.get('throttling') else 'no'})")
    if cur["baseline_temp"] is not None and base.get("baseline_temp") is not None \
            and abs(cur["baseline_temp"] - base["baseline_temp"]) >= 5:
        lines.append(f"Note: idle temperature differs ({cur['baseline_temp']} vs {base['baseline_temp']} °C) - "
                     f"ambient temperature or residual heat.")
    return {"status": "regression" if warnings else "ok", "warnings": warnings, "lines": lines,
            "baseline": name}


def baseline_report(result: dict) -> list[str]:
    """Printable lines of a check result."""
    title = f"BASELINE CHECK (vs {result['baseline']})"
    out = [title, "-" * len(title), *result["lines"]]
    if result["status"] == "ok":
        out.append("Verdict: OK, within the baseline.")
    elif result["status"] == "regression":
        out.append("Verdict: ⚠️  REGRESSION - " + "; ".join(result["warnings"]) + ".")
    return out


def check_run(run: RunData, log_dir) -> Optional[dict]:
    """Check of a finished test against its node's baseline; None when there is no baseline."""
    base = load_baseline(log_dir, run.node)
    if base is None:
        return None
    if base.get("source_log") and base["source_log"] == Path(run.path).name:
        return None                         # the baseline itself
    return check_against_baseline(run, base)
