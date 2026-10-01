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
PROFILE_NET = "net"            # network test (iperf3 + ping) between the node and a peer
PROFILE_DISK = "disk"          # fio disk benchmark (IOPS, MB/s, latency), no CPU stress
PROFILE_GPU = "gpu"            # NVIDIA GPU burn (gpu-burn) + nvidia-smi readings, no CPU stress
PROFILE_SPIKE = "spike"          # repeating jump: low % <-> target %, on and off, for the whole time
STEPS_DEFAULT = (25, 50, 75, 100)
STEP_TIME_DEFAULT = 180          # s, one stage (3 min, 12 min in total)
MIN_STEP_TIME = 30
MAX_STEP_TIME = 3600

SPIKE_TARGETS = (25, 50, 75, 100)   # 1/4, 2/4, 3/4, 4/4 - menu choices for the high phase
SPIKE_LOW_PCT = 10                  # % CPU load of the low phase of each cycle
SPIKE_PHASE_TIME_DEFAULT = 5        # s, default length of one phase (low or high)
MIN_SPIKE_PHASE_TIME = 1
MAX_SPIKE_PHASE_TIME = 300

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
    internal_ip: str = ""
    gpu_count: int = 0                # allocatable nvidia.com/gpu


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
    export: str = "json"              # machine-readable export next to the log: json, csv, both or none
    net_watch: str = ""               # ping a node during ANY test to see what the load does to the network ("auto" or a node)
    net_extra: tuple = ()             # network test: optional jobs (dns, internet, mtr, service)
    net_peer: str = ""                # network test: the other node ("" = chosen automatically)
    net_time: int = 10                # network test: seconds of one iperf3 test
    net_mode: str = "host"            # network test: "host" (nodes' network) or "pod" (pod network)
    net_rate: int = 0                 # network test: Mbit/s cap for TCP tests (0 = unlimited)
    disk_size: int = 1024             # disk benchmark: MiB of the test file
    disk_job_time: int = 15           # disk benchmark: s of one fio job
    disk_read_only: bool = False      # disk benchmark: read jobs only (set for the master)
    smart: bool = False               # preflight: read disk health (SMART) with a privileged pod
    allow_bad_disk: bool = False      # continue even if a disk reports a failed SMART health check
    gpu_max_temp: int = 80            # GPU test: GPU temperature that stops the test
    gpu_mem_pct: int = 90             # GPU test: % of the GPU memory gpu-burn uses
    gpu_double: bool = False          # GPU test: double precision (much slower on consumer cards)
    gpu_image: str = ""               # GPU test: image of the load pod ("" = default CUDA devel image)
    baseline_check: bool = True       # compare the finished test with the node's saved baseline
    profile: str = PROFILE_CLASSIC    # "classic", "stepped" (stages) or "spike" (jump)
    steps: tuple[int, ...] = STEPS_DEFAULT    # CPU load stages in % (stepped test only)
    step_time: int = STEP_TIME_DEFAULT        # length of one stage in s (stepped test only)
    spike_target: Optional[int] = None                    # high-phase target % (spike test only)
    spike_low_time: int = SPIKE_PHASE_TIME_DEFAULT         # s of the low phase, each cycle (spike only)
    spike_high_time: int = SPIKE_PHASE_TIME_DEFAULT        # s of the high phase, each cycle (spike only)
    spike_cycles: int = 1                                  # how many low/high cycles (spike only;
                                                            # duration must equal cycles * (low+high))

    @property
    def stepped(self) -> bool:
        return self.profile == PROFILE_STEPPED

    @property
    def spike(self) -> bool:
        return self.profile == PROFILE_SPIKE

    @property
    def net(self) -> bool:
        return self.profile == PROFILE_NET

    @property
    def disk(self) -> bool:
        return self.profile == PROFILE_DISK

    @property
    def gpu(self) -> bool:
        return self.profile == PROFILE_GPU

    @property
    def multi_stage(self) -> bool:
        """True for any profile made of several stress-ng stages (stepped or spike)."""
        return self.stepped or self.spike

    @property
    def total_duration(self) -> int:
        """Total time the load pod actually runs stress-ng for (all stages together)."""
        return self.duration

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
        if self.export not in ("json", "csv", "both", "none"):
            raise ValueError("The export must be json, csv, both or none.")
        if self.profile not in (PROFILE_CLASSIC, PROFILE_STEPPED, PROFILE_SPIKE, PROFILE_DISK,
                                PROFILE_NET, PROFILE_GPU):
            raise ValueError("The test profile must be classic, stepped, spike, disk, net or gpu.")
        if self.gpu:
            from .gpu import MAX_GPU_TIME, MIN_GPU_TIME, valid_image
            if not MIN_GPU_TIME <= self.duration <= MAX_GPU_TIME:
                raise ValueError(f"The GPU test time must be {MIN_GPU_TIME}–{MAX_GPU_TIME} s.")
            if not 50 <= self.gpu_max_temp <= 95:
                raise ValueError("The GPU temperature limit must be 50–95 °C.")
            if not 10 <= self.gpu_mem_pct <= 95:
                raise ValueError("The GPU memory share must be 10–95 %.")
            if self.ram_pct is not None or self.hdd:
                raise ValueError("The GPU test does not combine with the RAM and disk load.")
            if self.gpu_image and not valid_image(self.gpu_image):
                raise ValueError("The GPU image name contains characters that are not allowed.")
        if self.net:
            from .net import MAX_NET_TIME, MIN_NET_TIME, NET_MODES, net_duration
            if not MIN_NET_TIME <= self.net_time <= MAX_NET_TIME:
                raise ValueError(f"The network test time must be {MIN_NET_TIME}–{MAX_NET_TIME} s.")
            if self.net_mode not in NET_MODES:
                raise ValueError("The network mode must be host or pod.")
            if self.net_rate < 0:
                raise ValueError("The network rate cap cannot be negative.")
            if self.ram_pct is not None or self.hdd:
                raise ValueError("The network test does not combine with the RAM and disk load.")
            try:
                expected = net_duration(self.net_time, self.net_extra, self.net_mode)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            if self.duration != expected:
                raise ValueError("The total time of the network test must match its jobs.")
        if self.disk:
            from .disk import MAX_DISK_JOB_TIME, MAX_DISK_SIZE, MIN_DISK_JOB_TIME, MIN_DISK_SIZE, jobs
            if not MIN_DISK_SIZE <= self.disk_size <= MAX_DISK_SIZE:
                raise ValueError(f"The disk benchmark file must be {MIN_DISK_SIZE}–{MAX_DISK_SIZE} MiB.")
            if not MIN_DISK_JOB_TIME <= self.disk_job_time <= MAX_DISK_JOB_TIME:
                raise ValueError(f"The disk benchmark job time must be {MIN_DISK_JOB_TIME}–{MAX_DISK_JOB_TIME} s.")
            if self.ram_pct is not None or self.hdd:
                raise ValueError("The disk benchmark does not combine with the RAM and stress-ng disk test.")
            if self.duration != len(jobs(self.disk_read_only)) * self.disk_job_time:
                raise ValueError("The total time of the disk benchmark must be the number of jobs × the job time.")
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
        if self.spike:
            # SPIKE_TARGETS + MASTER_CPU_CAP: apply_master_limits() may cap the target to
            # MASTER_CPU_CAP (70 %), which is not itself one of the menu choices.
            if self.spike_target not in SPIKE_TARGETS and self.spike_target != MASTER_CPU_CAP:
                raise ValueError(
                    f"The spike target must be one of {'/'.join(str(s) for s in SPIKE_TARGETS)} %.")
            if not MIN_SPIKE_PHASE_TIME <= self.spike_low_time <= MAX_SPIKE_PHASE_TIME:
                raise ValueError(
                    f"The spike low-phase time must be {MIN_SPIKE_PHASE_TIME}–"
                    f"{MAX_SPIKE_PHASE_TIME} s.")
            if not MIN_SPIKE_PHASE_TIME <= self.spike_high_time <= MAX_SPIKE_PHASE_TIME:
                raise ValueError(
                    f"The spike high-phase time must be {MIN_SPIKE_PHASE_TIME}–"
                    f"{MAX_SPIKE_PHASE_TIME} s.")
            if self.spike_cycles < 1:
                raise ValueError("The spike test must run at least one cycle.")
            if self.duration != self.spike_cycles * (self.spike_low_time + self.spike_high_time):
                raise ValueError(
                    "The total time of the spike test must be cycles × (low time + high time).")
            if self.ram_pct is not None or self.hdd:
                raise ValueError("The spike test loads only the CPU (no RAM and disk).")

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
        if self.spike and self.spike_target is not None and self.spike_target > MASTER_CPU_CAP:
            messages.append(
                f"Master: spike target limited from {self.spike_target} % to {MASTER_CPU_CAP} %."
            )
            self.spike_target = MASTER_CPU_CAP
        if not self.stepped and not self.spike and not self.disk and not self.net and not self.gpu and self.cpu_load > MASTER_CPU_CAP:
            messages.append(
                f"Master: CPU load limited from {self.cpu_load} % to {MASTER_CPU_CAP} %."
            )
            self.cpu_load = MASTER_CPU_CAP
        if self.net:
            from .net import NET_RATE_CAP_MASTER
            if not self.net_rate or self.net_rate > NET_RATE_CAP_MASTER:
                messages.append(f"Master: network test limited to {NET_RATE_CAP_MASTER} Mbit/s "
                                f"(the API must not lose its network).")
                self.net_rate = NET_RATE_CAP_MASTER
        if self.disk:
            from .disk import MASTER_DISK_SIZE, jobs
            if not self.disk_read_only or self.disk_size > MASTER_DISK_SIZE:
                messages.append(f"Master: disk benchmark limited to read jobs and a {MASTER_DISK_SIZE} MiB file "
                                f"(no heavy writes next to the API/etcd).")
                self.disk_read_only = True
                self.disk_size = min(self.disk_size, MASTER_DISK_SIZE)
                self.duration = len(jobs(True)) * self.disk_job_time
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
    energy_uj: Optional[int] = None                       # RAPL package energy counter (sum of packages), µJ
    energy_max_uj: Optional[int] = None                   # value at which the RAPL counter wraps around
    pl1_w: Optional[float] = None                         # RAPL long-term power limit (PL1), W
    ping_ms: Optional[float] = None                       # --net-watch: reply time of one ping, ms
    ping_lost: bool = False                               # --net-watch: the ping got no reply
    pl2_w: Optional[float] = None                         # RAPL short-term power limit (PL2), W


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
    power_w: Optional[float] = None   # CPU package power from RAPL (average since the previous reading)
    ping_ms: Optional[float] = None   # latency to the watched node (--net-watch), None = not measured / lost
    ping_lost: bool = False           # the ping to the watched node got no reply
    gpu_temp: Optional[int] = None    # GPU test (nvidia-smi); all gpu_* stay None without a GPU
    gpu_power_w: Optional[float] = None
    gpu_sm_mhz: Optional[int] = None
    gpu_util_pct: Optional[int] = None
    gpu_mem_mib: Optional[int] = None
    gpu_throttle: Optional[int] = None
    gpu_fan_pct: Optional[int] = None # GPU fan speed in % (nvidia-smi gives no RPM)
