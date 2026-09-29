"""Disk benchmark (fio): job definitions, shell command, parsing of fio JSON, formatting and log lines."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

DISK_DIR = "/bench"                      # emptyDir of the load pod = the node's disk
DISK_SIZE_DEFAULT = 1024                 # MiB, size of the test file
MIN_DISK_SIZE, MAX_DISK_SIZE = 128, 8192
DISK_JOB_TIME_DEFAULT = 15               # s, one job
MIN_DISK_JOB_TIME, MAX_DISK_JOB_TIME = 5, 120
MASTER_DISK_SIZE = 256                   # MiB, the master reads only and with a small file

# (name, fio rw, block size, queue depth, writes?)
JOBS = (
    ("seq-read", "read", "1M", 8, False),
    ("seq-write", "write", "1M", 8, True),
    ("rand-read", "randread", "4k", 32, False),
    ("rand-write", "randwrite", "4k", 32, True),
)


def jobs(read_only: bool = False) -> tuple:
    return tuple(j for j in JOBS if not (read_only and j[4]))


@dataclass
class DiskResult:
    name: str
    mb_s: float
    iops: float
    lat_avg_ms: Optional[float] = None
    lat_p99_ms: Optional[float] = None

    def line(self) -> str:
        lat = f" | lat avg {self.lat_avg_ms:.2f} ms" if self.lat_avg_ms is not None else ""
        p99 = f" | p99 {self.lat_p99_ms:.2f} ms" if self.lat_p99_ms is not None else ""
        return f"{self.name:<11} {self.mb_s:8.1f} MB/s | {self.iops:8.0f} IOPS{lat}{p99}"

    def log_line(self) -> str:
        """Machine-readable line of the log (read back by RESULT_RE)."""
        avg = "?" if self.lat_avg_ms is None else f"{self.lat_avg_ms:.3f}"
        p99 = "?" if self.lat_p99_ms is None else f"{self.lat_p99_ms:.3f}"
        return f"Disk result: {self.name} | {self.mb_s:.1f} MB/s | {self.iops:.0f} IOPS | lat {avg} ms | p99 {p99} ms"


RESULT_RE = re.compile(r"^Disk result:\s*(\S+)\s*\|\s*([\d.]+) MB/s\s*\|\s*([\d.]+) IOPS\s*\|\s*lat (\S+) ms\s*\|\s*p99 (\S+) ms")


def parse_result_line(line: str) -> Optional[DiskResult]:
    m = RESULT_RE.match(line.strip())
    if not m:
        return None
    num = lambda v: None if v == "?" else float(v)          # noqa: E731
    return DiskResult(m.group(1), float(m.group(2)), float(m.group(3)), num(m.group(4)), num(m.group(5)))


def parse_fio_json(name: str, text: str) -> Optional[DiskResult]:
    """DiskResult from the JSON that fio prints for one job (None if unreadable)."""
    try:
        job = json.loads(text)["jobs"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    side = job.get("write") if job.get("write", {}).get("io_bytes", 0) > job.get("read", {}).get("io_bytes", 0) \
        else job.get("read", {})
    if not side:
        return None
    mb_s = side.get("bw_bytes", side.get("bw", 0) * 1024) / 1e6
    lat = side.get("lat_ns", {}).get("mean")
    p99 = side.get("clat_ns", {}).get("percentile", {}).get("99.000000")
    return DiskResult(name, mb_s, float(side.get("iops", 0)),
                      lat / 1e6 if lat is not None else None, p99 / 1e6 if p99 is not None else None)


def build_disk_command(size_mib: int, job_time: int, read_only: bool = False) -> str:
    """One shell loop over the fio jobs. Markers: DISK-JOB i/n name, DISK-RESULT name <json>, DISK-DONE / DISK-FAILED."""
    selected = jobs(read_only)
    specs = " ".join(f'"{name} {rw} {bs} {qd}"' for name, rw, bs, qd, _ in selected)
    fio = (f"fio --name=$1 --filename={DISK_DIR}/fio.dat --size={size_mib}M --rw=$2 --bs=$3 --iodepth=$4 "
           f"--ioengine=libaio --runtime={job_time} --time_based --group_reporting --output-format=json")
    return (f"rm -f {DISK_DIR}/fio.dat; i=0; ok=1; for spec in {specs}; do set -- $spec; i=$((i+1)); "
            f'echo "DISK-JOB $i/{len(selected)} $1"; '
            f'out=$({fio} --direct=1 2>/dev/null) || out=$({fio} --direct=0 2>/dev/null) || '
            f'{{ echo "DISK-FAILED $1"; ok=0; break; }}; '
            f'echo "DISK-RESULT $1 $(echo "$out" | tr -d \'\\n\')"; done; '
            f'rm -f {DISK_DIR}/fio.dat; [ "$ok" = 1 ] && echo DISK-DONE')
