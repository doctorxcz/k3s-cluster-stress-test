"""Pure functions for processing kubectl and probe output (no side effects)."""
from __future__ import annotations

import ipaddress
import re
from typing import Optional, Sequence

from .models import NodeInfo, NodeWorkload, ProbeData, ToolPod, TopMetrics

_SENSOR_LABELS = {
    "coretemp": "CPU",
    "k10temp": "CPU",
    "zenpower": "CPU",
    "amdgpu": "GPU",
    "radeon": "GPU",
    "nouveau": "GPU",
    "nvme": "NVMe",
    "drivetemp": "HDD",
}

MAX_DURATION_S = 7 * 24 * 3600          # no test longer than a week (activeDeadlineSeconds, runaway typos)
_NODE_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
MAX_TEXT = 500


def clean_ip(text: str) -> str:
    """The text if it is exactly an IPv4/IPv6 address, otherwise "" (addresses go into shell scripts run in pods)."""
    text = (text or "").strip()
    try:
        return str(ipaddress.ip_address(text)) if text else ""
    except ValueError:
        return ""


def is_node_name(text: str) -> bool:
    """A Kubernetes node name (DNS-1123 subdomain): it can never look like a kubectl option or carry a shell command."""
    return isinstance(text, str) and 0 < len(text) <= 253 and _NODE_NAME_RE.fullmatch(text) is not None


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    """One harmless line: control characters (also newlines) become spaces, the length is limited.

    Free text (--notes) goes into result logs that are read back line by line - a newline would forge log lines.
    """
    return _CTRL_RE.sub(" ", str(text)).strip()[:limit]


_UNITS_TO_MIB = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}


def parse_quantity_mib(quantity: str) -> int:
    """'3163Mi' -> 3163, '8Gi' -> 8192, '512Ki' -> 0, a bare number = bytes."""
    quantity = quantity.strip()
    for suffix, multiplier in _UNITS_TO_MIB.items():
        if quantity.endswith(suffix):
            return int(float(quantity[: -len(suffix)]) * multiplier)
    return int(float(quantity) / (1024 * 1024))


_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)(?:min|m))?(?:(\d+)(?:sec|s))?")


def parse_duration(text: str) -> int:
    """Duration in seconds. Accepts '90', '90s', '5m', '1h', '1h30m', '1h 5m 30s'."""
    cleaned = text.strip().lower().replace(" ", "")
    if cleaned.isdigit():
        seconds = int(cleaned)
    else:
        match = _DURATION_RE.fullmatch(cleaned)
        if not cleaned or match is None or not any(match.groups()):
            raise ValueError(
                f"Invalid duration '{text}'. Examples: 90, 90s, 5m, 1h, 1h30m.")
        hours, minutes, secs = (int(g) if g else 0 for g in match.groups())
        seconds = hours * 3600 + minutes * 60 + secs
    if seconds < 1:
        raise ValueError("The test duration must be at least 1 second.")
    if seconds > MAX_DURATION_S:
        raise ValueError(f"The duration is too long (maximum {MAX_DURATION_S // 86400} days).")
    return seconds


def format_duration(seconds: int) -> str:
    """3930 -> '1 h 5 min 30 s' (zero parts are left out)."""
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def describe_duration(seconds: int) -> str:
    """'5 min (300 s)'; if the duration is only seconds, returns '45 s'."""
    text = format_duration(seconds)
    return text if text == f"{seconds} s" else f"{text} ({seconds} s)"


def parse_pct(text: str) -> Optional[float]:
    """'45%' -> 45.0; None if it is not a number with a %."""
    text = text.strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def parse_top(line: str) -> Optional[TopMetrics]:
    """Parses a `kubectl top node --no-headers` line; None on invalid input."""
    parts = line.split()
    if len(parts) < 5:
        return None
    try:
        mem_mib = parse_quantity_mib(parts[3])
    except ValueError:
        return None
    return TopMetrics(cpu=parts[1], cpu_pct=parts[2], mem=parts[3],
                      mem_pct=parts[4], mem_mib=mem_mib)


def label_for_sensor(hwmon_name: str) -> str:
    """Translates a hwmon sensor name to a readable label (CPU, GPU, NVMe, HDD)."""
    return _SENSOR_LABELS.get(hwmon_name, "HDD")


def parse_probe_output(text: str) -> ProbeData:
    """Parses the probe output: lines `temp <sensor> <millidegrees>` and `freq <MHz>`."""
    data = ProbeData()
    for line in text.splitlines():
        parts = line.split()
        try:
            if len(parts) == 3 and parts[0] == "temp":
                label = label_for_sensor(parts[1])
                degrees = int(parts[2]) // 1000
                data.temps[label] = max(degrees, data.temps.get(label, degrees))
            elif len(parts) == 2 and parts[0] == "freq":
                data.freq_mhz = int(parts[1])
            elif len(parts) == 2 and parts[0] == "mem_available_kb":
                data.mem_available_mib = int(parts[1]) // 1024
            elif len(parts) == 2 and parts[0] == "mem_total_kb":
                data.mem_total_mib = int(parts[1]) // 1024
            elif len(parts) == 2 and parts[0] == "ping_ms":
                if parts[1] == "lost":
                    data.ping_lost = True
                else:
                    data.ping_ms = float(parts[1])
            elif len(parts) == 5 and parts[0] == "rapl_uj":
                data.energy_uj = int(parts[1])
                data.energy_max_uj = int(parts[2]) or None
                data.pl1_w = int(parts[3]) / 1e6 or None
                data.pl2_w = int(parts[4]) / 1e6 or None
            elif len(parts) == 3 and parts[0] == "cpu_stat":
                data.cpu_total, data.cpu_idle = int(parts[1]), int(parts[2])
        except ValueError:
            continue
    return data


def power_watts(prev: Optional[tuple[int, float]], energy_uj: Optional[int], now: float,
                max_uj: Optional[int] = None) -> Optional[float]:
    """Power in W from two RAPL energy readings (prev = (µJ, time in s)); handles the counter wrap-around."""
    if prev is None or energy_uj is None or now <= prev[1]:
        return None
    delta = energy_uj - prev[0]
    if delta < 0:
        if not max_uj:
            return None
        delta += max_uj
    return delta / 1e6 / (now - prev[1])


def cpu_percent(prev: Optional[tuple[int, int]],
                cur: Optional[tuple[int, int]]) -> Optional[float]:
    """CPU utilisation in % from the difference of the (total, idle) counters between two readings."""
    if prev is None or cur is None:
        return None
    d_total, d_idle = cur[0] - prev[0], cur[1] - prev[1]
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, (d_total - d_idle) / d_total * 100))


def calc_ram_target_mib(total_mib: int, used_mib: int, pct: int,
                        reserve_mib: int = 256) -> int:
    """How many MiB of RAM to allocate: pct % of FREE memory, always with a reserve."""
    free = total_mib - used_mib
    target = free * pct // 100
    target = min(target, free - reserve_mib)
    return max(target, 0)


def tool_pods_from_json(data: dict) -> list[ToolPod]:
    """Pods with the label of this tool from `kubectl get pods -l ... -o json`."""
    pods = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        pods.append(ToolPod(
            name=meta.get("name", "?"),
            node=item.get("spec", {}).get("nodeName", ""),
            phase=item.get("status", {}).get("phase", ""),
            run_id=meta.get("labels", {}).get("run-id", ""),
        ))
    return pods


STAGE_RE = re.compile(r"^STRESS-STAGE (\d+)/(\d+) (\d+)%")


def parse_steps(text: str) -> tuple[int, ...]:
    """'25,50,75,100' (or with slashes/spaces) -> (25, 50, 75, 100)."""
    parts = [p for p in re.split(r"[,/\s]+", text.strip()) if p]
    try:
        steps = tuple(int(p.rstrip("%")) for p in parts)
    except ValueError:
        raise ValueError(f"Invalid stages '{text}'. Enter e.g. 25,50,75,100.") from None
    if (not steps or any(not 1 <= s <= 100 for s in steps)
            or list(steps) != sorted(set(steps))):
        raise ValueError("Stages must be ascending numbers 1–100 %, e.g. 25,50,75,100.")
    return steps


def describe_steps(steps: Sequence[int]) -> str:
    return "/".join(str(s) for s in steps)


def count_completed_runs(lines: Sequence[str]) -> int:
    """How many runs of stress-ng finished successfully (in the stepped test one per stage)."""
    return sum(1 for line in lines
               if "successful run completed" in line and "unsuccessful" not in line)


def split_stage_lines(lines: Sequence[str]) -> dict[int, list[str]]:
    """Splits the stepped test output by the `STRESS-STAGE i/n P%` markers."""
    stages: dict[int, list[str]] = {}
    current = 0
    for line in lines:
        match = STAGE_RE.match(line)
        if match:
            current = int(match.group(1))
            stages.setdefault(current, [])
        elif current:
            stages[current].append(line)
    return stages


SYSTEM_NAMESPACE = "kube-system"
TOOL_APP_LABEL = "stress-test-tool"


def workload_from_json(data: dict) -> NodeWorkload:
    """Pods running on the node (from `kubectl get pods -A --field-selector ...`).

    Finished pods, pods of this tool and kube-system are not counted
    (kube-system is only counted).
    """
    user, system = [], 0
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        if item.get("status", {}).get("phase") not in ("Running", "Pending"):
            continue
        if meta.get("labels", {}).get("app") == TOOL_APP_LABEL:
            continue
        ns, name = meta.get("namespace", "?"), meta.get("name", "?")
        if ns == SYSTEM_NAMESPACE:
            system += 1
        else:
            user.append((ns, name))
    return NodeWorkload(tuple(sorted(user)), system)


def format_workload(work: NodeWorkload, per_namespace: int = 3) -> list[str]:
    """A readable overview by namespace: 'minecraft-lobby (1): minecraft-lobby-abc'."""
    by_ns: dict[str, list[str]] = {}
    for ns, name in work.user_pods:
        by_ns.setdefault(ns, []).append(name)
    lines = []
    for ns in sorted(by_ns):
        names = by_ns[ns]
        shown = ", ".join(names[:per_namespace])
        more = f" (+{len(names) - per_namespace} more)" if len(names) > per_namespace else ""
        lines.append(f"{ns} ({len(names)}): {shown}{more}")
    return lines


def parse_stressng_outcome(lines: list[str]) -> str:
    """'ok' / 'failed' / 'unknown' according to the final message of stress-ng.

    At the end stress-ng prints 'successful run completed in ...' (or
    'unsuccessful run completed ...'). If it is missing, the pod ended some other way (e.g. someone
    deleted it) and it is not a regular completion.
    """
    text = "\n".join(lines)
    if "unsuccessful run completed" in text:
        return "failed"
    if "successful run completed" in text:
        return "ok"
    return "unknown"


def node_from_json(data: dict) -> NodeInfo:
    """Builds NodeInfo from the output of `kubectl get node -o json`."""
    meta = data.get("metadata", {})
    status = data.get("status", {})
    labels = meta.get("labels", {})
    info = status.get("nodeInfo", {})
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions", [])
    )
    is_cp = ("node-role.kubernetes.io/control-plane" in labels
             or "node-role.kubernetes.io/master" in labels)
    return NodeInfo(
        name=meta.get("name", "?"),
        ready=ready,
        is_control_plane=is_cp,
        allocatable_mem_mib=parse_quantity_mib(
            status.get("allocatable", {}).get("memory", "0")),
        capacity_cpu=str(status.get("capacity", {}).get("cpu", "?")),
        os_image=info.get("osImage", "?"),
        kernel=info.get("kernelVersion", "?"),
        architecture=info.get("architecture", "?"),
        runtime=info.get("containerRuntimeVersion", "?"),
        internal_ip=next((clean_ip(a.get("address", "")) for a in status.get("addresses", [])
                          if a.get("type") == "InternalIP" and clean_ip(a.get("address", ""))), ""),
    )
