"""Cluster DASHBOARD (menu key D, `--dashboard`): a live overview of every node in the retro GUI style, a small k9s.

Everything is READ-ONLY and puts no load on the nodes: the Kubernetes API (nodes, pods, events), one tiny unprivileged probe
pod per node (hostPath /sys read-only, only for as long as the dashboard is open) and, on a node with a GPU, a second probe pod
that only runs `nvidia-smi` (it does not take the GPU away from a GPU test).

Three widths (the screen is redrawn when the window is resized):
  1  up to 90 columns       a compact table (CPU, temperature, RAM, GPU, pods)
  2  91 - 160 columns       more columns + a detail block of the selected node under the table
  3  over 160 columns       the whole width: wide table with history graphs + panels (detail, pods, events, tests)
"""
from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import background, gpu as gpumod, schedule, ui
from . import graph
from .graph import GraphState
from .kube import Kubectl, KubectlError
from .manifests import PROBE_SCRIPT, dashboard_gpu_pod, dashboard_pod
from .models import PodNames, ProbeData
from .parsing import clean_text, cpu_percent, format_duration, node_from_json, parse_probe_output, power_watts

log = logging.getLogger(__name__)

TIER1_MAX, TIER2_MAX = 90, 160                  # columns: tier 1 up to 90, tier 2 up to 160, tier 3 above
INTERVALS = (0.5, 1, 2, 5, 10, 30)
HISTORY = 3600                                   # samples kept per node for the graphs (g): an hour at 1 s
K8S_EVERY, EVENTS_EVERY = 10.0, 30.0            # s: nodes + pods, events
POD_DEADLINE = 3600                              # the probe pods end by themselves after an hour even if the tool is killed

# What the probe adds to PROBE_SCRIPT: uptime, load, swap, CPU model / cores, thermal throttle counters, NICs, disks, peers.
EXTRA_SCRIPT = r"""
echo "uptime $(cut -d' ' -f1 /proc/uptime 2>/dev/null | cut -d. -f1)"
echo "load $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
awk '/^SwapTotal:/ {print "swap_total_kb " $2} /^SwapFree:/ {print "swap_free_kb " $2}' /proc/meminfo 2>/dev/null
echo "threads $(grep -c '^processor' /proc/cpuinfo 2>/dev/null)"
echo "cores $(awk '/^physical id/ {p=$4} /^core id/ {print p ":" $4}' /proc/cpuinfo 2>/dev/null | sort -u | wc -l)"
m=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ *//'); [ -n "$m" ] && echo "cpu_model $m"
t=0; for f in /host-sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/core_throttle_count; do
    v=$(cat "$f" 2>/dev/null); [ -n "$v" ] && t=$((t+v)); done; echo "thr $t"
for n in /host-sys/class/net/*; do
    [ -e "$n/device" ] || continue
    b=$(basename "$n")
    echo "nic $b $(cat $n/speed 2>/dev/null || echo 0) $(cat $n/operstate 2>/dev/null || echo ?) $(cat $n/statistics/rx_bytes 2>/dev/null || echo 0) $(cat $n/statistics/tx_bytes 2>/dev/null || echo 0) $(cat $n/mtu 2>/dev/null || echo 0) $(cat $n/duplex 2>/dev/null || echo ?) $(cat $n/statistics/rx_errors 2>/dev/null || echo 0) $(cat $n/statistics/tx_errors 2>/dev/null || echo 0) $(cat $n/statistics/rx_dropped 2>/dev/null || echo 0) $(cat $n/statistics/tx_dropped 2>/dev/null || echo 0)"
done
for f in sys_vendor product_name product_version bios_vendor bios_version bios_date board_name; do
    v=$(cat /host-sys/class/dmi/id/$f 2>/dev/null); [ -n "$v" ] && echo "dmi $f $v"
done
c=/host-sys/devices/system/cpu/cpu0/cpufreq
[ -d "$c" ] && echo "gov $(cat $c/scaling_governor 2>/dev/null || echo ?) $(( $(cat $c/scaling_min_freq 2>/dev/null || echo 0) / 1000 )) $(( $(cat $c/scaling_max_freq 2>/dev/null || echo 0) / 1000 )) $(( $(cat $c/cpuinfo_max_freq 2>/dev/null || echo 0) / 1000 ))"
[ -e /host-sys/devices/system/cpu/intel_pstate/no_turbo ] && echo "turbo $(cat /host-sys/devices/system/cpu/intel_pstate/no_turbo)"
echo "cpufreqs $(for x in /host-sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do [ -r $x ] && echo $(( $(cat $x) / 1000 )); done | tr '\n' ' ')"
for d in /host-sys/class/hwmon/hwmon*; do
    [ "$(cat $d/name 2>/dev/null)" = coretemp ] || continue
    for l in $d/temp*_label; do [ -e "$l" ] || continue; i=${l%_label}; echo "ctemp $(cat $l | tr ' ' _) $(cat ${i}_input 2>/dev/null)"; done
done
for z in /host-sys/class/thermal/thermal_zone*; do [ -e "$z/temp" ] && echo "tz $(cat $z/type 2>/dev/null | tr ' ' _) $(cat $z/temp 2>/dev/null)"; done
for f in cpu memory io; do
    [ -r /proc/pressure/$f ] && awk -v n=$f '/^some/ {split($2,a,"="); print "psi " n " some " a[2]} /^full/ {split($2,a,"="); print "psi " n " full " a[2]}' /proc/pressure/$f
done
awk '/^(MemFree|Buffers|Cached|Dirty|Slab|SReclaimable|AnonPages|Shmem|HugePages_Total):/ {gsub(":","",$1); print "mem " $1 " " $2}' /proc/meminfo 2>/dev/null
for d in /host-sys/block/*; do
    b=$(basename "$d"); case "$b" in loop*|ram*|zram*|sr*|dm-*) continue ;; esac
    [ -r $d/stat ] && echo "dio $b $(awk '{print $3, $7}' $d/stat)"
done
echo "files $(cut -f1 /proc/sys/fs/file-nr 2>/dev/null)"
echo "tasks $(cut -d' ' -f4 /proc/loadavg 2>/dev/null)"
for d in /host-sys/block/*; do
    b=$(basename "$d"); case "$b" in loop*|ram*|zram*|sr*|dm-*) continue ;; esac
    echo "disk $b $(( $(cat $d/size 2>/dev/null || echo 0) / 1953125 )) $(cat $d/queue/rotational 2>/dev/null || echo 0) $(cat $d/device/model 2>/dev/null | sed 's/ *$//' || true)"
done
for p in $PEERS; do
    n=${p%%=*}; ip=${p#*=}
    r=$(ping -c 1 -W 1 "$ip" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\) ms.*/\1/p')
    echo "peer $n ${r:-lost}"
done
exit 0
"""


# ================================================================ data ====================================================
@dataclass
class NodeView:
    name: str
    master: bool = False
    ready: bool = True
    pressure: dict = field(default_factory=dict)        # {"Memory": True, ...} only the conditions that are on
    ip: str = ""
    os_image: str = ""
    kernel: str = ""
    arch: str = ""
    runtime: str = ""
    kubelet: str = ""
    cpu_cap: str = ""
    mem_alloc_mib: int = 0
    gpu_alloc: int = 0
    # probe
    probe_ok: bool = False
    cpu_pct: Optional[float] = None
    temps: dict = field(default_factory=dict)
    freq_mhz: Optional[int] = None
    mem_used_mib: Optional[int] = None
    mem_total_mib: Optional[int] = None
    swap_used_mib: Optional[int] = None
    swap_total_mib: Optional[int] = None
    power_w: Optional[float] = None
    load: tuple = ()
    uptime_s: Optional[int] = None
    cores: Optional[int] = None                         # PHYSICAL cores (unique physical id + core id in /proc/cpuinfo)
    threads: Optional[int] = None                       # logical CPUs = hardware threads (processors in /proc/cpuinfo)
    cpu_model: str = ""
    throttle_count: Optional[int] = None
    nics: list = field(default_factory=list)            # {name, speed, state, rx_bps, tx_bps}
    disks: list = field(default_factory=list)           # {name, size_gb, kind, model}
    peers: dict = field(default_factory=dict)           # name -> ms (None = lost)
    gpu: Optional[gpumod.GpuReading] = None
    gpu_info: dict = field(default_factory=dict)
    # more from the probe
    dmi: dict = field(default_factory=dict)             # vendor, product, BIOS ...
    gov: tuple = ()                                     # (governor, min MHz, max MHz, hardware max MHz)
    turbo_off: Optional[bool] = None
    cpufreqs: list = field(default_factory=list)        # MHz per core
    ctemps: dict = field(default_factory=dict)          # coretemp labels -> °C
    tzones: dict = field(default_factory=dict)          # thermal zones -> °C
    psi: dict = field(default_factory=dict)             # "cpu some" -> % stalled (avg10)
    meminfo: dict = field(default_factory=dict)         # key -> MiB
    dio: dict = field(default_factory=dict)             # disk -> (read B/s, write B/s)
    files: Optional[int] = None
    tasks: str = ""
    # more from the Kubernetes API
    conditions: dict = field(default_factory=dict)      # type -> (status, reason)
    taints: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    unschedulable: bool = False
    cap: dict = field(default_factory=dict)
    alloc: dict = field(default_factory=dict)
    age_s: Optional[int] = None
    images: tuple = (0, 0)                              # (count, bytes)
    addresses: dict = field(default_factory=dict)
    top: dict = field(default_factory=dict)             # metrics-server: cpu_m, cpu_pct, mem_mib, mem_pct
    has_baseline: bool = False
    # Kubernetes
    pods: list = field(default_factory=list)            # {name, ns, phase, restarts, cpu_m, mem_mib}
    last_test: str = ""
    # history for the graphs
    h_cpu: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_temp: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_gpu: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_net: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_mem: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_freq: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_pwr: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_gu: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    h_t: deque = field(default_factory=lambda: deque(maxlen=HISTORY))     # when each sample was taken (epoch s)

    @property
    def mem_pct(self) -> Optional[float]:
        return 100 * self.mem_used_mib / self.mem_total_mib if self.mem_used_mib is not None and self.mem_total_mib else None

    @property
    def temp(self) -> Optional[int]:
        return self.temps.get("CPU", max(self.temps.values()) if self.temps else None)

    @property
    def pods_running(self) -> int:
        return sum(1 for p in self.pods if p["phase"] == "Running")

    @property
    def pods_bad(self) -> int:
        return sum(1 for p in self.pods if p["phase"] not in ("Running", "Succeeded"))

    @property
    def restarts(self) -> int:
        return sum(p["restarts"] for p in self.pods)

    @property
    def lim_cpu_m(self) -> int:
        return sum(p.get("lim_cpu_m", 0) for p in self.pods)

    @property
    def lim_mem_mib(self) -> int:
        return sum(p.get("lim_mem_mib", 0) for p in self.pods)

    @property
    def req_cpu_m(self) -> int:
        return sum(p["cpu_m"] for p in self.pods)

    @property
    def req_mem_mib(self) -> int:
        return sum(p["mem_mib"] for p in self.pods)

    @property
    def peer_avg(self) -> Optional[float]:
        values = [v for v in self.peers.values() if v is not None]
        return sum(values) / len(values) if values else None

    @property
    def gpu_name(self) -> str:
        return self.gpu_info.get("GPU", "")

    @property
    def rx_tx(self) -> tuple:
        rx = sum(n["rx_bps"] or 0 for n in self.nics)
        tx = sum(n["tx_bps"] or 0 for n in self.nics)
        return rx, tx


@dataclass
class ClusterView:
    nodes: list = field(default_factory=list)
    events: list = field(default_factory=list)          # (age text, reason, object, message) newest first
    workloads: dict = field(default_factory=dict)       # {"deployments": (ready, desired), ...}
    namespaces: int = 0
    ns_pods: dict = field(default_factory=dict)
    services: int = 0
    svc_types: dict = field(default_factory=dict)       # ClusterIP / NodePort / LoadBalancer ... -> count
    ingresses: int = 0
    pvcs: dict = field(default_factory=dict)            # phase -> count, "gib" -> requested GiB
    pvs: int = 0
    jobs: dict = field(default_factory=dict)            # active / failed / done
    cronjobs: int = 0
    configmaps: int = 0
    api_ms: Optional[float] = None
    ev_counts: dict = field(default_factory=dict)       # reason -> count of warnings
    problems: list = field(default_factory=list)        # (node, ns/name, reason, restarts) of pods that are not fine
    top_restarts: list = field(default_factory=list)
    running: list = field(default_factory=list)         # background tests
    plans: list = field(default_factory=list)           # planned tests
    stamp: float = 0.0
    error: str = ""


def parse_extras(text: str) -> dict:
    """The lines EXTRA_SCRIPT prints -> {uptime, load, swap_total_kb, swap_free_kb, cores, cpu_model, thr, nics, disks, peers}."""
    out: dict = {"nics": [], "disks": [], "peers": {}, "dmi": {}, "ctemp": {}, "tz": {}, "psi": {}, "mem": {}, "dio": {}, "cpufreqs": []}
    for line in text.splitlines():
        key, _, rest = line.partition(" ")
        parts = rest.split()
        try:
            if key in ("uptime", "cores", "threads", "thr", "swap_total_kb", "swap_free_kb") and parts:
                out[key] = int(parts[0])
            elif key == "load" and len(parts) >= 3:
                out["load"] = tuple(float(p) for p in parts[:3])
            elif key == "cpu_model":
                out["cpu_model"] = clean_text(rest, 80)
            elif key == "nic" and len(parts) >= 5:
                num = lambda i: int(parts[i]) if len(parts) > i and parts[i].isdigit() else 0                # noqa: E731
                out["nics"].append({"name": parts[0], "speed": max(0, int(parts[1])) if parts[1].lstrip("-").isdigit() else 0,
                                    "state": parts[2], "rx": int(parts[3]), "tx": int(parts[4]), "mtu": num(5),
                                    "duplex": parts[6] if len(parts) > 6 else "?", "rx_err": num(7), "tx_err": num(8),
                                    "rx_drop": num(9), "tx_drop": num(10)})
            elif key == "dmi" and parts:
                out["dmi"][parts[0]] = clean_text(" ".join(parts[1:]), 60)
            elif key == "gov" and len(parts) >= 4:
                out["gov"] = (parts[0], int(parts[1]), int(parts[2]), int(parts[3]))
            elif key == "turbo" and parts:
                out["turbo_off"] = parts[0] == "1"
            elif key == "cpufreqs":
                out["cpufreqs"] = [int(p) for p in parts if p.isdigit()]
            elif key == "ctemp" and len(parts) >= 2:
                out["ctemp"][parts[0].replace("_", " ")] = int(parts[1]) // 1000
            elif key == "tz" and len(parts) >= 2:
                out["tz"][parts[0]] = int(parts[1]) // 1000
            elif key == "psi" and len(parts) >= 3:
                out["psi"][f"{parts[0]} {parts[1]}"] = float(parts[2])
            elif key == "mem" and len(parts) >= 2:
                out["mem"][parts[0]] = int(parts[1]) // 1024
            elif key == "dio" and len(parts) >= 3:
                out["dio"][parts[0]] = (int(parts[1]), int(parts[2]))
            elif key == "files" and parts:
                out["files"] = int(parts[0])
            elif key == "tasks" and parts:
                out["tasks"] = parts[0]
            elif key == "disk" and len(parts) >= 3:
                out["disks"].append({"name": parts[0], "size_gb": int(parts[1]) // 1, "kind": "HDD" if parts[2] == "1" else "SSD/NVMe",
                                     "model": clean_text(" ".join(parts[3:]), 40)})
            elif key == "peer" and len(parts) >= 2:
                out["peers"][parts[0]] = None if parts[1] == "lost" else float(parts[1])
        except ValueError:
            continue
    return out


def _quantity_cpu_m(text) -> int:
    """CPU quantity in millicores: '500m', '2', '0.5', '1500u'; 0 for anything odd."""
    text = str(text or "0").strip()
    try:
        if text.endswith("m"):
            return int(float(text[:-1]))
        if text.endswith("u"):
            return int(float(text[:-1]) / 1000)
        return int(float(text) * 1000)
    except ValueError:
        return 0


_SI = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
       "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60, "m": 1e-3}


def _quantity_mem_mib(text) -> int:
    """A Kubernetes quantity in MiB: '128Mi', '10M', '1G', '1.5Gi', '512k', '1e6', a bare number = bytes; 0 for anything odd."""
    text = str(text or "0").strip()
    try:
        for suffix in sorted(_SI, key=len, reverse=True):
            if text.endswith(suffix) and not text.endswith("e") and text[:-len(suffix)].replace(".", "", 1).isdigit():
                return int(float(text[:-len(suffix)]) * _SI[suffix] / 2 ** 20)
        return int(float(text) / 2 ** 20)
    except ValueError:
        return 0


class Collector:
    """Gathers everything for the dashboard. `refresh()` does one round (the thread calls it every interval)."""

    def __init__(self, kube: Kubectl, only: Optional[list] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.kube = kube
        self.only = only
        self._clock = clock
        self.view = ClusterView()
        self.rid = PodNames.new().run_id
        self.pods: dict = {}                       # node -> (probe pod, gpu pod or "")
        self.nodes: dict = {}                      # name -> NodeView
        self._prev: dict = {}                      # name -> (cpu counters, energy, nic counters, time)
        self._k8s_at = 0.0
        self._events_at = 0.0
        self._tests_at = 0.0
        self._test_cache: dict = {}                # log path -> (mtime, summary)
        self._lock = threading.RLock()
        self.status = "starting"

    # --- probe pods (only while the dashboard is open) ---------------------------------------------------------------
    def start(self, emit: Callable[[str], None] = lambda _t: None) -> None:
        self._cleanup_leftovers()
        names = [n for n in self.kube.list_node_names() if not self.only or n in self.only]
        infos = {n: self.kube.get_node(n) for n in names}
        threads = []
        for name, info in infos.items():
            view = self.nodes.setdefault(name, NodeView(name))
            view.master, view.ready, view.gpu_alloc = info.is_control_plane, info.ready, info.gpu_count
            if not info.ready:
                continue
            probe, gpu = f"dash-probe-{self.rid}-{len(self.pods)}", ""
            if info.gpu_count > 0:
                gpu = f"dash-gpu-{self.rid}-{len(self.pods)}"
            self.pods[name] = (probe, gpu)

            def prepare(n=name, p=probe, g=gpu) -> None:
                try:
                    self.kube.apply(dashboard_pod(n, p, self.rid, POD_DEADLINE))
                    if g:
                        self.kube.apply(dashboard_gpu_pod(n, g, self.rid, POD_DEADLINE))
                    ok = self.kube.wait_ready(p, 90) and (not g or self.kube.wait_ready(g, 120))
                except KubectlError:
                    ok = False
                self.nodes[n].probe_ok = ok
                emit(f"{'✅' if ok else '⚠️ '} {n}: {'probe ready' if ok else 'probe did not start (shown without live values)'}")
            t = threading.Thread(target=prepare, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def _cleanup_leftovers(self) -> None:
        self.kube.run("delete", "pod", "-l", "app=stress-test-dashboard", "--ignore-not-found", "--now", check=False, timeout=60)

    def stop(self) -> None:
        """Removes every probe pod of this dashboard (also called from a signal handler / finally)."""
        names = [n for pair in self.pods.values() for n in pair if n]
        if names:
            self.kube.delete_pods(*names)
        self._cleanup_leftovers()
        self.pods.clear()

    # --- one round -------------------------------------------------------------------------------------------------------
    def refresh(self) -> ClusterView:
        now = self._clock()
        if now - self._k8s_at >= K8S_EVERY or not self._k8s_at:
            self._guard(self._refresh_k8s, "nodes and pods")
            self._k8s_at = now
        if now - self._events_at >= EVENTS_EVERY or not self._events_at:
            self._guard(self._refresh_events, "events")
            self._events_at = now
        if now - self._tests_at >= 60 or not self._tests_at:
            self._guard(self._refresh_tests, "last tests")
            self._tests_at = now
        threads = [threading.Thread(target=self._safe_probe, args=(name,), daemon=True) for name in self.pods]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self._lock:
            self.view.nodes = [self.nodes[n] for n in self.nodes]
            self.view.running = [r for r in background.list_running() if not r.get("scheduled_at") or r["scheduled_at"] <= time.time()]
            try:
                self.view.plans = schedule.list_plans()
            except Exception:                                    # noqa: BLE001 - the plan list must not break the screen
                self.view.plans = []
            self.view.stamp = time.time()
        return self.view

    def _guard(self, step, what: str) -> None:
        """One part of a round may fail (an odd object in the cluster, a timeout) - the rest of the data must still show."""
        try:
            step()
        except Exception as exc:                                    # noqa: BLE001
            log.exception("dashboard: %s could not be refreshed", what)
            self.view.error = f"{what}: {str(exc)[:80]}"

    def _safe_probe(self, name: str) -> None:
        try:
            self._probe(name)
        except Exception:                                           # noqa: BLE001 - one node must not blank the others
            log.exception("dashboard probe of %s failed", name)
            self.nodes[name].probe_ok = False

    def _peers_env(self, name: str) -> str:
        return " ".join(f"{o.name}={o.ip}" for o in self.nodes.values() if o.name != name and o.ip and o.ready)

    def _probe(self, name: str) -> None:
        view = self.nodes[name]
        probe, gpu = self.pods[name]
        script = f'PEERS="{self._peers_env(name)}"\n' + PROBE_SCRIPT.rstrip().rsplit("exit 0", 1)[0] + EXTRA_SCRIPT
        now = self._clock()
        try:
            text = self.kube.exec(probe, script, timeout=25)
        except KubectlError as exc:
            log.warning("dashboard probe %s failed: %s", name, exc)
            view.probe_ok = False
            return
        view.probe_ok = True
        data: ProbeData = parse_probe_output(text)
        extra = parse_extras(text)
        now = self._clock()                                       # after the exec: the rates are over the time the data really stand for
        prev = self._prev.get(name) or {}
        view.cpu_pct = cpu_percent(prev.get("cpu"), data.cpu_stat)
        view.power_w = power_watts(prev.get("energy"), data.energy_uj, now, data.energy_max_uj)
        view.temps, view.freq_mhz = dict(data.temps), data.freq_mhz
        view.mem_total_mib = data.mem_total_mib
        view.mem_used_mib = data.mem_used_mib
        view.load, view.uptime_s = extra.get("load", ()), extra.get("uptime")
        view.threads = extra.get("threads")
        view.cores = extra.get("cores") or view.threads                    # no core ids (e.g. some ARM boards): one thread per core
        view.cpu_model, view.throttle_count = extra.get("cpu_model", view.cpu_model), extra.get("thr")
        if extra.get("swap_total_kb") is not None:
            view.swap_total_mib = extra["swap_total_kb"] // 1024
            view.swap_used_mib = (extra["swap_total_kb"] - extra.get("swap_free_kb", extra["swap_total_kb"])) // 1024
        view.disks, view.peers = extra["disks"], extra["peers"]
        view.dmi, view.gov, view.turbo_off = extra["dmi"] or view.dmi, extra.get("gov", view.gov), extra.get("turbo_off", view.turbo_off)
        view.cpufreqs, view.ctemps, view.tzones, view.psi = extra["cpufreqs"], extra["ctemp"], extra["tz"], extra["psi"]
        view.meminfo, view.files, view.tasks = extra["mem"], extra.get("files"), extra.get("tasks", "")
        span = (now - prev["t"]) if prev else 0
        old = prev.get("nic", {})
        view.nics = [{**n, "rx_bps": max(0.0, (n["rx"] - old[n["name"]][0]) / span) if span > 0 and n["name"] in old else None,
                      "tx_bps": max(0.0, (n["tx"] - old[n["name"]][1]) / span) if span > 0 and n["name"] in old else None}
                     for n in extra["nics"]]
        old_io = prev.get("dio", {})
        view.dio = {b: (max(0.0, (rd - old_io[b][0]) * 512 / span), max(0.0, (wr - old_io[b][1]) * 512 / span))
                    for b, (rd, wr) in extra["dio"].items() if span > 0 and b in old_io}
        self._prev[name] = {"cpu": data.cpu_stat, "energy": (data.energy_uj, now) if data.energy_uj is not None else None,
                            "nic": {n["name"]: (n["rx"], n["tx"]) for n in extra["nics"]}, "dio": dict(extra["dio"]), "t": now}
        if gpu:
            try:
                view.gpu = gpumod.parse_smi_csv(self.kube.exec(gpu, gpumod.SMI_QUERY, timeout=gpumod.SMI_TIMEOUT))
                if not view.gpu_info:
                    info = gpumod.parse_gpu_info(self.kube.exec(gpu, gpumod.INFO_QUERY, timeout=gpumod.SMI_TIMEOUT))
                    view.gpu_info = dict(line.split(": ", 1) for line in info if ": " in line)
            except KubectlError:
                view.gpu = None
        for hist, value in ((view.h_cpu, view.cpu_pct), (view.h_temp, view.temp), (view.h_gpu, view.gpu.temp if view.gpu else None),
                            (view.h_net, sum(view.rx_tx)), (view.h_mem, view.mem_pct), (view.h_freq, view.freq_mhz),
                            (view.h_pwr, view.power_w), (view.h_gu, view.gpu.util_pct if view.gpu else None)):
            hist.append(value)
        view.h_t.append(time.time())

    def _refresh_k8s(self) -> None:
        try:
            nodes = self.kube.get_json("nodes").get("items", [])
            pods = self.kube.get_json("pods", "-A").get("items", [])
        except (KubectlError, ValueError) as exc:
            self.view.error = str(exc)[:120]
            return
        self.view.error = ""
        for item in nodes:
            try:
                self._add_node(item)
            except (KeyError, TypeError, ValueError, AttributeError):
                log.warning("dashboard: a node could not be read", exc_info=True)
        for view in self.nodes.values():
            view.pods = []
        for pod in pods:
            try:
                self._add_pod(pod)
            except (KeyError, TypeError, ValueError, AttributeError):
                log.warning("dashboard: a pod could not be read", exc_info=True)
        self.view.namespaces = len({p.get("metadata", {}).get("namespace") for p in pods})
        self._derive_problems()
        self._guard(self._refresh_top, "metrics-server")
        self._guard(self._measure_api, "API latency")

    def _derive_problems(self) -> None:
        bad, restarts = [], []
        for n in self.nodes.values():
            for p in n.pods:
                tag = f"{p['ns']}/{p['name']}"
                if p["phase"] not in ("Running", "Succeeded") or p.get("reason", "").startswith(("CrashLoop", "ImagePull", "ErrImage", "Create")):
                    bad.append((n.name, tag, p.get("reason") or p["phase"], p["restarts"]))
                if p["restarts"] > 0:
                    restarts.append((n.name, tag, p.get("reason") or p["phase"], p["restarts"]))
        self.view.problems = sorted(bad, key=lambda x: -x[3])[:12]
        self.view.top_restarts = sorted(restarts, key=lambda x: -x[3])[:8]
        counts: dict = {}
        for n in self.nodes.values():
            for p in n.pods:
                counts[p["ns"]] = counts.get(p["ns"], 0) + 1
        self.view.ns_pods = counts

    def _refresh_top(self) -> None:
        """kubectl top nodes (metrics-server) - best effort, a cluster without it just has no `use` column."""
        raw = self.kube.run("top", "nodes", "--no-headers", check=False, timeout=20)
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] in self.nodes:
                try:
                    self.nodes[parts[0]].top = {"cpu_m": _quantity_cpu_m(parts[1]), "cpu_pct": float(parts[2].rstrip("%")),
                                                "mem_mib": _quantity_mem_mib(parts[3]), "mem_pct": float(parts[4].rstrip("%"))}
                except ValueError:
                    continue

    def _measure_api(self) -> None:
        t0 = time.monotonic()
        self.kube.run("get", "--raw", "/readyz", check=False, timeout=10)
        self.view.api_ms = (time.monotonic() - t0) * 1000

    def _add_node(self, item: dict) -> None:
        info = node_from_json(item)
        if self.only and info.name not in self.only:
            return
        view = self.nodes.setdefault(info.name, NodeView(info.name))
        view.master, view.ready, view.ip = info.is_control_plane, info.ready, info.internal_ip
        view.os_image, view.kernel, view.arch, view.runtime = info.os_image, info.kernel, info.architecture, info.runtime
        view.kubelet = item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion", "")
        view.cpu_cap, view.mem_alloc_mib, view.gpu_alloc = info.capacity_cpu, info.allocatable_mem_mib, info.gpu_count
        view.pressure = {c["type"].replace("Pressure", ""): True for c in item.get("status", {}).get("conditions", [])
                         if c.get("type", "").endswith("Pressure") and c.get("status") == "True"}
        status, spec, meta = item.get("status", {}), item.get("spec", {}), item.get("metadata", {})
        view.conditions = {c.get("type", "?"): (c.get("status", "?"), c.get("reason", "")) for c in status.get("conditions", [])}
        view.taints = [f"{t.get('key', '')}{'=' + t['value'] if t.get('value') else ''}:{t.get('effect', '')}" for t in spec.get("taints", []) or []]
        view.unschedulable = bool(spec.get("unschedulable"))
        keep = ("node-role.kubernetes.io", "kubernetes.io/arch", "node.kubernetes.io/instance-type", "nvidia.com/gpu", "topology.kubernetes.io")
        view.labels = {k: v for k, v in (meta.get("labels") or {}).items() if k.startswith(keep)}
        view.cap, view.alloc = dict(status.get("capacity", {})), dict(status.get("allocatable", {}))
        view.addresses = {a.get("type", "?"): a.get("address", "") for a in status.get("addresses", [])}
        images = status.get("images", []) or []
        view.images = (len(images), sum(int(i.get("sizeBytes", 0) or 0) for i in images))
        stamp = meta.get("creationTimestamp", "")
        try:
            view.age_s = int(time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) + time.timezone) if stamp else None
        except ValueError:
            view.age_s = None

    def _add_pod(self, pod: dict) -> None:
        node = self.nodes.get(pod.get("spec", {}).get("nodeName", ""))
        if node is None:
            return
        containers = pod.get("spec", {}).get("containers", [])
        statuses = pod.get("status", {}).get("containerStatuses", []) or []
        reason = ""
        for s in statuses:
            waiting = (s.get("state") or {}).get("waiting") or {}
            last = ((s.get("lastState") or {}).get("terminated") or {}).get("reason", "")
            if waiting.get("reason"):
                reason = waiting["reason"]
                break
            if last and last not in ("Unknown", "Completed") and not reason:
                reason = f"last: {last}"
        stamp = pod.get("metadata", {}).get("creationTimestamp", "")
        try:
            age = int(time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) + time.timezone) if stamp else None
        except ValueError:
            age = None
        node.pods.append({
            "name": pod["metadata"]["name"], "ns": pod["metadata"].get("namespace", ""),
            "phase": pod.get("status", {}).get("phase", "?"), "restarts": sum(s.get("restartCount", 0) for s in statuses),
            "reason": reason, "ready": f"{sum(1 for s in statuses if s.get('ready'))}/{len(containers)}", "age_s": age,
            "qos": pod.get("status", {}).get("qosClass", ""),
            "cpu_m": sum(_quantity_cpu_m(c.get("resources", {}).get("requests", {}).get("cpu")) for c in containers),
            "mem_mib": sum(_quantity_mem_mib(c.get("resources", {}).get("requests", {}).get("memory")) for c in containers),
            "lim_cpu_m": sum(_quantity_cpu_m(c.get("resources", {}).get("limits", {}).get("cpu")) for c in containers),
            "lim_mem_mib": sum(_quantity_mem_mib(c.get("resources", {}).get("limits", {}).get("memory")) for c in containers)})

    def _refresh_events(self) -> None:
        try:
            raw = self.kube.run("get", "events", "-A", "--field-selector", "type=Warning", "-o", "json", check=False, timeout=30)
            items = json.loads(raw).get("items", []) if raw.strip() else []
        except (KubectlError, ValueError):
            items = []
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "", reverse=True)
        self.view.events = [(((e.get("lastTimestamp") or "")[11:19] or "?"), e.get("reason", ""),
                             f"{e.get('involvedObject', {}).get('kind', '')}/{e.get('involvedObject', {}).get('name', '')}",
                             clean_text(e.get("message", ""), 160)) for e in items[:12]]
        counts_by_reason: dict = {}
        for e in items:
            counts_by_reason[e.get("reason", "?")] = counts_by_reason.get(e.get("reason", "?"), 0) + (e.get("count") or 1)
        self.view.ev_counts = dict(sorted(counts_by_reason.items(), key=lambda kv: -kv[1])[:6])
        self._guard(self._refresh_inventory, "inventory")
        try:
            svc = json.loads(self.kube.run("get", "services", "-A", "-o", "json", check=False, timeout=30) or "{}")
            self.view.services = len(svc.get("items", []))
            types: dict = {}
            for s in svc.get("items", []):
                kind = s.get("spec", {}).get("type", "ClusterIP")
                types[kind] = types.get(kind, 0) + 1
            self.view.svc_types = types
            counts = {}
            for kind in ("deployments", "statefulsets", "daemonsets"):
                data = json.loads(self.kube.run("get", kind, "-A", "-o", "json", check=False, timeout=30) or "{}")
                ready = desired = 0
                for item in data.get("items", []):
                    st, sp = item.get("status", {}), item.get("spec", {})
                    desired += sp.get("replicas", st.get("desiredNumberScheduled", 0)) or 0
                    ready += st.get("readyReplicas", st.get("numberReady", 0)) or 0
                counts[kind] = (ready, desired)
            self.view.workloads = counts
        except (KubectlError, ValueError):
            pass


    def _refresh_tests(self) -> None:
        """The newest result log of every node -> 'GPU 1 min · 67° · 01.10. 14:27', and whether the node has a baseline."""
        from . import baseline
        from .logparse import read_log
        from .paths import result_dirs, user_log_root
        root = user_log_root()
        newest: dict = {}
        for folder in result_dirs(root)[:6]:
            try:
                files = [p for p in folder.glob("*.log") if not p.name.startswith(("cluster-", "full-selftest", "net-matrix"))]
            except OSError:
                continue
            for path in files:
                for name in self.nodes:
                    if path.name.startswith(name + "-"):
                        mtime = path.stat().st_mtime
                        if name not in newest or mtime > newest[name][0]:
                            newest[name] = (mtime, path)
        for name, (mtime, path) in newest.items():
            cached = self._test_cache.get(str(path))
            if cached is None or cached[0] != mtime:
                try:
                    run = read_log(path)
                    temps = [s.cpu_temp for s in run.samples if s.cpu_temp is not None]
                    gtemps = [s.gpu_temp for s in run.samples if s.gpu_temp is not None]
                    top = max(gtemps) if run.profile == "gpu" and gtemps else max(temps) if temps else None
                    text = (f"{run.profile.upper() if run.profile in ('gpu', 'net') else run.profile} {run.duration_text}"
                            + (f" · {top}°" if top is not None else "") + f" · {time.strftime('%d.%m. %H:%M', time.localtime(mtime))}")
                except (OSError, ValueError):
                    text = time.strftime("%d.%m. %H:%M", time.localtime(mtime))
                self._test_cache[str(path)] = (mtime, text)
            self.nodes[name].last_test = self._test_cache[str(path)][1]
        for name, view in self.nodes.items():
            try:
                view.has_baseline = baseline.baseline_path(root, name).exists()
            except OSError:
                view.has_baseline = False

    def _json(self, *args: str) -> list:
        raw = self.kube.run("get", *args, "-A", "-o", "json", check=False, timeout=30)
        return json.loads(raw).get("items", []) if raw.strip() else []

    def _refresh_inventory(self) -> None:
        v = self.view
        v.ingresses = len(self._json("ingress"))
        pvcs: dict = {}
        gib = 0.0
        for c in self._json("pvc"):
            phase = c.get("status", {}).get("phase", "?")
            pvcs[phase] = pvcs.get(phase, 0) + 1
            gib += _quantity_mem_mib(c.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")) / 1024
        pvcs["GiB"] = round(gib, 1)
        v.pvcs = pvcs
        v.pvs = len(self.kube.run("get", "pv", "-o", "name", check=False, timeout=20).split())
        jobs = {"active": 0, "failed": 0, "done": 0}
        for j in self._json("jobs"):
            st = j.get("status", {})
            jobs["active"] += st.get("active", 0) or 0
            jobs["failed"] += st.get("failed", 0) or 0
            jobs["done"] += 1 if st.get("succeeded") else 0
        v.jobs = jobs
        v.cronjobs = len(self._json("cronjobs"))
        v.configmaps = len(self.kube.run("get", "configmaps", "-A", "-o", "name", check=False, timeout=30).split())


# ================================================================ drawing ===============================================
def tier(columns: int) -> int:
    return 1 if columns <= TIER1_MAX else 2 if columns <= TIER2_MAX else 3


def _bar(pct: Optional[float], length: int, on: bool, warn: float = 70, bad: float = 90) -> str:
    if pct is None:
        return ui.paint("·" * length, ui.GREY, on)
    code = ui.GREEN if pct < warn else ui.YELLOW if pct < bad else ui.RED
    done = max(0, min(length, round(length * pct / 100)))
    return ui.paint("█" * done, code, on) + ui.paint("░" * (length - done), ui.GREY, on)


def _pct(value: Optional[float]) -> str:
    return "  ?" if value is None else f"{value:>3.0f}%"


def _temp(value: Optional[float], on: bool, limit: int = 80) -> str:
    if value is None:
        return ui.paint("  ?", ui.GREY, on)
    code = ui.GREEN if value < 65 else ui.YELLOW if value < limit else ui.RED
    return ui.paint(f"{value:>3.0f}°", code, on)


def _spark(values, width: int, top: float, on: bool, code=None, floor: float = 0.0) -> str:
    data = [v for v in list(values)[-width:]]
    cells = "".join(" " if v is None else ui.spark(v - floor, max(top - floor, 1)) for v in data)
    return ui.paint(cells.ljust(width), code or ui.GREY, on)


def _rate(bps: Optional[float]) -> str:
    if bps is None:
        return "  ?"
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if bps >= div:
            value = bps / div
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return f"{bps:.0f}B"


def _gpu_cell(n: NodeView, on: bool, long: bool = False) -> str:
    if n.gpu_alloc <= 0:
        return ui.paint("–", ui.GREY, on)
    g = n.gpu
    if g is None:
        return ui.paint("GPU ?", ui.YELLOW, on)
    vram = (f" {g.mem_used_mib}/{g.mem_total_mib}M" if long and g.mem_total_mib else "")
    name = f"{n.gpu_name.replace('Quadro ', '').replace('NVIDIA ', '')[:10]} " if long and n.gpu_name else ""
    util = f" {g.util_pct}%" if g.util_pct is not None else ""
    return f"{name}{_temp(g.temp, on, 85)}{util}{vram}"


def _flags(n: NodeView, on: bool) -> str:
    out = []
    if not n.ready:
        out.append(ui.paint("NotReady", ui.RED, on))
    for key in n.pressure:
        out.append(ui.paint(f"{key[0]}!", ui.YELLOW, on))
    return " ".join(out) or ui.paint("ok", ui.GREEN, on)


def _row(cells: list, widths: list) -> str:
    """Cells padded / cut to their widths (display cells), joined with two spaces."""
    parts = []
    for text, w in zip(cells, widths):
        text = ui.fit(text, w)
        parts.append(text + " " * max(0, w - ui.visible_len(text)))
    return "  ".join(parts)


def _view_columns(view_name: str, tier_no: int, bar: int, on: bool) -> list:
    """The column sets of the other views of the table (key v): temperatures, network, disks, GPU."""
    node = ("NODE", 20 if tier_no == 1 else 22 if tier_no == 2 else 26, lambda n: n.name + (" (M)" if n.master else ""))
    if view_name == "temps":
        return [node, ("CPU", bar + 5, lambda n: f"{_bar(n.cpu_pct, bar, on)} {_pct(n.cpu_pct)}"), ("TEMP", 4, lambda n: _temp(n.temp, on)),
                ("CORES max", 9, lambda n: _temp(max(n.ctemps.values()), on) if n.ctemps else "?"),
                ("NVMe", 4, lambda n: _temp(n.temps.get("NVMe"), on)),
                ("CLOCK", 6, lambda n: f"{n.freq_mhz / 1000:.1f}GHz" if n.freq_mhz else "?"),
                ("PWR", 5, lambda n: f"{n.power_w:.0f} W" if n.power_w is not None else "?"),
                ("THROT", 5, lambda n: ui.paint(str(n.throttle_count), ui.YELLOW, on) if n.throttle_count else ("0" if n.throttle_count == 0 else "?")),
                ("GPU °", 5, lambda n: _temp(n.gpu.temp, on, 85) if n.gpu is not None else "–"),
                ("TEMP ▸ history", 16, lambda n: _spark(n.h_temp, 16, 90, on, ui.YELLOW, 25))]
    if view_name == "net":
        return [node, ("IP", 15, lambda n: n.ip or "?"),
                ("LINK", 7, lambda n: f"{max((x['speed'] for x in n.nics), default=0) or '?'}M" if n.nics else "?"),
                ("NET ↓↑", 11, lambda n: f"{_rate(n.rx_tx[0])} {_rate(n.rx_tx[1])}" if n.nics else "?"),
                ("PING", 6, lambda n: f"{n.peer_avg:.1f}ms" if n.peer_avg is not None else ("lost" if n.peers else "?")),
                ("LOST", 4, lambda n: str(sum(1 for v in n.peers.values() if v is None)) if n.peers else "?"),
                ("NET ▸ history", 16, lambda n: _spark(n.h_net, 16, max([v for v in n.h_net if v] or [1]), on, ui.CYAN))]
    if view_name == "disks":
        return [node, ("DISKS", 34, lambda n: ", ".join(f"{x['name']} {x['size_gb']:.0f}G {x['kind']}" for x in n.disks[:2]) or "?"),
                ("IO read/write", 13, lambda n: f"{_rate(sum(v[0] for v in n.dio.values()))} {_rate(sum(v[1] for v in n.dio.values()))}" if n.dio else "?"),
                ("RAM", bar + 5, lambda n: f"{_bar(n.mem_pct, bar, on, 80, 95)} {_pct(n.mem_pct)}"),
                ("SWAP", 11, lambda n: f"{n.swap_used_mib}/{n.swap_total_mib}M" if n.swap_total_mib else "none"),
                ("FILES", 7, lambda n: str(n.files) if n.files is not None else "?")]
    if view_name == "gpu":
        return [node, ("GPU", 30, lambda n: _gpu_cell(n, on, long=True)),
                ("ALLOC", 5, lambda n: str(n.gpu_alloc) if n.gpu_alloc else "–"),
                ("LOAD", 6, lambda n: f"{n.gpu.util_pct}%" if n.gpu is not None and n.gpu.util_pct is not None else "–"),
                ("FAN", 5, lambda n: f"{n.gpu.fan_pct}%" if n.gpu is not None and n.gpu.fan_pct is not None else "–"),
                ("POWER", 9, lambda n: f"{n.gpu.power_w:.0f}/{n.gpu.power_limit_w:.0f} W" if n.gpu is not None and n.gpu.power_w is not None and n.gpu.power_limit_w else "–"),
                ("GPU ▸ history", 16, lambda n: _spark(n.h_gpu, 16, 90, on, ui.YELLOW, 25))]
    return []


class Columns:
    """The table of nodes for a tier: (title, width, render(node) -> text)."""

    def __init__(self, tier_no: int, width: int, on: bool, view_name: str = "overview") -> None:
        self.on = on
        bar = 3 if width < 68 else 6 if tier_no == 1 else 8 if tier_no == 2 else 10
        cols = [("NODE", 20 if tier_no == 1 else 22 if tier_no == 2 else 26, lambda n: n.name + (" (M)" if n.master else "")),
                ("CPU", bar + 5, lambda n: f"{_bar(n.cpu_pct, bar, on)} {_pct(n.cpu_pct)}"),
                ("TEMP", 4, lambda n: _temp(n.temp, on)),
                ("RAM", bar + 5, lambda n: f"{_bar(n.mem_pct, bar, on, 80, 95)} {_pct(n.mem_pct)}")]
        if tier_no >= 2:
            cols.insert(1, ("ROLE", 6, lambda n: "master" if n.master else "worker"))
            cols += [("CLOCK", 6, lambda n: f"{n.freq_mhz / 1000:.1f}GHz" if n.freq_mhz else "?"),
                     ("PWR", 5, lambda n: f"{n.power_w:.0f} W" if n.power_w is not None else "?"),
                     ("LOAD", 4, lambda n: f"{n.load[0]:.1f}" if n.load else "?"),
                     ("NET ↓↑", 11, lambda n: f"{_rate(n.rx_tx[0])} {_rate(n.rx_tx[1])}" if n.nics else "?"),
                     ("PING", 6, lambda n: f"{n.peer_avg:.1f}ms" if n.peer_avg is not None else ("lost" if n.peers else "?"))]
        cols.append(("GPU", 8 if tier_no == 1 else 24, lambda n: _gpu_cell(n, on, long=tier_no >= 2)))
        cols.append(("PODS", 4, lambda n: f"{n.pods_running}" + (ui.paint(f"+{n.pods_bad}!", ui.YELLOW, on) if n.pods_bad else "")))
        if tier_no >= 2:
            cols.append(("STATE", 8, lambda n: _flags(n, on)))
            cols.append(("LAST TEST", 18 if tier_no == 2 else 30, lambda n: n.last_test or "–"))
        if tier_no >= 3:
            cols.insert(3, ("CPU ▸ 60 s", 14, lambda n: _spark(n.h_cpu, 14, 100, on, ui.GREEN)))
            cols.insert(5, ("TEMP ▸ 60 s", 14, lambda n: _spark(n.h_temp, 14, 90, on, ui.YELLOW, 25)))
            cols += [("IP", 15, lambda n: n.ip or "?"),
                     ("USE top", 9, lambda n: f"{n.top['cpu_pct']:.0f}%/{n.top['mem_pct']:.0f}%" if n.top else "–"),
                     ("KERNEL", 12, lambda n: n.kernel or "?"), ("K3S", 13, lambda n: n.kubelet or "?"),
                     ("IMG", 5, lambda n: str(n.images[0]) if n.images[0] else "?"),
                     ("REQ cpu/ram", 11, lambda n: f"{_pct(100 * n.req_cpu_m / (float(n.cpu_cap) * 1000) if n.cpu_cap.isdigit() and n.req_cpu_m else None).strip()}/"
                                              f"{_pct(100 * n.req_mem_mib / n.mem_alloc_mib if n.mem_alloc_mib and n.req_mem_mib else None).strip()}"),
                     ("RESTART", 7, lambda n: str(n.restarts)),
                     ("UP", 7, lambda n: format_duration(n.uptime_s).split(" ")[0] + (" " + format_duration(n.uptime_s).split(" ")[1] if n.uptime_s and n.uptime_s >= 3600 else "") if n.uptime_s else "?")]
        priority = ["IMG", "K3S", "KERNEL", "IP", "USE top", "UP", "RESTART", "REQ cpu/ram", "LAST TEST", "LOAD", "PING", "PWR", "CLOCK",
                    "TEMP ▸ 60 s", "CPU ▸ 60 s", "NET ↓↑", "STATE", "ROLE", "GPU"]
        if view_name != "overview" and _view_columns(view_name, tier_no, bar, on):
            cols = _view_columns(view_name, tier_no, bar, on)
            priority = ["FILES", "TEMP ▸ history", "NET ▸ history", "GPU ▸ history", "FAN", "POWER", "LOST", "THROT", "CORES max", "NVMe", "SWAP",
                        "IO read/write", "LINK", "ALLOC", "PWR", "CLOCK", "IP", "PING", "GPU °", "LOAD"]
        # drop the least important columns until it fits the width
        self.cols = cols
        room = width - 4 - 2
        while sum(w for _t, w, _f in self.cols) + 2 * (len(self.cols) - 1) > room and priority:
            drop = priority.pop(0)
            self.cols = [c for c in self.cols if c[0] != drop]
        over = sum(w for _t, w, _f in self.cols) + 2 * (len(self.cols) - 1) - room
        if over > 0:                                           # still too wide: the node name gives way (never below 12)
            self.cols = [(t, max(12, w - over) if t == "NODE" else w, f) for t, w, f in self.cols]
        # a spare width goes to the node name (up to the longest name) and the GPU cell
        spare = room - (sum(w for _t, w, _f in self.cols) + 2 * (len(self.cols) - 1))
        if spare > 0:
            self.cols = [(t, w + (min(spare, 6) if t == "NODE" else spare - min(spare, 6) if t == "GPU" and tier_no >= 2 else 0), f)
                         for t, w, f in self.cols]

    def header(self) -> str:
        return ui.paint("  " + _row([t for t, _w, _f in self.cols], [w for _t, w, _f in self.cols]), ui.GREY, self.on)

    def row(self, node: NodeView, selected: bool) -> str:
        text = _row([f(node) for _t, _w, f in self.cols], [w for _t, w, f in self.cols])
        mark = ui.paint("▸", ui.YELLOW, self.on) if selected else " "
        return f"{mark} {text}"


def _alerts(view: ClusterView, on: bool) -> list:
    out = []
    for n in view.nodes:
        if not n.ready:
            out.append(f"❌ {n.name} is NotReady")
        if n.pressure:
            out.append(f"⚠️ {n.name}: {', '.join(n.pressure)} pressure")
        if n.temp is not None and n.temp >= 80:
            out.append(f"🔥 {n.name} {n.temp:.0f}°C")
        if n.gpu is not None and n.gpu.temp is not None and n.gpu.temp >= 80:
            out.append(f"🔥 GPU {n.name} {n.gpu.temp}°C")
    bad = sum(n.pods_bad for n in view.nodes)
    if bad:
        where = sum(1 for n in view.nodes if n.pods_bad)
        out.append(f"⚠️ {bad} pod(s) not running on {where} node(s)")
    return [ui.paint(a, ui.YELLOW, on) for a in out[:3]]


def _tests_rows(view: ClusterView, on: bool) -> list:
    rows = []
    for r in view.running:
        rows.append(f"🟡 {ui.paint('running', ui.YELLOW, on)} {r.get('title') or r.get('node', '?')} ({r.get('node', '?')})")
    for p in view.plans[:5]:
        rows.append(f"🕒 {schedule.describe_when(p['start_at'])}  {p['title']}")
    return rows or [ui.paint("no test is running or planned", ui.GREY, on)]


def detail_rows(n: NodeView, on: bool, wide: bool = False) -> list:
    """All we know about one node, as text rows."""
    g = lambda label, value: f"{ui.paint(label, ui.GREY, on)} {value}"                      # noqa: E731
    rows = []
    from .parsing import cpu_summary
    cpu = cpu_description(n)
    rows.append(g("CPU", cpu) + (f"   {g('load', ' '.join(f'{x:.2f}' for x in n.load))}" if n.load else ""))
    mem = (f"{n.mem_used_mib}/{n.mem_total_mib} MiB ({_pct(n.mem_pct).strip()})" if n.mem_total_mib else "?")
    swap = f"   {g('swap', f'{n.swap_used_mib}/{n.swap_total_mib} MiB')}" if n.swap_total_mib else ""
    rows.append(g("RAM", mem) + swap)
    rows.append(g("OS", f"{n.os_image} · kernel {n.kernel} · {n.arch} · {n.runtime} · {n.kubelet}"))
    rows.append(g("IP", n.ip or "?") + (f"   {g('up', format_duration(n.uptime_s))}" if n.uptime_s else ""))
    if n.temps:
        rows.append(g("temps", "  ".join(f"{k} {v}°C" for k, v in n.temps.items())) +
                    (f"   {g('thermal throttle events', n.throttle_count)}" if n.throttle_count is not None else ""))
    if n.power_w is not None:
        rows.append(g("power", f"{n.power_w:.1f} W (CPU package)"))
    if n.gpu_alloc > 0:
        gi = n.gpu_info
        if n.gpu is not None:
            r = n.gpu
            rows.append(g("GPU", f"{n.gpu_name or 'NVIDIA'} · driver {gi.get('Driver', '?')} · {r.temp}°C · {r.util_pct}% · "
                                 f"{r.sm_mhz} MHz · VRAM {r.mem_used_mib}/{r.mem_total_mib or gi.get('Memory', '?')} MiB · "
                                 f"power {('%.0f W' % r.power_w) if r.power_w is not None else 'N/A'}"
                                 + (f" · fan {r.fan_pct}%" if r.fan_pct is not None else "")
                                 + (f" · throttle {','.join(gpumod.throttle_names(r.throttle, True))}" if gpumod.throttle_names(r.throttle, True) else "")))
        else:
            rows.append(g("GPU", "allocatable but no reading"))
    for nic in n.nics[:3]:
        rows.append(g("NIC", f"{nic['name']} {nic['speed'] or '?'} Mbit/s {nic['state']}  ↓ {_rate(nic['rx_bps'])}/s  ↑ {_rate(nic['tx_bps'])}/s"))
    for d in n.disks[:4]:
        rows.append(g("disk", f"{d['name']} {d['size_gb']} GB {d['kind']} {d['model']}"))
    if n.peers:
        rows.append(g("ping", "  ".join(f"{k} {'lost' if v is None else f'{v:.1f}ms'}" for k, v in n.peers.items())))
    rows.append(g("pods", f"{n.pods_running} running" + (f", {n.pods_bad} not running" if n.pods_bad else "") + f" · {n.restarts} restarts · requests "
                          f"{n.req_cpu_m} m CPU / {n.req_mem_mib} MiB"))
    flags = _flags(n, on)
    rows.append(g("state", flags) + (f"   {g('last test', n.last_test)}" if n.last_test else ""))
    return rows


def pods_rows(n: NodeView, on: bool, limit: int) -> list:
    bad_first = sorted(n.pods, key=lambda p: (p["phase"] == "Running", -p["restarts"], p["ns"], p["name"]))
    rows = []
    for p in bad_first[:limit]:
        phase = ui.paint(p["phase"], ui.GREEN if p["phase"] == "Running" else ui.YELLOW, on)
        rows.append(f"{p['ns'][:14]:<14} {p['name'][:34]:<34} {phase:<{12 + (len(phase) - ui.visible_len(phase))}} ↻{p['restarts']}")
    return rows or [ui.paint("no pods", ui.GREY, on)]


def events_rows(view: ClusterView, on: bool, limit: int, width: int) -> list:
    rows = [f"{ui.paint(t, ui.GREY, on)} {ui.paint(reason, ui.YELLOW, on)} {obj[:30]} {msg}" for t, reason, obj, msg in view.events[:limit]]
    return rows or [ui.paint("no warnings", ui.GREEN, on)]


def cluster_rows(view: ClusterView, on: bool) -> list:
    w = view.workloads
    parts = [f"{len(view.nodes)} nodes", f"{view.namespaces} namespaces", f"{view.services} services"]
    parts += [f"{k} {r}/{d}" for k, (r, d) in w.items()]
    kube = sorted({n.kubelet for n in view.nodes if n.kubelet})
    return [ui.paint(" · ".join(parts), ui.GREY, on) + (f"   {ui.paint('k3s ' + ', '.join(kube), ui.GREY, on)}" if kube else "")]


def _kv(label: str, value: str, on: bool) -> str:
    return f"{ui.paint(label, ui.GREY, on)} {value}"


def _title(text: str, on: bool) -> str:
    return ui.paint(f"▸ {text}", ui.BOLD, on)


def _age(seconds: Optional[int]) -> str:
    """'14 d 2 h', '5 h 10 min', '3 min'."""
    if not seconds:
        return "?"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return (f"{days} d {hours} h" if days else f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min")


def cpu_description(n: "NodeView") -> str:
    """'Intel Core i7-4790S @ 3.2 GHz · 4 cores / 8 threads' - the counts are written out, never 4c/8t."""
    from .parsing import cpu_summary
    if not n.cpu_model:
        return "?"
    text = cpu_summary(f"CPU: {n.cpu_model}")
    name = re.sub(r"\s*·.*$", "", text)
    freq = re.search(r"@ [\d.]+ GHz", text)
    name = name.strip()
    counts = ""
    if n.cores and n.threads:
        counts = (f"{n.cores} core{'s' if n.cores != 1 else ''} / {n.threads} thread{'s' if n.threads != 1 else ''}"
                  + (" (hyper-threading)" if n.threads > n.cores else ""))
    elif n.threads:
        counts = f"{n.threads} thread{'s' if n.threads != 1 else ''}"
    return name + (f" {freq.group(0)}" if freq and "GHz" not in name else "") + (f" · {counts}" if counts else "")


def _gib(mib) -> str:
    return f"{mib / 1024:.1f} GiB" if mib is not None else "?"


def block_hw(n: NodeView, on: bool) -> list:
    """Everything the probe knows about the machine of the selected node."""
    from .parsing import cpu_summary
    rows = [_title(f"{n.name} · hardware", on)]
    if n.dmi:
        d = n.dmi
        version = d.get("product_version", "")
        version = "" if version.lower() in ("", "00", "0", "not specified", "none", "default string", "to be filled by o.e.m.") else version
        rows.append(_kv("machine", f"{d.get('sys_vendor', '?')} {d.get('product_name', '')} {version}".strip()
                        + (f" · board {d['board_name']}" if d.get("board_name") else ""), on))
        if d.get("bios_version"):
            rows.append(_kv("BIOS", f"{d.get('bios_vendor', '')} {d['bios_version']} ({d.get('bios_date', '?')})", on))
    rows.append(_kv("CPU", cpu_description(n), on))
    if n.gov:
        rows.append(_kv("governor", f"{n.gov[0]} · {n.gov[1]}-{n.gov[2]} MHz (hardware max {n.gov[3]} MHz)"
                        + ("" if n.turbo_off is None else " · turbo " + ("OFF" if n.turbo_off else "on")), on))
    if n.cpufreqs:
        rows.append(_kv("core clocks", " ".join(f"{f / 1000:.1f}" for f in n.cpufreqs[:16]) + " GHz", on))
    if n.ctemps:
        rows.append(_kv("core temps", "  ".join(f"{k.replace('Core ', 'C')} {v}°" for k, v in list(n.ctemps.items())[:12]), on))
    others = {k: v for k, v in n.temps.items() if k != "CPU"}
    zones = {k: v for k, v in n.tzones.items()}
    if others or zones:
        rows.append(_kv("sensors", "  ".join(f"{k} {v}°" for k, v in {**others, **zones}.items()), on))
    if n.throttle_count is not None:
        rows.append(_kv("thermal throttle events", str(n.throttle_count), on))
    if n.power_w is not None:
        rows.append(_kv("power", f"{n.power_w:.1f} W (CPU package)", on))
    if n.load or n.tasks:
        rows.append(_kv("load", " ".join(f"{x:.2f}" for x in n.load) + (f" · tasks {n.tasks}" if n.tasks else "")
                        + (f" · open files {n.files}" if n.files is not None else ""), on))
    if n.psi:
        rows.append(_kv("pressure (stall avg10)", "  ".join(f"{k} {v:.1f}%" for k, v in n.psi.items()), on))
    if n.mem_total_mib:
        m = n.meminfo
        rows.append(_kv("RAM", f"{n.mem_used_mib}/{n.mem_total_mib} MiB ({_pct(n.mem_pct).strip()})"
                        + "".join(f" · {lab} {m[key]}" for lab, key in (("free", "MemFree"), ("buffers", "Buffers"), ("cached", "Cached"),
                                                                          ("dirty", "Dirty"), ("slab", "Slab"), ("anon", "AnonPages"),
                                                                          ("shmem", "Shmem")) if key in m) + " MiB", on))
    if n.swap_total_mib:
        rows.append(_kv("swap", f"{n.swap_used_mib}/{n.swap_total_mib} MiB", on))
    for disk in n.disks[:4]:
        io = n.dio.get(disk["name"])
        rows.append(_kv("disk", f"{disk['name']} {disk['size_gb']} GB {disk['kind']} {disk['model']}"
                        + (f" · read {_rate(io[0])}/s write {_rate(io[1])}/s" if io else ""), on))
    for nic in n.nics[:3]:
        errs = (nic.get("rx_err", 0) or 0) + (nic.get("tx_err", 0) or 0)
        drops = (nic.get("rx_drop", 0) or 0) + (nic.get("tx_drop", 0) or 0)
        rows.append(_kv("NIC", f"{nic['name']} {nic['speed'] or '?'} Mbit/s {nic.get('duplex', '?')} {nic['state']} mtu {nic.get('mtu', '?')}"
                        f" · ↓ {_rate(nic['rx_bps'])}/s ↑ {_rate(nic['tx_bps'])}/s"
                        + (ui.paint(f" · errors {errs} drops {drops}", ui.YELLOW, on) if errs or drops else " · no errors"), on))
    if n.peers:
        rows.append(_kv("ping", "  ".join(f"{k} {'lost' if v is None else f'{v:.1f}ms'}" for k, v in n.peers.items()), on))
    if n.gpu_alloc > 0:
        gi, r = n.gpu_info, n.gpu
        if r is not None:
            rows.append(_kv("GPU", f"{n.gpu_name or 'NVIDIA'} · driver {gi.get('Driver', '?')} · CC {gi.get('Compute capability', '?')} · "
                                   f"PCIe {gi.get('PCIe (max)', '?')} {gi.get('PCIe width', '')}", on))
            rows.append(_kv("GPU now", f"{r.temp}°C · {r.util_pct}% · {r.sm_mhz}/{gi.get('Max SM clock', '?')} · VRAM {r.mem_used_mib}/"
                                       f"{r.mem_total_mib or gi.get('Memory', '?')} MiB · power {('%.0f W' % r.power_w) if r.power_w is not None else 'N/A'}"
                                       f" (limit {gi.get('Power limit', 'N/A')}) · {r.pstate}"
                                       + (f" · fan {r.fan_pct}%" if r.fan_pct is not None else "")
                                       + (f" · throttle {','.join(gpumod.throttle_names(r.throttle, True))}"
                                          if gpumod.throttle_names(r.throttle, True) else ""), on))
        else:
            rows.append(_kv("GPU", "allocatable but no reading", on))
    return rows


def _res_row(label: str, alloc: str, req: str, lim: str, use: str, on: bool) -> str:
    return f"{ui.paint(f'{label:<4}', ui.GREY, on)} alloc {alloc:<9} req {req:<14} limits {lim:<9} use {use}"


def block_k8s(n: NodeView, on: bool) -> list:
    """What Kubernetes says about the selected node."""
    rows = [_title(f"{n.name} · Kubernetes", on)]
    cond = []
    for kind in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"):
        if kind in n.conditions:
            status = n.conditions[kind][0]
            good = (status == "True") if kind == "Ready" else (status != "True")
            cond.append(ui.paint(f"{kind.replace('Pressure', '')} {status}", ui.GREEN if good else ui.RED, on))
    rows.append(_kv("conditions", "  ".join(cond) or "?", on))
    rows.append(_kv("schedulable", ui.paint("CORDONED (unschedulable)", ui.YELLOW, on) if n.unschedulable else "yes", on)
                + _kv("   taints", ", ".join(n.taints) or "none", on))
    if n.labels:
        rows.append(_kv("labels", " ".join(f"{k.split('/')[-1]}={v}" if v else k.split("/")[-1] for k, v in list(n.labels.items())[:6]), on))
    rows.append(_kv("system", f"{n.os_image} · kernel {n.kernel} · {n.arch} · {n.runtime} · {n.kubelet}", on))
    addr = " ".join(f"{k} {v}" for k, v in n.addresses.items() if v)
    rows.append(_kv("addresses", addr + (f" · node age {_age(n.age_s)}" if n.age_s else ""), on))
    if n.images[0]:
        rows.append(_kv("images", f"{n.images[0]} cached, {n.images[1] / 1e9:.1f} GB", on))
    a, c = n.alloc, n.cap
    cpu_alloc = _quantity_cpu_m(a.get("cpu"))
    mem_alloc = _quantity_mem_mib(a.get("memory"))
    pct = lambda used, total: f"{100 * used / total:.0f}%" if total else "?"                       # noqa: E731
    top = n.top
    rows.append(_res_row("CPU", f"{cpu_alloc} m", f"{n.req_cpu_m} m ({pct(n.req_cpu_m, cpu_alloc)})", f"{n.lim_cpu_m} m",
                         f"{top['cpu_pct']:.0f}% ({top['cpu_m']} m)" if top else "n/a (no metrics-server)", on))
    rows.append(_res_row("RAM", f"{mem_alloc} Mi", f"{n.req_mem_mib} Mi ({pct(n.req_mem_mib, mem_alloc)})", f"{n.lim_mem_mib} Mi",
                         f"{top['mem_pct']:.0f}% ({top['mem_mib']} Mi)" if top else "n/a", on))
    rows.append(_kv("pods", f"{len(n.pods)} of {a.get('pods', c.get('pods', '?'))} · running {n.pods_running} · not running {n.pods_bad} · "
                            f"restarts {n.restarts}", on)
                + (_kv(" · ephemeral", f"{_quantity_mem_mib(a.get('ephemeral-storage')) / 1024:.0f} GiB", on) if a.get("ephemeral-storage") else "")
                + (_kv(" · gpu", a.get("nvidia.com/gpu", "0"), on) if n.gpu_alloc else ""))
    rows.append(_kv("last test", n.last_test or "none yet", on) + ("   " + ui.paint("✓ baseline", ui.GREEN, on) if n.has_baseline else
                                                                   "   " + ui.paint("no baseline", ui.GREY, on)))
    return rows


def block_pods(n: NodeView, on: bool, limit: int) -> list:
    rows = [_title(f"{n.name} · pods ({len(n.pods)})", on)]
    ordered = sorted(n.pods, key=lambda p: (p["phase"] == "Running" and not p.get("reason", "").startswith(("CrashLoop", "ImagePull")),
                                            -p["restarts"], p["ns"], p["name"]))
    for p in ordered[:max(1, limit - 1)]:
        shown = p.get("reason") or p["phase"]
        code = ui.GREEN if p["phase"] == "Running" and not p.get("reason", "").startswith(("CrashLoop", "ImagePull", "last")) else ui.YELLOW
        age = [_age(p["age_s"])] if p.get("age_s") else []
        rows.append(f"{p['ns'][:14]:<14} {p['name'][:30]:<30} {p.get('ready', ''):<4} {ui.paint(f'{shown[:18]:<18}', code, on)} "
                    f"↻{p['restarts']:<3} {' '.join(age):<7} {p.get('qos', '')[:2]}")
    if len(ordered) > limit - 1:
        rows.append(ui.paint(f"… and {len(ordered) - (limit - 1)} more", ui.GREY, on))
    return rows


def block_cluster(view: ClusterView, on: bool) -> list:
    nodes = view.nodes
    rows = [_title("cluster", on)]
    ready = sum(1 for n in nodes if n.ready)
    cores = sum(int(n.cpu_cap) for n in nodes if str(n.cpu_cap).isdigit())
    ram = sum(n.mem_alloc_mib for n in nodes)
    gpus = sum(n.gpu_alloc for n in nodes)
    pods = sum(len(n.pods) for n in nodes)
    max_pods = sum(int(n.alloc.get("pods", 0) or 0) for n in nodes)
    cpu_alloc = sum(_quantity_cpu_m(n.alloc.get("cpu")) for n in nodes)
    req_cpu, req_mem = sum(n.req_cpu_m for n in nodes), sum(n.req_mem_mib for n in nodes)
    rows.append(_kv("nodes", f"{ready}/{len(nodes)} Ready · {sum(1 for n in nodes if n.master)} master · {sum(1 for n in nodes if n.unschedulable)} cordoned"
                    + (f" · k3s {', '.join(sorted({n.kubelet for n in nodes if n.kubelet}))}" if any(n.kubelet for n in nodes) else ""), on))
    rows.append(_kv("capacity", f"{cores} logical CPUs · {_gib(ram)} RAM · {gpus} GPU · {pods}/{max_pods or '?'} pods", on))
    rows.append(_kv("requested", f"CPU {req_cpu} m of {cpu_alloc} m ({100 * req_cpu / cpu_alloc:.0f}%)" if cpu_alloc else "CPU ?", on)
                + _kv(" · RAM", f"{req_mem} Mi of {ram} Mi ({100 * req_mem / ram:.0f}%)" if ram else "?", on))
    top_ns = sorted(view.ns_pods.items(), key=lambda kv: -kv[1])[:6]
    rows.append(_kv("namespaces", f"{view.namespaces} · " + " ".join(f"{k}({v})" for k, v in top_ns), on))
    rows.append(_kv("services", f"{view.services} · " + " ".join(f"{k} {v}" for k, v in view.svc_types.items())
                    + f" · ingress {view.ingresses} · configmaps {view.configmaps}", on))
    if view.pvcs or view.pvs:
        rows.append(_kv("storage", f"PVC " + " ".join(f"{k} {v}" for k, v in view.pvcs.items() if k != "GiB")
                        + f" · {view.pvcs.get('GiB', 0)} GiB requested · PV {view.pvs}", on))
    if view.jobs:
        rows.append(_kv("jobs", f"active {view.jobs['active']} · failed {view.jobs['failed']} · done {view.jobs['done']} · cronjobs {view.cronjobs}", on))
    if view.workloads:
        rows.append(_kv("workloads", " · ".join(f"{k} {r}/{d}" for k, (r, d) in view.workloads.items()), on))
    if view.api_ms is not None:
        rows.append(_kv("API server", f"{view.api_ms:.0f} ms (/readyz)", on))
    if view.ev_counts:
        rows.append(_kv("warnings", " · ".join(f"{k} ×{v}" for k, v in view.ev_counts.items()), on))
    return rows


def block_problems(view: ClusterView, on: bool, width: int) -> list:
    rows = [_title("problems", on)]
    if view.problems:
        for node, tag, reason, restarts in view.problems[:6]:
            rows.append(f"{ui.paint('✗', ui.RED, on)} {tag[:36]:<36} {ui.paint(reason[:20], ui.YELLOW, on):<20} ↻{restarts} {node}")
    else:
        rows.append(ui.paint("✓ every pod is running", ui.GREEN, on))
    if view.top_restarts:
        rows.append(_kv("most restarts", "  ".join(f"{t.split('/')[-1][:22]} ↻{r}" for _n, t, _x, r in view.top_restarts[:4]), on))
    for t, reason, obj, msg in view.events[:4]:
        rows.append(f"{ui.paint(t, ui.GREY, on)} {ui.paint(reason, ui.YELLOW, on)} {obj[:28]} {msg}")
    return rows


def block_tests(view: ClusterView, on: bool) -> list:
    rows = [_title("tests", on)]
    rows += _tests_rows(view, on)[:3]
    for n in view.nodes:
        if n.last_test:
            rows.append(f"{n.name[:24]:<24} {n.last_test}" + (ui.paint("  ✓ baseline", ui.GREEN, on) if n.has_baseline else ""))
    return rows


def _fit_rows(rows: list, height: int, on: bool) -> list:
    if len(rows) <= height:
        return rows
    return rows[:height - 1] + [ui.paint(f"… {len(rows) - height + 1} more rows (make the window taller)", ui.GREY, on)]


def join_n(columns: list, widths: list, on: bool) -> list:
    """Columns of rows side by side, separated by ' │ ', every row padded to its column."""
    height = max((len(c) for c in columns), default=0)
    sep = ui.paint(" │ ", ui.GREY, on)
    out = []
    for i in range(height):
        cells = []
        for col, w in zip(columns, widths):
            text = ui.fit(col[i], w) if i < len(col) else ""
            cells.append(text + " " * max(0, w - ui.visible_len(text)))
        out.append(sep.join(cells))
    return out


def join_cols(left: list, right: list, left_w: int, right_w: int, on: bool) -> list:
    out = []
    for i in range(max(len(left), len(right))):
        a = ui.fit(left[i], left_w) if i < len(left) else ""
        b = ui.fit(right[i], right_w) if i < len(right) else ""
        out.append(a + " " * max(0, left_w - ui.visible_len(a)) + ui.paint(" │ ", ui.GREY, on) + b)
    return out


@dataclass
class UiState:
    selected: int = 0
    scroll: int = 0
    interval: float = 5
    tab: int = 0
    paused: bool = False
    detail_page: bool = False                # tier 1: Enter shows the detail instead of the table
    sort: int = 0                            # index in SORTS (key o)
    preset: int = 0                          # index in PRESETS (key f)
    view: int = 0                            # index in VIEWS (key v)
    filter: str = ""                         # text typed after / (a part of the node name)
    typing: bool = False                     # the filter is being typed
    overlay: str = ""                        # "" | "graph" (g) | "pods" (l) | "help" (?)
    graph: GraphState = field(default_factory=GraphState)             # window, cursor, view of the graph page
    pod_i: int = 0
    pod_log: bool = False
    log_lines: list = field(default_factory=list)
    log_note: str = ""


TABS = ("overview", "pods", "events")
SORTS = ("default", "temp", "cpu", "ram", "pods", "gpu")
PRESETS = ("all", "problems", "workers", "gpu")
VIEWS = ("overview", "temps", "net", "disks", "gpu")
_DESC = lambda value: (value is None, -(value or 0))                                  # noqa: E731 - biggest first, unknown last
SORT_KEYS = {"temp": lambda n: _DESC(n.temp), "cpu": lambda n: _DESC(n.cpu_pct), "ram": lambda n: _DESC(n.mem_pct),
             "pods": lambda n: (-n.pods_bad, -n.restarts), "gpu": lambda n: _DESC(n.gpu.temp if n.gpu is not None else None)}


def has_problem(n: NodeView) -> bool:
    return (not n.ready or bool(n.pressure) or n.pods_bad > 0 or (n.temp is not None and n.temp >= 80)
            or (n.gpu is not None and n.gpu.temp is not None and n.gpu.temp >= 80))


def shown_nodes(view: ClusterView, state: UiState) -> list:
    """The nodes as the table shows them: the text filter, the preset (all / problems / workers / gpu), then the sort."""
    nodes = list(view.nodes)
    text = state.filter.strip().lower()
    if text:
        nodes = [n for n in nodes if text in n.name.lower()]
    preset = PRESETS[state.preset % len(PRESETS)]
    if preset == "problems":
        nodes = [n for n in nodes if has_problem(n)]
    elif preset == "workers":
        nodes = [n for n in nodes if not n.master]
    elif preset == "gpu":
        nodes = [n for n in nodes if n.gpu is not None or n.gpu_alloc > 0]
    key = SORT_KEYS.get(SORTS[state.sort % len(SORTS)])
    if key is not None:
        nodes.sort(key=key)
    return nodes


def view_line(state: UiState, on: bool) -> str:
    """What is changed from the default, one line under the title (empty = nothing)."""
    bits = []
    if state.sort:
        bits.append(f"sort: {SORTS[state.sort % len(SORTS)]}")
    if state.preset:
        bits.append(f"show: {PRESETS[state.preset % len(PRESETS)]}")
    if state.view:
        bits.append(f"view: {VIEWS[state.view % len(VIEWS)]}")
    if state.filter or state.typing:
        bits.append(f"filter: “{state.filter}{'▌' if state.typing else ''}”")
    return ui.paint("  ".join(bits) + ("   (c = clear)" if bits and not state.typing else ""), ui.CYAN, on) if bits else ""


def _nearest(interval: float) -> int:
    """Index of the interval in INTERVALS (the nearest one)."""
    return min(range(len(INTERVALS)), key=lambda i: abs(INTERVALS[i] - interval))


_POD_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def pod_ref(pod: dict) -> Optional[tuple]:
    """(namespace, name) of a pod only when both are real Kubernetes names (they come from the cluster: never passed on unchecked)."""
    ns, name = pod.get("ns", ""), pod.get("name", "")
    return (ns, name) if _POD_NAME.match(ns or "") and _POD_NAME.match(name or "") else None


def _help_rows(on: bool) -> list:
    rows = [("↑ ↓  PgUp PgDn  Home End", "select a node"), ("Tab", "next tab of the detail (overview · pods · events); in a wide window the pods / events panel"),
            ("Enter", "narrow window: the detail page of the node"), ("o", "sort: default · temp · cpu · ram · pods · gpu"),
            ("/", "filter by a part of the node name (Enter keeps it, Esc clears it)"), ("f", "show: all · problems · workers · gpu"),
            ("v", "view of the table: overview · temps · net · disks · gpu"), ("c", "clear sort, filter, show and view"),
            ("g", "graphs: the history of the selected node. Inside: [ ] window · ← → time · , . cursor · b Braille dots · a area · t save as text · s save CSV+JSON (in scr/graphs/) · x combined"),
            ("  x (in graphs)", "all lines in ONE chart, each scaled 0-100 % of its own range - to see what rises or falls at the same time (the numbers are in the legend)"),
            ("l", "pods of the selected node, Enter = the end of the log of the pod (read only)"),
            ("e", "SCREEN: saves what you see as a text file to the folder scr/"), ("+ -", "refresh interval 0.5 · 1 · 2 · 5 · 10 · 30 s"),
            ("p", "pause"), ("r", "refresh now"), ("?", "this help"), ("q / Esc", "back (closes a page first)")]
    return rows


def _help_lines(on: bool, room: int) -> list:
    out = []
    for key, text in _help_rows(on):
        parts = ui.wrap(text, max(12, room - 26), "", "") or [""]
        out.append(f"{ui.paint(key.ljust(24), ui.BOLD + ';' + ui.YELLOW, on)} {parts[0]}")
        out += [" " * 25 + part for part in parts[1:]]
    return out


def render_overlay(view: ClusterView, state: UiState, nodes: list, columns: int, lines: int, on: bool) -> list:
    """The full-screen pages: graphs (g), pods and a pod log (l), help (?)."""
    width = max(30, columns)
    room = width - 4
    sel = nodes[state.selected] if nodes else None
    if state.overlay == "help":
        return ui.box("📊 DASHBOARD · keys", [_help_lines(on, room),
                                              [ui.paint("any key = back", ui.GREY, on)]], on, width)
    if sel is None:
        return ui.box("📊 DASHBOARD", [[ui.paint("no node is selected (c clears the filter)", ui.GREY, on)], [ui.paint("q back", ui.GREY, on)]], on, width)
    if state.overlay == "graph":
        title, sections = graph.page(sel, state.graph, state.interval, columns, lines, on)
        return ui.box(title, sections, on, width)
    pods = sorted(sel.pods, key=lambda p: (p["ns"], p["name"]))
    state.pod_i = max(0, min(state.pod_i, len(pods) - 1)) if pods else 0
    if state.pod_log and pods:
        pod = pods[state.pod_i]
        keep = max(5, lines - 9)
        body = [ui.paint(f"▸ {pod['ns']}/{pod['name']} · {pod['phase']} · restarts {pod['restarts']}", ui.BOLD, on)]
        text = state.log_lines[-keep:] if state.log_lines else [ui.paint(state.log_note or "reading the log…", ui.GREY, on)]
        return ui.box(f"📜 {sel.name} · pod log", [body, [clean_text(x, 400) for x in text],
                      [ui.paint("Enter/Esc back to the pods   ↑↓ other pod   q back", ui.GREY, on)]], on, width)
    keep = max(4, lines - 8)
    first = max(0, min(state.pod_i - keep // 2, max(0, len(pods) - keep)))
    rows = [ui.paint(f"  {'NAMESPACE':<16}{'POD':<40}{'PHASE':<18}RESTARTS", ui.GREY, on)]
    for i, p in enumerate(pods[first:first + keep], first):
        mark = ui.paint("▸", ui.YELLOW, on) if i == state.pod_i else " "
        phase = ui.paint(p["phase"].ljust(18), ui.GREEN if p["phase"] == "Running" else ui.YELLOW, on)
        rows.append(f"{mark} {ui.fit(p['ns'], 15).ljust(16)}{ui.fit(p['name'], 39).ljust(40)}{phase}{p['restarts']}")
    if not pods:
        rows.append(ui.paint("  no pod on this node", ui.GREY, on))
    return ui.box(f"📜 {sel.name} · pods ({len(pods)})", [rows, [ui.paint("↑↓ pod   Enter = log   q back", ui.GREY, on)]], on, width)


def render(view: ClusterView, state: UiState, columns: int, lines: int, on: bool, status: str = "") -> list:
    """The whole screen as lines (pure: easy to test for every width and height)."""
    t = tier(columns)
    width = max(30, columns)
    nodes = shown_nodes(view, state)
    state.selected = max(0, min(state.selected, len(nodes) - 1)) if nodes else 0
    if state.overlay:
        return render_overlay(view, state, nodes, columns, lines, on)
    ready = sum(1 for n in view.nodes if n.ready)
    count = f"{len(nodes)}/{len(view.nodes)}" if len(nodes) != len(view.nodes) else str(len(view.nodes))
    title = (f"📊 {'CLUSTER DASHBOARD' if width >= 72 else 'DASHBOARD'} · {count} node{'s' if len(view.nodes) != 1 else ''} · {ready} Ready"
             + (f" · {ui.paint('PAUSED', ui.YELLOW, on)}" if state.paused else "") + f" · {state.interval:g} s · {time.strftime('%H:%M:%S')}")
    head = [*_alerts(view, on), *_tests_rows(view, on)[:1]] if t < 3 else [*_alerts(view, on)]
    if view.error:
        head.append(ui.paint(f"⚠️ kubectl: {view.error}", ui.RED, on))
    if status:
        head.append(ui.paint(status, ui.GREY, on))
    if view_line(state, on):
        head.append(view_line(state, on))
    keys = ui.paint("↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back" if width >= 100 else
                    "↑↓  Tab  o  /  f  v  g  l  e  ?  q" if t > 1 else "↑↓ Enter o / f v g l e ? q", ui.GREY, on)
    if t == 1 and state.detail_page and nodes:                      # compact: the detail of the node instead of the table
        n = nodes[state.selected]
        tab = TABS[state.tab % len(TABS)]
        if tab == "events":
            body = [[f"{ui.paint(n.name, ui.BOLD, on)}  · cluster events (Tab)"], [*events_rows(view, on, max(3, lines - 12), width)]]
        elif tab == "pods":
            body = [[f"{ui.paint(n.name, ui.BOLD, on)}  · pods (Tab)"], [*pods_rows(n, on, max(3, lines - 12))]]
        else:
            body = [[f"{ui.paint(n.name, ui.BOLD, on)}  {'master' if n.master else 'worker'}  (Tab: pods, events)", *detail_rows(n, on)],
                    [*pods_rows(n, on, max(3, lines - 22))]]
        return ui.box(title, [*([head] if head else []), *body, [ui.paint("Enter/Esc back to the table   q back", ui.GREY, on)]], on, width)
    cols = Columns(t, width, on, VIEWS[state.view % len(VIEWS)])
    # vertical budget: frame (4) + head + header row + keys + detail
    detail_h = {1: 0, 2: 12, 3: 0}[t]
    avail = max(3, lines - 8 - len(head) - detail_h - (len(cluster_rows(view, on)) if t == 3 else 0))
    state.scroll = max(0, min(state.scroll, max(0, state.selected - avail + 1) if state.selected >= avail else 0, max(0, len(nodes) - avail)))
    if state.selected < state.scroll:
        state.scroll = state.selected
    if state.selected >= state.scroll + avail:
        state.scroll = state.selected - avail + 1
    shown = nodes[state.scroll:state.scroll + avail]
    table = [cols.header(), *[cols.row(n, state.scroll + i == state.selected) for i, n in enumerate(shown)]]
    if len(nodes) > avail:
        table.append(ui.paint(f"  {state.scroll + 1}-{state.scroll + len(shown)} of {len(nodes)}  (↑↓ PgUp PgDn)", ui.GREY, on))
    if not nodes:
        table.append(ui.paint("  no node matches the filter (c clears it)" if view.nodes else "  waiting for the first data…", ui.GREY, on))
    sections = [*([head] if head else []), table]
    sel = nodes[state.selected] if nodes else None
    if t == 2 and sel:
        tab = TABS[state.tab % len(TABS)]
        if tab == "overview":
            body = [f"{ui.paint('▸ ' + sel.name, ui.BOLD, on)}", *detail_rows(sel, on)][:detail_h]
        elif tab == "pods":
            body = [f"{ui.paint('▸ ' + sel.name + ' · pods', ui.BOLD, on)}", *pods_rows(sel, on, detail_h - 1)]
        else:
            body = [f"{ui.paint('▸ cluster events (warnings)', ui.BOLD, on)}", *events_rows(view, on, detail_h - 1, width)]
        sections.append(body)
    if t == 3 and sel:
        room = width - 4
        ncols = 3 if width >= 230 else 2
        widths = [(room - 3 * (ncols - 1)) // ncols] * ncols
        widths[0] += room - 3 * (ncols - 1) - sum(widths)
        panel_h = max(8, lines - 8 - len(head) - len(table))
        pods_h = max(5, panel_h // 3)
        hw, k8s = block_hw(sel, on), block_k8s(sel, on)
        tab = TABS[state.tab % len(TABS)]
        pods, cl = block_pods(sel, on, pods_h + (panel_h // 3 if tab == "pods" else 0)), block_cluster(view, on)
        if tab == "events":                                  # Tab: the events take the place of the pods
            pods = [ui.paint("▸ cluster events (warnings)", ui.BOLD, on), *events_rows(view, on, max(3, pods_h), width // 3)]
        prob, tests = block_problems(view, on, widths[-1]), block_tests(view, on)
        if ncols == 3:
            cols = [hw, [*k8s, "", *pods], [*cl, "", *prob, "", *tests]]
        else:
            cols = [[*hw, "", *k8s], [*pods, "", *cl, "", *prob, "", *tests]]
        sections.append(join_n([_fit_rows(c, panel_h, on) for c in cols], widths, on))
    if ui.height_mode(lines) == "tall" and nodes:
        _fill_down(view, state, t, sections, [keys], title, lines, width, on, nodes)
    sections.append([keys])
    return ui.box(title, sections, on, width)


def _fill_down(view: ClusterView, state: UiState, t: int, sections: list, tail: list, title: str, lines: int, width: int, on: bool,
               nodes: Optional[list] = None) -> None:
    """A tall window (>= 40 lines): everything that still fits is opened under the table instead of being scrolled to -
    the selected node and its pods, the cluster totals, problems, tests, then the other nodes one after another, the events last.
    Each block is added whole or not at all (the last one may be shortened), so the screen is never cut in the middle of a block."""
    nodes = view.nodes if nodes is None else nodes
    sel = nodes[state.selected]
    room = width - 4

    def left() -> int:
        return lines - 1 - len(ui.box(title, [*sections, tail], on, width))

    def add(block: list, min_rows: int = 0) -> bool:
        if t == 1:                                           # a narrow window: long rows are wrapped, nothing is cut with an ellipsis
            block = [part for row in block for part in (ui.wrap(row, room, "", "  ") if ui.visible_len(row) > room else [row])]
        space = left() - 1
        if not block or space < max(1, min_rows):
            return False
        sections.append(block[:space] if len(block) > space else block)
        return len(block) <= space

    if t == 1:
        add([ui.paint("▸ " + sel.name, ui.BOLD, on), *detail_rows(sel, on)])
        add([ui.paint("▸ pods · " + sel.name, ui.BOLD, on), *pods_rows(sel, on, max(3, min(12, left() - 3)))], 4)
    elif t == 2 and TABS[state.tab % len(TABS)] != "pods":
        add([ui.paint("▸ pods · " + sel.name, ui.BOLD, on), *pods_rows(sel, on, max(3, min(12, left() - 3)))], 4)
    if t < 3:
        add(block_cluster(view, on), 4)
        add(block_problems(view, on, room), 3)
        add(block_tests(view, on), 3)
    for other in nodes:
        if other is sel:
            continue
        if not add([ui.paint("▸ " + other.name, ui.BOLD, on), *detail_rows(other, on)], 4):
            break
    if t < 3 or TABS[state.tab % len(TABS)] != "events":
        add([ui.paint("▸ cluster events (warnings)", ui.BOLD, on), *events_rows(view, on, max(3, left() - 3), width)], 4)


# ================================================================ the screen (keys, redraw) =============================
def read_key(stream_fd: int, timeout: float) -> Optional[str]:
    """One key (arrows and paging as names) within `timeout` s, None when nothing was pressed."""
    ready, _, _ = select.select([stream_fd], [], [], timeout)
    if not ready:
        return None
    data = os.read(stream_fd, 8)
    text = data.decode("utf-8", "ignore")
    names = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
             "\x1b[H": "home", "\x1b[F": "end", "\x1b[1~": "home", "\x1b[4~": "end", "\x1bOH": "home", "\x1bOF": "end",
             "\t": "tab", "\r": "enter", "\n": "enter", "\x1b": "esc", "\x03": "q"}
    return names.get(text, text[:1])


class Dashboard:
    """The live screen: a collector thread fills the data, the loop draws it and reacts to keys."""

    def __init__(self, kube: Kubectl, only: Optional[list] = None, stream=None, keys: Optional[Callable] = None) -> None:
        self.kube = kube
        self.collector = Collector(kube, only)
        self.stream = stream or sys.stdout
        self.state = UiState()
        self.canvas = ui.Canvas(self.stream)
        self._keys = keys                            # injected key source for tests: callable(timeout) -> key | None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = ""
        self._status_until = 0.0
        self._frame: list = []                       # the last drawn screen (key e saves it)
        self._frame_size = (0, 0)
        self._log_at = 0.0

    def _size(self) -> tuple:
        try:
            s = os.get_terminal_size(self.stream.fileno())
            return s.columns, s.lines
        except (OSError, ValueError, AttributeError):
            return 100, 40

    def _loop_data(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            if not self.state.paused:
                try:
                    self.collector.refresh()
                except Exception:                                  # noqa: BLE001 - one bad round must not kill the screen
                    log.exception("dashboard refresh failed")
            self._wake.wait(max(0.05, self.state.interval - (time.monotonic() - started)))     # a round takes time: the interval is start to start
            self._wake.clear()

    def _say(self, text: str, seconds: float = 6.0) -> None:
        self.status, self._status_until = text, time.monotonic() + seconds

    def _fetch_log(self, nodes: list) -> None:
        """The end of the log of the selected pod (read only, at most every 2 s, only while the log page is open)."""
        s = self.state
        if not (s.overlay == "pods" and s.pod_log and nodes) or time.monotonic() - self._log_at < 2.0:
            return
        pods = sorted(nodes[min(s.selected, len(nodes) - 1)].pods, key=lambda p: (p["ns"], p["name"]))
        if not pods:
            return
        self._log_at = time.monotonic()
        ref = pod_ref(pods[min(s.pod_i, len(pods) - 1)])
        if ref is None:
            s.log_lines, s.log_note = [], "the pod name is not a valid Kubernetes name - not read"
            return
        try:
            text = self.kube.run("logs", "-n", ref[0], ref[1], "--tail=200", "--all-containers=true", check=False, timeout=10)
        except KubectlError as exc:
            s.log_lines, s.log_note = [], f"the log cannot be read: {clean_text(str(exc), 120)}"
            return
        s.log_lines = [clean_text(x, 400) for x in text.splitlines()]
        s.log_note = "" if s.log_lines else "the pod has no log yet (or it is not started)"

    def save_graph_data(self) -> None:
        """Key s on the graph page: every kept sample of the node as CSV and JSON in scr/."""
        nodes = shown_nodes(self.collector.view, self.state)
        if not nodes:
            return
        n = nodes[min(self.state.selected, len(nodes) - 1)]
        rows = graph.export_rows(n)
        if not rows:
            self._say("nothing to save yet - the history is empty")
            return
        written = ui.save_files(f"graph-{n.name}", {".csv": graph.to_csv(rows), ".json": graph.to_json(n.name, self.state.interval, rows)}, "graphs")
        self._say(f"💾 {len(rows)} samples saved: scr/graphs/{written[0].name} (+ .json)" if written
                  else "❌ the data could not be saved (scr/graphs is not writable)")

    def save_graph_text(self) -> None:
        """Key t on the graph page: all the graphs of the node as a text file (cat / less / batcat) in scr/graphs/."""
        nodes = shown_nodes(self.collector.view, self.state)
        if not nodes:
            return
        n = nodes[min(self.state.selected, len(nodes) - 1)]
        if not n.h_t:
            self._say("nothing to save yet - the history is empty")
            return
        written = ui.save_files(f"graph-{n.name}", {".txt": graph.text_report(n, self.state.graph, self.state.interval)}, "graphs")
        self._say(f"📝 graphs saved as text: scr/graphs/{written[0].name}" if written else "❌ the text could not be saved (scr/graphs is not writable)")

    def draw(self) -> None:
        columns, lines = self._size()
        on = ui.color_enabled()
        if self.status and time.monotonic() > self._status_until:
            self.status = ""
        nodes = shown_nodes(self.collector.view, self.state)
        self._fetch_log(nodes)
        frame = render(self.collector.view, self.state, columns, lines, on, self.status)[:max(10, lines - 1)]
        self._frame, self._frame_size = frame, (columns, lines)
        self.canvas.draw(frame)

    def save_screen(self) -> None:
        """Key e: the screen exactly as drawn -> scr/dashboard-<time>-<columns>x<rows>.txt (plain text)."""
        if not self._frame:
            return
        written = ui.save_screen(self._frame, "dashboard", *self._frame_size)
        self._say(f"📸 screen saved: {written[0].parent.name}/{written[0].name}" if written else "❌ the screen could not be saved (scr/ is not writable)")

    def handle(self, key: str) -> bool:
        """Applies a key; False = leave the dashboard."""
        s = self.state
        nodes = shown_nodes(self.collector.view, s)
        if s.typing:                                                      # the filter is being typed: every key is text
            if key == "enter":
                s.typing = False
            elif key == "esc":
                s.typing, s.filter = False, ""
            elif key in ("\x7f", "\x08"):
                s.filter = s.filter[:-1]
            elif len(key) == 1 and key.isprintable() and len(s.filter) < 30:
                s.filter += key
            s.selected = 0
            return True
        if s.overlay:
            return self._overlay_key(key, nodes)
        key = key.lower() if len(key) == 1 else key                      # q Q, o O ...: both cases work
        name = nodes[s.selected].name if nodes and s.selected < len(nodes) else ""

        def keep_selected() -> None:                                       # after a sort / filter the same node stays selected
            after = shown_nodes(self.collector.view, s)
            s.selected = next((i for i, n in enumerate(after) if n.name == name), 0)

        if key in ("q", "esc"):
            if s.detail_page:
                s.detail_page = False
                return True
            return False
        if key == "up":
            s.selected = max(0, s.selected - 1)
        elif key == "down":
            s.selected = min(max(0, len(nodes) - 1), s.selected + 1)
        elif key == "pgup":
            s.selected = max(0, s.selected - 5)
        elif key == "pgdn":
            s.selected = min(max(0, len(nodes) - 1), s.selected + 5)
        elif key == "home":
            s.selected = 0
        elif key == "end":
            s.selected = max(0, len(nodes) - 1)
        elif key == "tab":
            s.tab = (s.tab + 1) % len(TABS)
        elif key == "enter":
            s.detail_page = not s.detail_page
        elif key in ("+", "="):
            s.interval = INTERVALS[min(len(INTERVALS) - 1, _nearest(s.interval) + 1)]
        elif key in ("-", "_"):
            s.interval = INTERVALS[max(0, _nearest(s.interval) - 1)]
        elif key == "p":
            s.paused = not s.paused
        elif key == "r":
            self._wake.set()
        elif key == "o":
            s.sort = (s.sort + 1) % len(SORTS)
            keep_selected()
        elif key == "f":
            s.preset = (s.preset + 1) % len(PRESETS)
            keep_selected()
        elif key == "v":
            s.view = (s.view + 1) % len(VIEWS)
        elif key == "/":
            s.typing = True
        elif key == "c":
            s.sort = s.preset = s.view = 0
            s.filter = ""
            keep_selected()
        elif key == "g" and nodes:
            s.overlay = "graph"
        elif key == "l" and nodes:
            s.overlay, s.pod_i, s.pod_log = "pods", 0, False
        elif key == "e":
            self.save_screen()
        elif key == "?":
            s.overlay = "help"
        return True

    def _graph_key(self, key: str, nodes: list) -> None:
        """Keys of the graph page: node, window, time, cursor, view, saving the data."""
        s, g = self.state, self.state.graph
        if key in ("up", "down"):
            s.selected = max(0, min(max(0, len(nodes) - 1), s.selected + (1 if key == "down" else -1)))
        elif key == "[":
            g.window = max(0, g.window - 1)
            g.offset = 0
        elif key == "]":
            g.window = min(len(graph.WINDOWS) - 1, g.window + 1)
            g.offset = 0
        elif key in ("left", "right", "end", "home") and graph.WINDOWS[g.window]:
            step = max(1, graph.WINDOWS[g.window] // 4)
            g.offset = 0 if key == "end" else max(0, g.offset + step if key in ("left", "home") else g.offset - step)
        elif key in (",", "."):
            columns = self._size()[0] - 4 - 6
            g.cursor = max(0, columns - 1) if g.cursor is None else g.cursor + (-1 if key == "," else 1)
            g.cursor = max(0, min(g.cursor, max(0, columns - 1)))
        elif key == "b":
            g.braille = not g.braille
        elif key == "x":
            g.combined = not g.combined
        elif key == "a":
            g.fill = not g.fill
        elif key == "c":
            g.cursor = None
        elif key == "s":
            self.save_graph_data()
        elif key == "t":
            self.save_graph_text()

    def _overlay_key(self, key: str, nodes: list) -> bool:
        """Keys of the full-screen pages (graphs, pods and the pod log, help)."""
        s = self.state
        key = key.lower() if len(key) == 1 else key
        if s.overlay == "help":
            s.overlay = ""
            return True
        if key in ("q", "esc") or (key == "enter" and s.overlay == "graph"):
            if s.overlay == "pods" and s.pod_log:
                s.pod_log = False
            else:
                s.overlay = ""
            return True
        if key == "e":
            self.save_screen()
        elif key in ("+", "="):
            s.interval = INTERVALS[min(len(INTERVALS) - 1, _nearest(s.interval) + 1)]
        elif key in ("-", "_"):
            s.interval = INTERVALS[max(0, _nearest(s.interval) - 1)]
        elif s.overlay == "graph":
            self._graph_key(key, nodes)
        elif s.overlay == "pods":
            pods = nodes[s.selected].pods if nodes and s.selected < len(nodes) else []
            if key in ("up", "down", "pgup", "pgdn", "home", "end"):
                step = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8}.get(key, 0)
                s.pod_i = 0 if key == "home" else len(pods) - 1 if key == "end" else max(0, min(max(0, len(pods) - 1), s.pod_i + step))
                s.log_lines, s.log_note = [], ""
                self._log_at = 0.0
            elif key == "enter" and pods:
                s.pod_log = not s.pod_log
                s.log_lines, s.log_note = [], ""
                self._log_at = 0.0
        return True

    def run(self) -> int:
        interactive = self._keys is None
        old = None
        if interactive:
            import termios
            import tty
            try:
                old = termios.tcgetattr(0)
                tty.setcbreak(0)
            except (termios.error, OSError, ValueError):
                ui.emit("❌ The dashboard needs a terminal.")
                return 1
        resized = threading.Event()
        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, lambda *_a: resized.set())
        try:
            self.stream.write("\x1b[?25l")
            ui.emit(f"{ui.ICON_STAGE} Starting the probes on the nodes (read-only, removed when you leave)…")
            self.collector.start(lambda text: ui.emit("  " + text))
            self._thread = threading.Thread(target=self._loop_data, daemon=True)
            self._thread.start()
            while True:
                self.draw()
                wait = min(1.0, max(0.25, self.state.interval / 2))        # fast intervals redraw more often
                key = self._keys(wait) if self._keys else read_key(0, wait)
                if key is None:
                    continue
                if not self.handle(key):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self._wake.set()
            self.stream.write("\x1b[?25h")
            self.canvas.reset()
            self.stream.flush()
            ui.emit("\n🧹 Removing the probe pods…")
            self.collector.stop()
            if old is not None:
                import termios
                termios.tcsetattr(0, termios.TCSADRAIN, old)
        return 0


def run(only: Optional[list] = None) -> int:
    """`--dashboard` / menu key D."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        ui.emit("❌ The dashboard needs a terminal (stdin and stdout).")
        return 1
    return Dashboard(Kubectl(), only).run()
