"""Disk health (SMART) from `smartctl -j`: parsing and the verdict for the preflight."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

PERCENT_USED_WARN = 90        # NVMe wear (%) from which a warning is printed
TEMP_WARN = 70                # °C of the disk


@dataclass
class DiskHealth:
    dev: str
    model: str = "?"
    passed: Optional[bool] = None            # SMART overall health; None = unknown
    temp: Optional[int] = None
    power_on_hours: Optional[int] = None
    reallocated: Optional[int] = None        # ATA: Reallocated_Sector_Ct
    pending: Optional[int] = None            # ATA: Current_Pending_Sector
    uncorrectable: Optional[int] = None      # ATA: Offline_Uncorrectable
    percentage_used: Optional[int] = None    # NVMe wear
    media_errors: Optional[int] = None       # NVMe
    critical_warning: Optional[int] = None   # NVMe
    error: str = ""
    problems: list[str] = field(default_factory=list)   # warnings
    fatal: list[str] = field(default_factory=list)      # reasons to refuse the test

    def line(self) -> str:
        bits = ["PASSED" if self.passed else "FAILED" if self.passed is False else "health unknown"]
        if self.temp is not None:
            bits.append(f"{self.temp} °C")
        if self.power_on_hours is not None:
            bits.append(f"{self.power_on_hours} h")
        if self.percentage_used is not None:
            bits.append(f"wear {self.percentage_used} %")
        if self.reallocated is not None:
            bits.append(f"reallocated {self.reallocated}")
        if self.pending is not None:
            bits.append(f"pending {self.pending}")
        text = f"{self.dev}: {self.model} | " + " | ".join(bits)
        if self.error:
            text += f" | {self.error}"
        return text


def _attr(table: list, attr_id: int) -> Optional[int]:
    for row in table:
        if row.get("id") == attr_id:
            raw = row.get("raw", {}).get("value")
            return int(raw) if isinstance(raw, (int, float)) else None
    return None


def parse_disk(dev: str, text: str) -> DiskHealth:
    disk = DiskHealth(dev)
    try:
        data = json.loads(text)
    except ValueError:
        disk.error = "smartctl output could not be read"
        return disk
    disk.model = data.get("model_name") or data.get("device", {}).get("name") or "?"
    status = data.get("smart_status", {})
    if "passed" in status:
        disk.passed = bool(status["passed"])
    disk.temp = data.get("temperature", {}).get("current")
    disk.power_on_hours = data.get("power_on_time", {}).get("hours")
    table = data.get("ata_smart_attributes", {}).get("table", [])
    disk.reallocated, disk.pending, disk.uncorrectable = _attr(table, 5), _attr(table, 197), _attr(table, 198)
    nvme = data.get("nvme_smart_health_information_log", {})
    disk.percentage_used = nvme.get("percentage_used")
    disk.media_errors = nvme.get("media_errors")
    disk.critical_warning = nvme.get("critical_warning")
    if disk.passed is False:
        disk.fatal.append(f"{dev}: SMART health check FAILED")
    if disk.critical_warning:
        disk.fatal.append(f"{dev}: NVMe critical warning ({disk.critical_warning})")
    if disk.reallocated:
        disk.problems.append(f"{dev}: {disk.reallocated} reallocated sectors")
    if disk.pending:
        disk.problems.append(f"{dev}: {disk.pending} sectors pending reallocation")
    if disk.uncorrectable:
        disk.problems.append(f"{dev}: {disk.uncorrectable} offline uncorrectable sectors")
    if disk.media_errors:
        disk.problems.append(f"{dev}: {disk.media_errors} NVMe media errors")
    if disk.percentage_used is not None and disk.percentage_used >= PERCENT_USED_WARN:
        disk.problems.append(f"{dev}: {disk.percentage_used} % of the NVMe endurance used")
    if disk.temp is not None and disk.temp >= TEMP_WARN:
        disk.problems.append(f"{dev}: disk temperature {disk.temp} °C")
    return disk


def parse_smart_output(text: str) -> tuple[list[DiskHealth], str]:
    """(disks, note): note explains why there is nothing to show ("" when disks were read)."""
    if "SMART-UNAVAILABLE" in text:
        return [], "smartmontools could not be installed in the pod (no internet / apt?)"
    disks: list[DiskHealth] = []
    dev, buf = None, []
    for line in text.splitlines():
        if line.startswith("SMART-BEGIN "):
            dev, buf = line.split(None, 1)[1].strip(), []
        elif line.startswith("SMART-END"):
            if dev:
                disks.append(parse_disk(dev, "\n".join(buf)))
            dev = None
        elif dev is not None:
            buf.append(line)
    if not disks:
        return [], "no disks reported by smartctl (virtual machine or a RAID controller?)"
    return disks, ""
