"""Machine-readable export of a finished test: JSON (everything) and CSV (the measurements)."""
from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import Optional

from .logparse import RunData
from .models import WARN_TEMP
from .paths import open_private
from .summary import run_stats

SCHEMA_VERSION = 1
EXPORT_CHOICES = ("json", "csv", "both", "none")
EXPORT_DEFAULT = "json"
CSV_FIELDS = ("t", "phase", "stage", "cpu_temp", "freq_mhz", "cpu_pct", "mem_used_mib", "mem_used_pct", "power_w")


def run_to_dict(run: RunData, warn_temp: int = WARN_TEMP) -> dict:
    """All that the log knows about a test as plain JSON-able data (stats computed like in the summary)."""
    targets = run.stage_targets if run.profile in ("stepped", "spike") and run.stage_targets else None
    stats = run_stats(run.samples, run.baseline_temp, warn_temp, stage_targets=targets)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_log": Path(run.path).name if run.path else "",
        "node": run.node,
        "started": run.started,
        "duration_text": run.duration_text,
        "profile": run.profile,
        "notes": run.notes,
        "concurrent": run.concurrent,
        "baseline_temp": run.baseline_temp,
        "net_peer": run.net_peer,
        "net_watch": run.net_watch,
        "net_results": [{"name": r.name, **r.values} for r in run.net_results],
        "disk_results": [dataclasses.asdict(r) for r in run.disk_results],
        "disk_health": list(run.disk_health),
        "power_limits_w": {"pl1": run.power_limits[0], "pl2": run.power_limits[1]},
        "stage_targets": list(run.stage_targets),
        "stage_ops": {str(k): v for k, v in sorted(run.stage_ops.items())},
        "metrics": {m.name: m.ops_per_s for m in run.metrics},
        "stats": dataclasses.asdict(stats),
        "samples": [dataclasses.asdict(s) for s in run.samples],
    }


def export_paths(log_path: str, formats: str) -> dict[str, Path]:
    """Where the exports of a log go: next to it, same name, extension .json / .csv."""
    base = Path(log_path)
    wanted = {"json": ("json",), "csv": ("csv",), "both": ("json", "csv")}.get(formats, ())
    return {ext: base.with_suffix(f".{ext}") for ext in wanted}


def write_exports(run: RunData, log_path: str, formats: str = EXPORT_DEFAULT,
                  extra: Optional[dict] = None) -> list[Path]:
    """Writes the requested exports (private files), returns the paths written. `extra` is merged into the JSON."""
    written = []
    for ext, path in export_paths(log_path, formats).items():
        if ext == "json":
            data = run_to_dict(run)
            data.update(extra or {})
            with open_private(path, "w") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        else:
            with open_private(path, "w") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_FIELDS)
                for s in run.samples:
                    writer.writerow([s.t, s.phase, s.stage, s.cpu_temp, s.freq_mhz,
                                     s.cpu_pct, s.mem_used_mib, s.mem_used_pct, s.power_w])
        written.append(path)
    return written
