"""Data models and constants (no kubectl logic, just plain data)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Optional

# --- limits and defaults -------------------------------------------
DEFAULT_MAX_TEMP = 85      # °C at which the test stops
MIN_MAX_TEMP = 60
MAX_MAX_TEMP = 95
WARN_TEMP = 80             # °C from which a warning is shown in the output
MASTER_CPU_CAP = 70        # % of CPU load allowed on the master
MAX_NODE_BUSY_PCT = 80     # % of node CPU/RAM usage (kubectl top) before the test, above which it is refused
MAX_RAM_PCT = 95
COOLDOWN_DEFAULT = 60      # s of measuring after the load ends (cooldown)
COOLDOWN_MAX = 600

MASTER_MAX_TEMP = 80             # temperature limit for the master (lower than the default 85 °C)

PROFILE_CLASSIC = "classic"      # one load for the whole time
PROFILE_STEPPED = "stepped"      # gradually 25 -> 50 -> 75 -> 100 %
STEPS_DEFAULT = (25, 50, 75, 100)
STEP_TIME_DEFAULT = 180          # s, one stage (3 min, 12 min in total)
MIN_STEP_TIME = 30
MAX_STEP_TIME = 3600

# --- pods: every run has its own names with a random suffix so that concurrent
# --- tests do not delete each other (before, all had the same name "stress-test") ---
STRESS_POD = "stress-test"      # name prefixes
PROBE_POD = "temp-probe"
HW_POD = "hw-info"
TOOL_LABEL = "stress-test-tool"  # label app=..., used to find the pods


@dataclass(frozen=True)
class PodNames:
    """Pod names of one run (e.g. stress-test-a1b2c3)."""

    run_id: str
    stress: str
    probe: str
    hw: str

    @classmethod
    def new(cls, run_id: Optional[str] = None) -> "PodNames":
        rid = run_id or secrets.token_hex(3)
        return cls(rid, f"{STRESS_POD}-{rid}", f"{PROBE_POD}-{rid}", f"{HW_POD}-{rid}")

    @property
    def all(self) -> tuple[str, str, str]:
        return (self.stress, self.probe, self.hw)


@dataclass(frozen=True)
class NodeWorkload:
    """What is currently running on the node (without this tool's pods and finished ones)."""

    user_pods: tuple[tuple[str, str], ...] = ()   # (namespace, name), excluding kube-system
    system_count: int = 0                         # number of pods in kube-system


@dataclass(frozen=True)
class ToolPod:
    """A pod created by this tool (also by another, concurrent run)."""

    name: str
    node: str
    phase: str
    run_id: str


@dataclass(frozen=True)
class NodeInfo:
    """Basic information about a node from `kubectl get node -o json`."""

    name: str
    ready: bool
    is_control_plane: bool
    allocatable_mem_mib: int
    capacity_cpu: str = "?"
    os_image: str = "?"
    kernel: str = "?"
    architecture: str = "?"
    runtime: str = "?"


@dataclass
class StressConfig:
    """Settings of one test."""

    node: str
    duration: int = 60
    max_temp: int = DEFAULT_MAX_TEMP
    cpu_load: int = 100
    ram_pct: Optional[int] = None   # None = RAM is not stressed
    hdd: bool = False
    log: bool = False
    notes: str = ""
    background: bool = False        # the test runs detached from the terminal (always with a log)
    cooldown: int = COOLDOWN_DEFAULT  # how many s after the test to keep measuring temperature and clock (0 = no)
    profile: str = PROFILE_CLASSIC    # "classic" or "stepped" (stages)
    steps: tuple[int, ...] = STEPS_DEFAULT    # CPU load stages in % (stepped test only)
    step_time: int = STEP_TIME_DEFAULT        # length of one stage in s (stepped test only)

    @property
    def stepped(self) -> bool:
        return self.profile == PROFILE_STEPPED

    def validate(self) -> None:
        """Raises ValueError if something is outside the allowed range."""
        if self.duration < 1:
            raise ValueError("The test duration must be at least 1 second.")
        if not MIN_MAX_TEMP <= self.max_temp <= MAX_MAX_TEMP:
            raise ValueError(
                f"The temperature limit must be {MIN_MAX_TEMP}–{MAX_MAX_TEMP} °C."
            )
        if not 1 <= self.cpu_load <= 100:
            raise ValueError("CPU load must be 1–100 %.")
        if self.ram_pct is not None and not 1 <= self.ram_pct <= MAX_RAM_PCT:
            raise ValueError(f"RAM load must be 1–{MAX_RAM_PCT} %.")
        if not 0 <= self.cooldown <= COOLDOWN_MAX:
            raise ValueError(f"The cooldown time must be 0–{COOLDOWN_MAX} s.")
        if self.profile not in (PROFILE_CLASSIC, PROFILE_STEPPED):
            raise ValueError("The test profile must be classic or stepped.")
        if self.stepped:
            if (not self.steps or any(not 1 <= s <= 100 for s in self.steps)
                    or list(self.steps) != sorted(set(self.steps))):
                raise ValueError("Stages must be ascending numbers 1–100 %, e.g. 25,50,75,100.")
            if not MIN_STEP_TIME <= self.step_time <= MAX_STEP_TIME:
                raise ValueError(f"The stage time must be {MIN_STEP_TIME}–{MAX_STEP_TIME} s.")
            if self.ram_pct is not None or self.hdd:
                raise ValueError("The stepped test loads only the CPU (no RAM and disk).")
            if self.duration != len(self.steps) * self.step_time:
                raise ValueError("The total time of the stepped test must be the number of stages × the stage time.")

    def apply_master_limits(self) -> list[str]:
        """Limits the test for the master; returns a list of messages about what changed."""
        messages: list[str] = []
        if self.stepped:
            capped = tuple(sorted({min(s, MASTER_CPU_CAP) for s in self.steps}))
            if capped != tuple(self.steps):
                messages.append(
                    f"Master: stages limited to at most {MASTER_CPU_CAP} %: "
                    f"{'/'.join(str(s) for s in capped)}.")
                self.steps = capped
                self.duration = len(capped) * self.step_time
        if not self.stepped and self.cpu_load > MASTER_CPU_CAP:
            messages.append(
                f"Master: CPU load limited from {self.cpu_load} % to {MASTER_CPU_CAP} %."
            )
            self.cpu_load = MASTER_CPU_CAP
        if self.ram_pct is not None:
            messages.append("Master: RAM test turned off.")
            self.ram_pct = None
        if self.hdd:
            messages.append("Master: disk test turned off.")
            self.hdd = False
        if self.max_temp > MASTER_MAX_TEMP:
            messages.append(f"Master: temperature limit lowered from {self.max_temp} °C to "
                            f"{MASTER_MAX_TEMP} °C.")
            self.max_temp = MASTER_MAX_TEMP
        return messages


@dataclass(frozen=True)
class TopMetrics:
    """One line of `kubectl top node --no-headers`."""

    cpu: str
    cpu_pct: str
    mem: str
    mem_pct: str
    mem_mib: int


@dataclass
class ProbeData:
    """Temperatures and clock read by the probe from the host."""

    temps: dict[str, int] = field(default_factory=dict)   # e.g. {"CPU": 38}
    freq_mhz: Optional[int] = None
    mem_available_mib: Optional[int] = None               # host MemAvailable
    mem_total_mib: Optional[int] = None                   # host MemTotal
    cpu_total: Optional[int] = None                       # counter from /proc/stat (sum)
    cpu_idle: Optional[int] = None                        # counter from /proc/stat (idle + iowait)

    @property
    def cpu_temp(self) -> Optional[int]:
        return self.temps.get("CPU")

    @property
    def cpu_stat(self) -> Optional[tuple[int, int]]:
        """(total, idle) CPU counters; their difference between readings gives the utilisation."""
        if self.cpu_total is None or self.cpu_idle is None:
            return None
        return (self.cpu_total, self.cpu_idle)

    @property
    def mem_used_mib(self) -> Optional[int]:
        if self.mem_total_mib is None or self.mem_available_mib is None:
            return None
        return max(self.mem_total_mib - self.mem_available_mib, 0)

    @property
    def mem_used_pct(self) -> Optional[float]:
        if not self.mem_total_mib or self.mem_used_mib is None:
            return None
        return self.mem_used_mib / self.mem_total_mib * 100


@dataclass(frozen=True)
class Sample:
    """One probe reading (for the summary at the end of the test)."""

    t: float                          # s since the start of measuring
    phase: str                        # "test" or "cooldown"
    cpu_temp: Optional[int] = None
    freq_mhz: Optional[int] = None
    cpu_pct: Optional[float] = None
    mem_used_mib: Optional[int] = None
    mem_used_pct: Optional[float] = None
    stage: int = 0                    # stage number of the stepped test (0 = classic test)
