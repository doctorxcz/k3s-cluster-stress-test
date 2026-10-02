"""GPU test (NVIDIA): gpu-burn load, nvidia-smi readings, throttle reasons, summary and log fields."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import images

GPU_IMAGE_DEFAULT = "nvidia/cuda:12.9.1-devel-ubuntu24.04"   # the FALLBACK image (builds gpu-burn in the pod); CUDA 12.x is fine for Pascal cards (e.g. Quadro P620)
# The normal image is the prebuilt one (images.image("gpu")): the CUDA runtime with gpu_burn already built for several GPU generations
GPU_BURN_REPO = "https://github.com/wilicc/gpu-burn"
GPU_BURN_COMMIT = "3ead140434da9473582b68452f7115967a7a0581"       # pinned: the pod must never build whatever the repository has today
_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")


def valid_image(text: str) -> bool:
    """A container image reference (letters, digits and . _ / : @ + - only; it goes into a manifest, never into a shell)."""
    return bool(_IMAGE_RE.fullmatch(text or ""))
GPU_RESOURCE = "nvidia.com/gpu"
GPU_MAX_TEMP_DEFAULT = 80            # °C, abort (selectable; these cards usually run up to 85-90 °C)
GPU_WARN_MARGIN = 5                  # °C below the limit from which only a warning is shown
GPU_WARN_TEMP = GPU_MAX_TEMP_DEFAULT - GPU_WARN_MARGIN
SMI_TIMEOUT = 10                     # s, one nvidia-smi reading (a stuck card must not block the monitor)
SMI_LOST_LIMIT = 3                   # failed readings in a row after which the test stops (GPU guard is blind)
GPU_MEM_PCT_DEFAULT = 90
MIN_GPU_TIME, MAX_GPU_TIME = 20, 3600
GPU_LIMIT_MIB = 2048                 # memory limit of the load pod (RAM of the host, not of the GPU)

SMI_FIELDS = ("temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,utilization.gpu,"
              "memory.used,memory.total,pstate,{throttle},fan.speed")
SMI_QUERY = ("nvidia-smi --query-gpu=" + SMI_FIELDS.format(throttle="clocks_throttle_reasons.active")
             + " --format=csv,noheader,nounits 2>/dev/null || nvidia-smi --query-gpu="
             + SMI_FIELDS.format(throttle="clocks_event_reasons.active") + " --format=csv,noheader,nounits")

# bits of the nvidia-smi throttle reasons mask -> name
THROTTLE_BITS = (
    (0x1, "gpu_idle"), (0x2, "app_clocks"), (0x4, "sw_power_cap"), (0x8, "hw_slowdown"),
    (0x10, "sync_boost"), (0x20, "sw_thermal"), (0x40, "hw_thermal"), (0x80, "hw_power_brake"),
)
BAD_THROTTLE = 0x4 | 0x8 | 0x20 | 0x40 | 0x80     # idle / application clocks / sync boost are harmless
THERMAL_THROTTLE = 0x8 | 0x20 | 0x40


@dataclass(frozen=True)
class GpuReading:
    temp: Optional[int] = None
    power_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    sm_mhz: Optional[int] = None
    mem_mhz: Optional[int] = None
    util_pct: Optional[int] = None
    mem_used_mib: Optional[int] = None
    mem_total_mib: Optional[int] = None
    pstate: str = ""
    throttle: Optional[int] = None
    fan_pct: Optional[int] = None


def _num(text: str, kind=float):
    """'[N/A]', '[Not Supported]', '' -> None."""
    text = text.strip()
    if not text or text.startswith("["):
        return None
    try:
        return kind(text)
    except ValueError:
        return None


def parse_smi_csv(text: str) -> Optional[GpuReading]:
    """First GPU line of `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` (None if unreadable)."""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue
        mask = None
        if parts[9] and not parts[9].startswith("["):
            try:
                mask = int(parts[9], 16) if parts[9].lower().startswith("0x") else int(parts[9])
            except ValueError:
                mask = None
        return GpuReading(
            temp=_num(parts[0], int), power_w=_num(parts[1]), power_limit_w=_num(parts[2]),
            sm_mhz=_num(parts[3], int), mem_mhz=_num(parts[4], int), util_pct=_num(parts[5], int),
            mem_used_mib=_num(parts[6], int), mem_total_mib=_num(parts[7], int),
            pstate="" if parts[8].startswith("[") else parts[8], throttle=mask,
            fan_pct=_num(parts[10], int) if len(parts) > 10 else None)
    return None


def throttle_names(mask: Optional[int], only_bad: bool = False) -> list[str]:
    if not mask:
        return []
    return [name for bit, name in THROTTLE_BITS if mask & bit and (not only_bad or bit & BAD_THROTTLE)]


class GpuGuard:
    """Stops the test when the GPU temperature stays at/above the limit for N readings in a row."""

    def __init__(self, limit: int = GPU_MAX_TEMP_DEFAULT, consecutive: int = 2) -> None:
        self.limit, self.consecutive, self._hot = limit, consecutive, 0

    def update(self, temp: Optional[int]) -> bool:
        if temp is None:
            return False
        self._hot = self._hot + 1 if temp >= self.limit else 0
        return self._hot >= self.consecutive


# --- log field --------------------------------------------------------------------------
def _f(value, fmt: str) -> str:
    return "?" if value is None else fmt.format(value)


def _mem(r: GpuReading) -> str:
    if r.mem_used_mib is None:
        return "?"
    return f"{r.mem_used_mib}/{r.mem_total_mib}MiB" if r.mem_total_mib else f"{r.mem_used_mib}MiB"


def format_gpu(reading: Optional[GpuReading], warn_temp: int = GPU_WARN_TEMP) -> str:
    """The ` | GPU: ...` tail of a measurement line ('' when there is no reading)."""
    if reading is None:
        return ""
    text = (f" | GPU: {_f(reading.temp, '{}°C')} {_f(reading.power_w, '{:.1f}W')} "
            f"{_f(reading.sm_mhz, '{}MHz')} {_f(reading.util_pct, '{}%')} "
            f"{_mem(reading)} thr={reading.throttle or 0:#x}"
            + (f" fan={reading.fan_pct}%" if reading.fan_pct is not None else ""))
    if reading.temp is not None and reading.temp >= warn_temp:
        text += " ⚠️ GPU HOT!"
    return text


_GPU_FIELD_RE = re.compile(r"^(\S+?)(?:°C)?\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+thr=(0x[0-9a-fA-F]+)(?:\s+fan=(\d+)%)?")


def parse_gpu_field(text: str) -> Optional[GpuReading]:
    """Reads back what format_gpu wrote (text after `GPU:`)."""
    m = _GPU_FIELD_RE.match(text.strip())
    if not m:
        return None
    strip = lambda v, suf: None if v == "?" else v[:-len(suf)]       # noqa: E731
    try:
        return GpuReading(
            temp=_num(m.group(1), int) if m.group(1) != "?" else None,
            power_w=_num(strip(m.group(2), "W") or "", float), sm_mhz=_num(strip(m.group(3), "MHz") or "", int),
            util_pct=_num(strip(m.group(4), "%") or "", int),
            mem_used_mib=_num((strip(m.group(5), "MiB") or "").split("/")[0], int),
            mem_total_mib=_num((strip(m.group(5), "MiB") or "").partition("/")[2], int),
            throttle=int(m.group(6), 16), fan_pct=int(m.group(7)) if m.group(7) else None)
    except ValueError:
        return None


# --- static info of the card (read once, before the load) ----------------------------------------
INFO_QUERY = ("nvidia-smi --query-gpu=name,driver_version,memory.total,power.limit,clocks.max.sm,clocks.max.mem,"
              "pcie.link.gen.max,pcie.link.width.max,compute_cap,vbios_version --format=csv,noheader,nounits")
INFO_LABELS = ("GPU", "Driver", "Memory", "Power limit", "Max SM clock", "Max memory clock", "PCIe (max)", "PCIe width",
               "Compute capability", "VBIOS")


def parse_gpu_info(text: str) -> list[str]:
    """Readable `label: value` lines from INFO_QUERY (an empty list when unreadable); N/A values are shown as such."""
    from .parsing import clean_text
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue
        v = ["N/A" if p.startswith("[") or not p else p for p in parts]
        units = ("", "", " MiB", " W", " MHz", " MHz", "", "x", "", "")
        pairs = [(label, val + (unit if val != "N/A" else "")) for label, val, unit in zip(INFO_LABELS, v, units)]
        if v[6] != "N/A":
            pairs[6] = ("PCIe (max)", f"gen {v[6]}")
        return [clean_text(f"{k}: {val}", 120) for k, val in pairs]
    return []


# --- load command ---------------------------------------------------------------------------
def build_gpu_setup(mem_pct: int = GPU_MEM_PCT_DEFAULT) -> str:
    """Shell run after apt: finds a ready gpu_burn (prebuilt --gpu-image) or builds it for this card."""
    return ("GPU_BIN=$(command -v gpu_burn || ls /app/gpu_burn 2>/dev/null); "
            "if [ -z \"$GPU_BIN\" ]; then "
            f"{images.ensure_tools('git make g++')} && "
            f"git init -q /tmp/gpu-burn && cd /tmp/gpu-burn && git fetch -q --depth 1 {GPU_BURN_REPO} {GPU_BURN_COMMIT} && "
            "git checkout -q FETCH_HEAD && "
            "CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n1 | tr -d '. '); "
            "make -s COMPUTE=${CC:-61} >/dev/null 2>&1 && GPU_BIN=/tmp/gpu-burn/gpu_burn; fi; "
            "[ -n \"$GPU_BIN\" ] && [ -x \"$GPU_BIN\" ]")


def build_gpu_command(duration: int, mem_pct: int = GPU_MEM_PCT_DEFAULT, double: bool = False) -> str:
    """Runs gpu_burn and prints GPU-DONE / GPU-FAILED <why> (the result is judged from its final lines)."""
    flags = f"-m {mem_pct}%" + (" -d" if double else "")
    return (f'"$GPU_BIN" {flags} {duration} > /tmp/gb.out 2>&1; '
            "tr '\\r' '\\n' < /tmp/gb.out | grep -E 'GPU [0-9]+:|Tested|rrors: [1-9]|rror:|FAULTY|Couldn' | tail -n 6 | sed 's/^/GPU-INFO /'; "
            "tr '\\r' '\\n' < /tmp/gb.out | grep 'Gflop/s' | tail -n 1 | sed 's/^/GPU-PERF /'; "
            "if grep -q 'FAULTY' /tmp/gb.out; then echo 'GPU-FAILED faulty (compute errors)'; "
            "elif grep -qE 'GPU [0-9]+: OK' /tmp/gb.out; then echo GPU-DONE; "
            "else echo 'GPU-FAILED gpu_burn did not finish correctly'; fi; "
            "sleep 3600")             # the pod stays alive (idle) so that the cooldown can read the GPU; the runner deletes it


def gpu_state(hw_text: str, allocatable: int, hw_skipped: bool = False) -> tuple[str, str]:
    """('ok'|'no-gpu'|'no-plugin', message) from the hardware text and the node's nvidia.com/gpu allocatable."""
    if allocatable > 0:
        return "ok", ""
    if hw_skipped:
        return "no-plugin", ("Kubernetes does not offer nvidia.com/gpu on this node: the NVIDIA driver, nvidia-container-toolkit, "
                             "runtime 'nvidia' or the device plugin is missing (see HELPDESK.md). Hardware detection was skipped (--no-hw).")
    if re.search(r"nvidia", hw_text, re.I):
        return "no-plugin", ("The node has an NVIDIA GPU, but Kubernetes does not offer nvidia.com/gpu "
                             "(driver, nvidia-container-toolkit, runtime 'nvidia' and the device plugin are needed - see HELPDESK.md).")
    return "no-gpu", "The node has no NVIDIA GPU that Kubernetes can use (nvidia.com/gpu is missing)."


# --- summary ----------------------------------------------------------------------------------
@dataclass
class GpuSummary:
    n: int
    max_temp: Optional[int]
    avg_util: Optional[float]
    avg_power: Optional[float]
    max_power: Optional[float]
    clock_min: Optional[int]
    clock_max: Optional[int]
    throttle_pct: float            # share of load-phase readings with a bad throttle reason
    thermal_pct: float
    reasons: list[str]
    verdict: str                   # "ok" | "warn"
    findings: list[str]


def summarize(samples: list, warn_temp: int = GPU_WARN_TEMP) -> Optional[GpuSummary]:
    test = [s for s in samples if s.phase == "test"
            and (getattr(s, "gpu_temp", None) is not None or getattr(s, "gpu_sm_mhz", None) is not None)]
    if not test:
        return None
    temps = [s.gpu_temp for s in test if s.gpu_temp is not None]
    util = [s.gpu_util_pct for s in test if s.gpu_util_pct is not None]
    power = [s.gpu_power_w for s in test if s.gpu_power_w is not None]
    # clock under load: ignore idle readings at the start (util < 50 %)
    loaded = [s for s in test if (s.gpu_util_pct or 0) >= 50 and s.gpu_sm_mhz is not None] or \
             [s for s in test if s.gpu_sm_mhz is not None]
    clocks = [s.gpu_sm_mhz for s in loaded]
    masks = [s.gpu_throttle or 0 for s in test if s.gpu_throttle is not None]
    bad = sum(1 for m in masks if m & BAD_THROTTLE)
    thermal = sum(1 for m in masks if m & THERMAL_THROTTLE)
    reasons: list[str] = []
    for m in masks:
        for name in throttle_names(m, only_bad=True):
            if name not in reasons:
                reasons.append(name)
    n = len(masks) or 1
    busy = [u for u in util if u >= 50]          # readings while the burn really ran (not the start / the final check)
    util_avg = sum(busy) / len(busy) if busy else (sum(util) / len(util) if util else None)
    out = GpuSummary(len(test), max(temps) if temps else None, util_avg,
                     sum(power) / len(power) if power else None, max(power) if power else None,
                     min(clocks) if clocks else None, max(clocks) if clocks else None,
                     100 * bad / n, 100 * thermal / n, reasons, "ok", [])
    if out.max_temp is not None and out.max_temp >= warn_temp:
        out.findings.append(f"GPU reached {out.max_temp} °C (warning from {warn_temp} °C) - check the cooling.")
    if out.thermal_pct >= 10:
        out.findings.append(f"Thermal throttling in {out.thermal_pct:.0f} % of the readings ({', '.join(out.reasons)}).")
    elif out.throttle_pct >= 50:
        out.findings.append(f"Clock limited in {out.throttle_pct:.0f} % of the readings by: {', '.join(out.reasons)}.")
    if out.avg_util is not None and out.avg_util < 50:
        out.findings.append(f"Average GPU utilization only {out.avg_util:.0f} % - the load did not run properly?")
    if out.findings:
        out.verdict = "warn"
    return out


def summary_lines(samples: list, warn_temp: int = GPU_WARN_TEMP) -> list[str]:
    s = summarize(samples, warn_temp)
    if s is None:
        return ["GPU: no readings (nvidia-smi gave no data)."]
    power = (f"{s.avg_power:.0f} W avg / {s.max_power:.0f} W max" if s.avg_power is not None
             else "N/A (this card does not report power)")
    clock = f"{s.clock_min}-{s.clock_max} MHz" if s.clock_min is not None else "?"
    lines = [f"GPU temperature:          max {_f(s.max_temp, '{} °C')}",
             f"GPU utilization:          avg {_f(s.avg_util, '{:.0f} %')}",
             f"GPU power:                {power}",
             f"GPU SM clock (loaded):    {clock}",
             f"GPU throttling:           {s.throttle_pct:.0f} % of readings" + (f" ({', '.join(s.reasons)})" if s.reasons else "")]
    cool = [x.gpu_temp for x in samples if x.phase == "cooldown" and getattr(x, "gpu_temp", None) is not None]
    if cool and s.max_temp is not None:
        lines.append(f"GPU cooldown:             {s.max_temp} °C → {cool[-1]} °C ({len(cool)} readings)")
    lines += [f"  ⚠️  {f}" for f in s.findings] or ["  ✅ GPU: nothing suspicious."]
    return lines
