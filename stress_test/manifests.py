"""Pod definitions as Python dictionaries (no manual assembling of JSON strings)."""
from __future__ import annotations

from typing import Optional, Sequence

from .models import TOOL_LABEL, PodNames, StressConfig

# image versions pinned firmly (no "latest", behaviour does not change by itself)
IMAGE_UBUNTU = "ubuntu:24.04"
IMAGE_BUSYBOX = "busybox:1.36"

# pod memory limits (the load pod may use the RAM test target + overhead, no more)
STRESS_BASE_LIMIT_MIB = 1536       # test without RAM load (apt, stress-ng)
STRESS_OVERHEAD_MIB = 1024         # extra overhead on top of the RAM test target
PROBE_LIMIT_MIB = 128
HW_LIMIT_MIB = 768

STARTED_MARKER = "STRESS-NG STARTED"
FAILED_MARKER = "STRESS-NG FAILED"

# Reads temperatures (hwmon) and the average CPU clock. Output: `temp ...` and `freq ...` lines.
PROBE_SCRIPT = r"""
for d in /host-sys/class/hwmon/hwmon*; do
    n=$(cat "$d/name" 2>/dev/null)
    case "$n" in
        k10temp|coretemp|zenpower|amdgpu|radeon|nouveau|nvme|drivetemp)
            t=$(cat "$d/temp1_input" 2>/dev/null)
            [ -n "$t" ] && echo "temp $n $t"
            ;;
    esac
done
f=0; c=0
for x in /host-sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do
    v=$(cat "$x" 2>/dev/null)
    if [ -n "$v" ]; then f=$((f+v)); c=$((c+1)); fi
done
if [ "$c" -gt 0 ]; then echo "freq $((f/c/1000))"; fi
# CPU counters of the whole node (utilisation in % is computed from the difference of two readings)
awk '/^cpu / {t=0; for (i=2; i<=9; i++) t+=$i; print "cpu_stat " t " " ($5+$6)}' /proc/stat 2>/dev/null
# host memory (inside a container /proc/meminfo shows the values of the whole node)
awk '/^MemTotal:/ {print "mem_total_kb " $2} /^MemAvailable:/ {print "mem_available_kb " $2}' /proc/meminfo 2>/dev/null
exit 0
"""

# Detects node hardware (CPU, RAM modules...). Runs in a privileged pod.
HW_SCRIPT = r"""
echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo "Threads: $(nproc)"
echo "System: $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null) $(cat /sys/class/dmi/id/product_name 2>/dev/null)"
echo "Motherboard: $(cat /sys/class/dmi/id/board_name 2>/dev/null)"
echo "RAM total: $(free -m | awk '/Mem:/{print $2}') MB"

apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq dmidecode >/dev/null 2>&1
dmidecode -t memory 2>/dev/null | awk -F': ' -v priv="$HW_PRIVILEGED" '
    function flush() {
        if (inrec && size != "" && size !~ /No Module/) {
            n++
            printf "RAM module %d: %s | %s | %s | %s | %s %s\n", n, loc, size, type, speed, man, pn
        }
    }
    /^Memory Device/ { flush(); inrec=1; size=type=speed=loc=man=pn=""; next }
    /^\tSize:/          { size=$2 }
    /^\tType:/          { type=$2 }
    /^\tSpeed:/         { speed=$2 }
    /^\tLocator:/       { loc=$2 }
    /^\tManufacturer:/  { man=$2 }
    /^\tPart Number:/   { pn=$2 }
    END {
        flush()
        if (n > 0) printf "RAM module count: %d\n", n
        else if (priv == "1") print "RAM modules: cannot be determined (dmidecode returned no data)"
        else print "RAM modules: cannot be determined without privileged mode (run with --hw-privileged)"
    }'
"""


def stress_memory_limit_mib(ram_target_mib: Optional[int]) -> int:
    """Memory limit of the load pod: RAM test target + overhead, otherwise a fixed base."""
    if ram_target_mib:
        return ram_target_mib + STRESS_OVERHEAD_MIB
    return STRESS_BASE_LIMIT_MIB


def build_stepped_command(steps: Sequence[int], step_time: int) -> str:
    """Stepped test: one shell loop, the stages run one after another in one pod.

    Each stage is a separate run of stress-ng (`--cpu-load` = target in %). Before a stage
    a marker `STRESS-STAGE i/n P%` is printed, which tells the tool where it is.
    All stages use the same method (matrixprod) so that they are comparable.
    """
    count = len(steps)
    values = " ".join(str(s) for s in steps)
    return (f'i=0; for p in {values}; do i=$((i+1)); echo "STRESS-STAGE $i/{count} $p%"; '
            f"stress-ng --cpu 0 --cpu-method matrixprod --cpu-load $p "
            f"--timeout {step_time}s --metrics-brief || break; done")


def build_stress_command(cfg: StressConfig, ram_target_mib: Optional[int]) -> str:
    """Builds the stress-ng command from the test settings."""
    if cfg.stepped:
        return build_stepped_command(cfg.steps, cfg.step_time)
    if cfg.cpu_load == 100:
        args = ["--cpu 0 --cpu-method matrixprod --matrix 0"]
    else:
        args = [f"--cpu 0 --cpu-load {cfg.cpu_load}"]
    if ram_target_mib:
        args.append(f"--vm 1 --vm-bytes {ram_target_mib}M --vm-populate --vm-hang 0")
    if cfg.hdd:
        args.append("--hdd 0")
    return f"stress-ng {' '.join(args)} --timeout {cfg.duration}s --metrics-brief"


def _base_pod(name: str, node: str, deadline: int, run_id: str, role: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name,
                     "labels": {"app": TOOL_LABEL, "run-id": run_id, "role": role}},
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            # safety net: Kubernetes ends the pod by itself even if the tool disappeared
            "activeDeadlineSeconds": deadline,
            "tolerations": [{"operator": "Exists"}],
            "automountServiceAccountToken": False,   # pods do not need access to the API
            "containers": [],
            "volumes": [],
        },
    }


def hw_pod(node: str, names: PodNames, deadline: int = 300,
           privileged: bool = False) -> dict:
    """Pod for detecting hardware. Without privileged and without hostPath by default.

    /sys/class/dmi/id is readable even by an unprivileged container (CPU, system, board).
    RAM modules (dmidecode) usually need privileged mode, which is turned on
    only explicitly with --hw-privileged.
    """
    pod = _base_pod(names.hw, node, deadline, names.run_id, "hw")
    security = ({"privileged": True} if privileged
                else {"allowPrivilegeEscalation": False})
    pod["spec"]["containers"] = [{
        "name": "hw-info",
        "image": IMAGE_UBUNTU,
        "securityContext": security,
        "env": [{"name": "HW_PRIVILEGED", "value": "1" if privileged else "0"}],
        "command": ["bash", "-c", HW_SCRIPT],
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{HW_LIMIT_MIB}Mi"}},
    }]
    return pod


def probe_pod(node: str, names: PodNames, deadline: int) -> dict:
    pod = _base_pod(names.probe, node, deadline, names.run_id, "probe")
    pod["spec"]["containers"] = [{
        "name": "probe",
        "image": IMAGE_BUSYBOX,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["sleep", "infinity"],
        "resources": {"requests": {"memory": "16Mi"},
                      "limits": {"memory": f"{PROBE_LIMIT_MIB}Mi"}},
        "volumeMounts": [{"name": "sys", "mountPath": "/host-sys", "readOnly": True}],
    }]
    pod["spec"]["volumes"] = [{"name": "sys", "hostPath": {"path": "/sys"}}]
    return pod


def stress_pod(node: str, names: PodNames, deadline: int, stress_cmd: str,
               memory_limit_mib: int = STRESS_BASE_LIMIT_MIB) -> dict:
    script = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "if apt-get update -qq >/dev/null 2>&1 && "
        "apt-get install -y -qq stress-ng >/dev/null 2>&1; then "
        f"echo '{STARTED_MARKER}'; {stress_cmd}; "
        f"else echo '{FAILED_MARKER}'; fi"
    )
    pod = _base_pod(names.stress, node, deadline, names.run_id, "stress")
    pod["spec"]["containers"] = [{
        "name": "stress-test",
        "image": IMAGE_UBUNTU,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["/bin/bash", "-c", script],
        # small request + fixed limit: when memory runs short the kernel kills this pod first
        # (it uses orders of magnitude more than it "ordered"), not foreign services
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{memory_limit_mib}Mi"}},
    }]
    return pod
