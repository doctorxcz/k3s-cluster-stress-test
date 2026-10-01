"""Pod definitions as Python dictionaries (no manual assembling of JSON strings)."""
from __future__ import annotations

from typing import Optional, Sequence

from .disk import build_disk_command
from .gpu import build_gpu_command
from .net import NET_SERVER_SCRIPT, build_net_command
from .models import SPIKE_LOW_PCT, TOOL_LABEL, PodNames, StressConfig

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
READY_MARKER = "STRESS-NG READY"          # synchronised start: the tool is installed, the pod waits for /tmp/go
GATE_FILE = "/tmp/go"

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
# CPU package power (Intel RAPL, needs no privileged mode: the probe runs as root): energy counter
# in µJ (power = its growth over time) and the PL1/PL2 limits; sums all packages, subzones are skipped
e=0; m=0; pl1=0; pl2=0; found=0
for z in /host-sys/class/powercap/intel-rapl:*; do
    case "${z##*/}" in intel-rapl:*:*) continue ;; esac
    v=$(cat "$z/energy_uj" 2>/dev/null) || continue
    [ -n "$v" ] || continue
    found=1; e=$((e+v)); m=$((m+$(cat "$z/max_energy_range_uj" 2>/dev/null || echo 0)))
    pl1=$((pl1+$(cat "$z/constraint_0_power_limit_uw" 2>/dev/null || echo 0)))
    pl2=$((pl2+$(cat "$z/constraint_1_power_limit_uw" 2>/dev/null || echo 0)))
done
if [ "$found" = 1 ]; then echo "rapl_uj $e $m $pl1 $pl2"; fi
# latency to the watched node (--net-watch): one ping per reading, PING_TARGET is set by the caller
if [ -n "$PING_TARGET" ]; then
    r=$(ping -c 1 -W 1 "$PING_TARGET" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\) ms.*/\1/p')
    if [ -n "$r" ]; then echo "ping_ms $r"; else echo "ping_ms lost"; fi
fi
# host memory (inside a container /proc/meminfo shows the values of the whole node)
awk '/^MemTotal:/ {print "mem_total_kb " $2} /^MemAvailable:/ {print "mem_available_kb " $2}' /proc/meminfo 2>/dev/null
exit 0
"""

# Detects node hardware (CPU, RAM modules...). Runs in a privileged pod.
HW_SCRIPT = r"""
echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo "Threads: $(nproc)"
echo "Cores: $(lscpu -p=CORE,SOCKET 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
echo "System: $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null) $(cat /sys/class/dmi/id/product_name 2>/dev/null)"
echo "Motherboard: $(cat /sys/class/dmi/id/board_name 2>/dev/null)"
echo "RAM total: $(free -m | awk '/Mem:/{print $2}') MB"

apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq dmidecode pciutils >/dev/null 2>&1
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
gpu=$(lspci 2>/dev/null | grep -iE 'vga compatible|3d controller|display controller' | sed 's/^[^ ]* [^:]*: //' | paste -sd ';' - | sed 's/;/; /g')
echo "GPU: ${gpu:-none detected (or pciutils could not be installed)}"
lsblk -dn -o NAME,SIZE,ROTA,MODEL 2>/dev/null | awk '$1 !~ /^(loop|ram|zram|sr)/ {kind=($3=="1")?"HDD":"SSD/NVMe"; m=""; for(i=4;i<=NF;i++) m=m" "$i; printf "Disk %s: %s | %s |%s\n", $1, $2, kind, m}'
"""


# Disk health: SMART data of all disks as JSON between SMART-BEGIN/SMART-END markers (privileged pod).
SMART_SCRIPT = r"""
export DEBIAN_FRONTEND=noninteractive
if ! { apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq smartmontools >/dev/null 2>&1; }; then
    echo "SMART-UNAVAILABLE apt"; exit 0
fi
for dev in $(smartctl --scan 2>/dev/null | awk '{print $1}'); do
    echo "SMART-BEGIN $dev"
    smartctl -j -H -A -i "$dev" 2>/dev/null
    echo "SMART-END"
done
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


def build_spike_command(target_pct: int, cycles: int, low_time: int, high_time: int,
                        low_pct: int = SPIKE_LOW_PCT) -> str:
    """Spike test: repeating low/high cycles (e.g. 10 % <-> target %), on and off.

    One shell loop, `cycles` repetitions of two back-to-back stress-ng runs (low phase,
    then an abrupt jump to the high phase) - no restart delay beyond the ~1 s it takes a
    new process to start. Same marker format as the stepped test
    (`STRESS-STAGE i/n P%`, n = cycles * 2), so the tool's stage parsing works unchanged.
    """
    total = cycles * 2
    return (f'i=0; c=1; while [ $c -le {cycles} ]; do '
            f'i=$((i+1)); echo "STRESS-STAGE $i/{total} {low_pct}%"; '
            f"stress-ng --cpu 0 --cpu-method matrixprod --cpu-load {low_pct} "
            f"--timeout {low_time}s --metrics-brief || break; "
            f'i=$((i+1)); echo "STRESS-STAGE $i/{total} {target_pct}%"; '
            f"stress-ng --cpu 0 --cpu-method matrixprod --cpu-load {target_pct} "
            f"--timeout {high_time}s --metrics-brief || break; "
            f"c=$((c+1)); done")


def build_stress_command(cfg: StressConfig, ram_target_mib: Optional[int],
                         net_target: str = "", net_port: int = 5201, net_service_ip: str = "") -> str:
    """Builds the stress-ng command from the test settings."""
    if cfg.stepped:
        return build_stepped_command(cfg.steps, cfg.step_time)
    if cfg.net:
        return build_net_command(net_target, net_port, cfg.net_mode, cfg.net_time, cfg.net_rate,
                                 cfg.net_extra, net_service_ip)
    if cfg.disk:
        return build_disk_command(cfg.disk_size, cfg.disk_job_time, cfg.disk_read_only)
    if cfg.gpu:
        return build_gpu_command(cfg.duration, cfg.gpu_mem_pct, cfg.gpu_double)
    if cfg.spike:
        return build_spike_command(cfg.spike_target, cfg.spike_cycles, cfg.spike_low_time,
                                   cfg.spike_high_time)
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


def smart_pod(node: str, names: PodNames, deadline: int = 300) -> dict:
    """Pod reading SMART data. Always privileged (smartctl needs raw access to the disks) and read-only."""
    pod = _base_pod(names.hw, node, deadline, names.run_id, "hw")
    pod["spec"]["containers"] = [{
        "name": "smart",
        "image": IMAGE_UBUNTU,
        "securityContext": {"privileged": True},
        "command": ["bash", "-c", SMART_SCRIPT],
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{HW_LIMIT_MIB}Mi"}},
    }]
    return pod


def gpu_scan_pod(node: str, names: PodNames, deadline: int = 240) -> dict:
    """Read-only pod listing the graphics cards of a node (lspci). Unprivileged, no hostPath."""
    from .gpuscan import SCAN_SCRIPT
    pod = _base_pod(names.hw, node, deadline, names.run_id, "hw")
    pod["spec"]["containers"] = [{
        "name": "gpu-scan",
        "image": IMAGE_UBUNTU,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["bash", "-c", SCAN_SCRIPT],
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{HW_LIMIT_MIB}Mi"}},
    }]
    return pod


def image_pull_pod(node: str, names: PodNames, image: str, deadline: int = 1800) -> dict:
    """Pod that only pulls `image` on the node (it starts, sleeps a second and ends) - warms the image cache."""
    pod = _base_pod(names.hw, node, deadline, names.run_id, "hw")
    pod["spec"]["containers"] = [{
        "name": "image-pull",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["sleep", "1"],
        "resources": {"requests": {"memory": "16Mi"}, "limits": {"memory": "64Mi"}},
    }]
    return pod


def net_server_pod(node: str, names: PodNames, deadline: int, port: int, host_network: bool) -> dict:
    """iperf3 server of the network test on the peer node (reuses the name of the hw pod, which is finished by then)."""
    pod = _base_pod(names.hw, node, deadline, names.run_id, "net-server")
    pod["spec"]["containers"] = [{
        "name": "net-server",
        "image": IMAGE_UBUNTU,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["bash", "-c", NET_SERVER_SCRIPT.replace("PORT", str(port))],
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{HW_LIMIT_MIB}Mi"}},
    }]
    if host_network:
        pod["spec"]["hostNetwork"] = True
        pod["spec"]["dnsPolicy"] = "ClusterFirstWithHostNet"
    return pod


def net_service(name: str, run_id: str, port: int) -> dict:
    """ClusterIP Service in front of the iperf3 server pod (network test extra `service`: the kube-proxy path)."""
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "labels": {"app": TOOL_LABEL, "run-id": run_id}},
            "spec": {"selector": {"run-id": run_id, "role": "net-server"},
                     "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}]}}


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
               memory_limit_mib: int = STRESS_BASE_LIMIT_MIB, package: str = "stress-ng",
               scratch_mib: int = 0, host_network: bool = False, gate_seconds: int = 0,
               image: str = "", gpu: bool = False, setup: str = "") -> dict:
    # gate_seconds > 0: after the installation the pod says READY and waits (at most that long) until
    # the file /tmp/go appears, so that all the pods of a parallel test can start the load together
    gate = (f"echo '{READY_MARKER}'; while [ ! -f {GATE_FILE} ] && [ $SECONDS -lt {gate_seconds} ]; do sleep 0.2; done; "
            f"if [ ! -f {GATE_FILE} ]; then echo '{FAILED_MARKER}'; exit 1; fi; ") if gate_seconds > 0 else ""
    script = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "if apt-get update -qq >/dev/null 2>&1 && "
        f"apt-get install -y -qq {package} >/dev/null 2>&1{f' && {setup}' if setup else ''}; then "
        f"{gate}echo '{STARTED_MARKER}'; {stress_cmd}; "
        f"else echo '{FAILED_MARKER}'; fi"
    )
    pod = _base_pod(names.stress, node, deadline, names.run_id, "stress")
    pod["spec"]["containers"] = [{
        "name": "stress-test",
        "image": image or IMAGE_UBUNTU,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["/bin/bash", "-c", script],
        # small request + fixed limit: when memory runs short the kernel kills this pod first
        # (it uses orders of magnitude more than it "ordered"), not foreign services
        "resources": {"requests": {"memory": "64Mi"},
                      "limits": {"memory": f"{memory_limit_mib}Mi"}},
    }]
    if gpu:               # GPU test: the NVIDIA runtime + one GPU from the device plugin (no privileged, no hostPath)
        pod["spec"]["runtimeClassName"] = "nvidia"
        container = pod["spec"]["containers"][0]
        container["resources"]["limits"]["nvidia.com/gpu"] = 1
        container["env"] = [{"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "utility,compute"}]
    if host_network:      # network test: the pod uses the node's own network
        pod["spec"]["hostNetwork"] = True
        pod["spec"]["dnsPolicy"] = "ClusterFirstWithHostNet"
    if scratch_mib:       # disk benchmark: an emptyDir = a directory on the node's own disk, removed with the pod
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "bench", "mountPath": "/bench"}]
        pod["spec"]["volumes"] = [{"name": "bench", "emptyDir": {"sizeLimit": f"{scratch_mib}Mi"}}]
    return pod
