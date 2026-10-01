"""Course of one test: hardware -> probe -> stress-ng -> monitor -> cleanup."""
from __future__ import annotations

import logging
import os
import re
import signal
import time
from typing import Callable, Optional

from . import baseline
from . import ui
from .export import write_exports
from .kube import Kubectl, KubectlError
from .logparse import read_log
from .disk import jobs as disk_jobs, parse_fio_json
from . import net as netmod
from . import gpu as gpumod
from .smart import parse_smart_output
from .manifests import (FAILED_MARKER, GATE_FILE, READY_MARKER, net_service, PROBE_SCRIPT, STARTED_MARKER,
                        build_stress_command, hw_pod, probe_pod, stress_memory_limit_mib,
                        net_server_pod, smart_pod, stress_pod)
from .models import (MAX_NODE_BUSY_PCT, SPIKE_LOW_PCT, WARN_TEMP, NodeInfo, PodNames,
                     ProbeData, StressConfig)
from .monitor import Monitor, OverheatGuard
from .parsing import (STAGE_RE, calc_ram_target_mib, count_completed_runs,
                      describe_duration, describe_steps, format_duration,
                      clean_block, clean_text, cpu_summary, parse_pct, parse_probe_output, parse_stressng_outcome, parse_top,
                      split_stage_lines)
from .paths import log_root_of, open_private
from .summary import (RunStats, StressMetric, build_summary, parse_stressng_metrics,
                      run_stats)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_OVERHEAT = 3
EXIT_PREMATURE = 4      # the test ended earlier than it should (the pod disappeared/was terminated)
EXIT_INTERRUPTED = 130

# preparation of a node: (key, short label, typical seconds) - the bar creeps within a step by time
PREP_STEPS = (("check", "checking", 5), ("probe", "probe pod", 15), ("smart", "SMART", 20),
              ("hw", "hardware", 25), ("pod", "pod start", 20), ("install", "installing", 60),
              ("ready", "waiting", 10))


def prep_fraction(base: float, share: float, typical: float, spent: float) -> float:
    """Progress of the preparation: finished steps + a part of the running one (by time, never 100 % of it)."""
    return min(1.0, base + share * min(0.95, spent / typical if typical > 0 else 0.0))


GATE_TIMEOUT = 900          # s how long a ready node waits for the others (synchronised start)
POD_DEADLINE_MARGIN = 600   # s extra for pulling the image and apt install
GPU_DEADLINE_EXTRA = 1200   # s more for the GPU test: first pull of the ~3 GB CUDA image + building gpu-burn


class RunnerError(RuntimeError):
    """The test could not be prepared or started."""


class StressRunner:
    def __init__(self, kube: Kubectl, node: NodeInfo, cfg: StressConfig,
                 ram_target_mib: Optional[int] = None,
                 log_path: Optional[str] = None,
                 interval: float = 5.0,
                 remaining_every: int = 3,
                 allow_no_sensor: bool = False,
                 confirm_no_sensor: Optional[Callable[[], bool]] = None,
                 out: Callable[[str], None] = print,
                 names: Optional[PodNames] = None,
                 hw_privileged: bool = False,
                 skip_hw: bool = False,
                 concurrent: int = 1,
                 max_busy_pct: int = MAX_NODE_BUSY_PCT,
                 allow_busy_node: bool = False,
                 skip_capacity_check: bool = False,
                 start_gate: str = "",
                 gate_timeout: int = GATE_TIMEOUT,
                 gate_wait: bool = True,
                 gpu_board=None) -> None:
        self.gpu_board = gpu_board                  # multi-node GPU test: the shared table (see gpuscan.GpuBoard)
        self.gpu_summary = None                     # GpuSummary of the finished GPU test
        self.kube = kube
        self.node = node
        self.cfg = cfg
        self.ram_target_mib = ram_target_mib
        self.log_path = log_path
        self.interval = interval
        self.remaining_every = remaining_every
        self.allow_no_sensor = allow_no_sensor
        self.confirm_no_sensor = confirm_no_sensor
        self._raw_out = out
        self._screen: Optional[ui.LiveScreen] = None   # live in-place view of the running test (terminal only)
        self.names = names or PodNames.new()      # unique pod names of this run
        self.hw_privileged = hw_privileged
        self.skip_hw = skip_hw
        self.concurrent = concurrent                # how many nodes are tested at once (1 = only this one)
        self.max_busy_pct = max_busy_pct
        self.allow_busy_node = allow_busy_node
        self.skip_capacity_check = skip_capacity_check
        self.stats: Optional[RunStats] = None      # test indicators (for the series summary)
        self.metrics: list[StressMetric] = []      # stress-ng performance from the output
        self._phase = "test"                       # "test" or "cooldown" (for printing only)
        self._baseline: Optional[ProbeData] = None  # idle values before the test
        self._service_name = ""                     # network test: the Service in front of the iperf3 server (extra "service")
        self._service_ip = ""
        self._watch_ip = ""                         # --net-watch: address pinged during the test
        self._watch_name = ""
        self.net_results: list = []                 # NetResult of the network test
        self.net_peer: Optional[NodeInfo] = None
        self.net_target = ""
        self.net_port = netmod.PORT_BASE
        self.disk_results: list = []                # fio results of the disk benchmark
        self._smart_lines: list[str] = []           # disk health for the log header (--smart)
        self._prep_key = ""                         # the running preparation step
        self._prep_at = time.monotonic()
        self._deferred: list[str] = []              # long texts (hardware) printed after the live screen is closed
        self.start_gate = start_gate                # parallel test: path prefix of the <gate>.ready / <gate>.go files
        self.gate_timeout = gate_timeout
        self.gate_wait = bool(start_gate) and gate_wait   # False = rolling start: the load starts as soon as this node is ready
        self.deadline = (cfg.total_duration + POD_DEADLINE_MARGIN + (GPU_DEADLINE_EXTRA if cfg.gpu else 0)
                         + (gate_timeout if self.gate_wait else 0))

    # --- output and log -------------------------------------------------------
    def out(self, line: str) -> None:
        if self._screen:
            log.info("OUT %s", line)
            if line.count("\n") > 3:                 # a long text (hardware): shown after the screen is closed
                self._deferred.append(line)
                line = line.split("\n", 1)[0] + " (full text below after the test)"
            self._screen.event(line)
        else:
            self._raw_out(line)

    def _prep_steps(self) -> list:
        return [st for st in PREP_STEPS
                if (st[0] != "smart" or self.cfg.smart) and (st[0] != "ready" or self.gate_wait)]

    def _prep_state(self) -> tuple:
        """(fraction, label, 'k/N') of the running preparation step."""
        steps = self._prep_steps()
        total = sum(w for _k, _l, w in steps)
        done = 0.0
        for index, (key, label, weight) in enumerate(steps, 1):
            if key == self._prep_key:
                fraction = prep_fraction(done / total, weight / total, weight, time.monotonic() - self._prep_at)
                return fraction, label, f"{index}/{len(steps)}"
            done += weight
        return (0.0 if not self._prep_key else 1.0), "starting", f"{'0' if not self._prep_key else len(steps)}/{len(steps)}"

    def _step(self, key: str) -> None:
        """A new preparation step: for the bar of the screen and (parallel test) for the parent's table."""
        self._prep_key, self._prep_at = key, time.monotonic()
        if not self.start_gate:
            return
        steps = self._prep_steps()
        total = sum(w for _k, _l, w in steps)
        done = 0.0
        for _index, (k, label, weight) in enumerate(steps, 1):
            if k == key:
                try:
                    with open_private(self.start_gate + ".prep", "w") as fh:
                        fh.write(f"{done / total:.4f} {weight / total:.4f} {weight} {time.time():.1f} {label}\n")
                except OSError:
                    pass
                return
            done += weight

    def _mark_started(self) -> None:
        """Parallel test: the moment this node's own load started (the parent draws the node's bar from it)."""
        if self.start_gate:
            try:
                with open_private(self.start_gate + ".started", "w") as fh:
                    fh.write(f"{time.time():.1f}\n")
            except OSError:
                pass

    def _open_screen(self) -> None:
        if self.concurrent <= 1 and ui.LiveScreen.wanted():
            if self.cfg.gpu:
                self._screen = ui.GpuScreen(self.node.name, total=self.cfg.total_duration,
                                            limit=self.cfg.gpu_max_temp, warn=self.cfg.gpu_max_temp - gpumod.GPU_WARN_MARGIN,
                                            board=self.gpu_board)
            else:
                self._screen = ui.LiveScreen(f"{self.node.name} · {describe_duration(self.cfg.duration)} test",
                                             total=self.cfg.total_duration)
            self._screen.begin_prep(self._prep_state)
            self._screen.start_ticker()

    def _close_screen(self) -> None:
        if self._screen:
            screen, self._screen = self._screen, None
            screen.close()
            for text in self._deferred:
                self._raw_out(text)
            self._deferred.clear()

    def _log(self, text: str) -> None:
        if self.log_path:
            with open_private(self.log_path, "a") as fh:      # permissions 0600
                fh.write(text + "\n")

    def _emit_measurement(self, line: str) -> None:
        prefix = "❄️  " if self._phase == "cooldown" else "⏳ "
        if self._screen:
            self._screen.reading(line, self._phase == "cooldown")
        else:
            self.out(prefix + line)
        self._log(("[cooldown] " if self._phase == "cooldown" else "") + line)

    def _emit_info(self, line: str) -> None:
        if self._screen and "⏱️" in line:
            self._screen.status(line)
        else:
            self.out(line)
        self._log(line)

    # --- steps ---------------------------------------------------------------
    def _collect_hardware(self) -> str:
        if self.skip_hw:
            return "Hardware detection skipped (--no-hw)."
        if self.hw_privileged:
            self.out("⚠️  Hardware is detected by a PRIVILEGED pod (--hw-privileged).")
        self.out("⏳ Detecting node hardware...")
        self.kube.apply(hw_pod(self.node.name, self.names, privileged=self.hw_privileged))
        phase = ""
        for _ in range(90):
            phase = self.kube.pod_phase(self.names.hw)
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        info = (clean_block(self.kube.logs(self.names.hw)).strip() if phase == "Succeeded"
                else f"Hardware detection failed (pod phase: {phase or 'unknown'}).")
        self.kube.delete_pods(self.names.hw)
        return info

    def _check_smart(self) -> None:
        """Preflight (--smart): disk health via a privileged pod; a failed disk refuses the test."""
        if not self.cfg.smart:
            return
        self.out("⚠️  Reading disk health (SMART) with a PRIVILEGED pod (--smart).")
        self.kube.apply(smart_pod(self.node.name, self.names))
        phase = ""
        for _ in range(90):
            phase = self.kube.pod_phase(self.names.hw)
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        text = self.kube.logs(self.names.hw) if phase == "Succeeded" else ""
        self.kube.delete_pods(self.names.hw)
        if phase != "Succeeded":
            self.out(f"⚠️  SMART check could not be done (pod phase: {phase or 'unknown'}), continuing.")
            return
        disks, note = parse_smart_output(text)
        if not disks:
            self.out(f"⚠️  SMART: {note}. Continuing.")
            self._smart_lines = [f"SMART unavailable: {note}"]
            return
        fatal = [f for d in disks for f in d.fatal]
        self._smart_lines = [d.line() for d in disks]
        for disk in disks:
            self.out(f"💽 {disk.line()}")
            for problem in disk.problems:
                self.out(f"⚠️  {problem}")
                self._smart_lines.append(f"WARNING {problem}")
        if fatal:
            msg = "; ".join(fatal)
            if self.cfg.allow_bad_disk:
                self.out(f"⚠️  {msg}. Continuing (--allow-bad-disk).")
                self._smart_lines += [f"FAILED {f}" for f in fatal]
                return
            raise RunnerError(f"{msg}. A load test could finish off the disk; "
                              f"use --allow-bad-disk to test anyway.")
        self.out("✅ Disk health OK (SMART).")

    def _check_gpu(self, hardware: str) -> None:
        """GPU test preflight: the node must offer nvidia.com/gpu (driver, toolkit, runtime and device plugin)."""
        state, message = gpumod.gpu_state(hardware, self.node.gpu_count, hw_skipped=self.skip_hw)
        if state != "ok":
            raise RunnerError(message)
        busy = self.kube.gpu_in_use(self.node.name)
        if busy and busy >= self.node.gpu_count:
            raise RunnerError(f"The GPU of {self.node.name} is already used by another pod ({busy}x nvidia.com/gpu).")
        self.out(f"🎮 GPU available: {self.node.gpu_count}x nvidia.com/gpu. Stop at {self.cfg.gpu_max_temp} °C, "
                 f"warning from {self.cfg.gpu_max_temp - gpumod.GPU_WARN_MARGIN} °C.")

    def _gpu_info(self) -> None:
        """Static data of the card (model, driver, memory, limits) - shown and written to the log before the load."""
        try:
            lines = gpumod.parse_gpu_info(self.kube.exec(self.names.stress, gpumod.INFO_QUERY, timeout=gpumod.SMI_TIMEOUT))
        except KubectlError as exc:
            log.warning("GPU info could not be read: %s", exc)
            return
        if not lines:
            return
        if isinstance(self._screen, ui.GpuScreen):
            self._screen.set_card(lines)
        self.out(f"{ui.ICON_GPU} GPU INFO:\n" + "\n".join(f"  {ln}" for ln in lines))
        self._log("=== GPU ===")
        for ln in lines:
            self._log(f"GPU {ln}" if not ln.startswith("GPU:") else ln.replace("GPU:", "GPU model:", 1))

    def _write_log_header(self, hardware: str) -> None:
        n, c = self.node, self.cfg
        self._log("=== KUBERNETES STRESS-NG LOG ===")
        self._log(f"Node: {n.name}")
        self._log(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"Test duration: {describe_duration(c.total_duration)}")
        if c.stepped:
            self._log(f"Profile: stepped {describe_steps(c.steps)} % for "
                      f"{describe_duration(c.step_time)}")
            self._log(f"CPU load: gradually {describe_steps(c.steps)} %")
        elif c.net:
            self._log(f"Profile: network - iperf3 + ping, {c.net_mode} network, {c.net_time} s per test")
            self._log("CPU load: none (network test)")
        elif c.gpu:
            self._log(f"Profile: gpu - gpu-burn {c.gpu_mem_pct} % of the GPU memory"
                      f"{', double precision' if c.gpu_double else ''}, image {c.gpu_image or gpumod.GPU_IMAGE_DEFAULT}")
            self._log(f"GPU temperature limit: {c.gpu_max_temp}°C (2 readings in a row), warning from {c.gpu_max_temp - gpumod.GPU_WARN_MARGIN}°C")
            self._log("CPU load: none (GPU test)")
        elif c.disk:
            self._log(f"Profile: disk - fio benchmark, {len(disk_jobs(c.disk_read_only))} jobs of "
                      f"{describe_duration(c.disk_job_time)}, {c.disk_size} MiB file"
                      f"{', read only' if c.disk_read_only else ''}")
            self._log("CPU load: none (disk benchmark)")
        elif c.spike:
            self._log(f"Profile: spike - {c.spike_cycles} cycles of "
                      f"{describe_duration(c.spike_low_time)} at {SPIKE_LOW_PCT} % / "
                      f"{describe_duration(c.spike_high_time)} at {c.spike_target} %")
            self._log(f"CPU load: {SPIKE_LOW_PCT} % <-> {c.spike_target} % "
                      f"(repeating jump, {c.spike_cycles}x)")
        else:
            ram = (f"{c.ram_pct} % of free memory (the target is determined after the probe starts)"
                   if c.ram_pct else "no")
            self._log(f"CPU load: {c.cpu_load}%  RAM: {ram}  disk: {'yes' if c.hdd else 'no'}")
        self._log(f"Automatic stop at: {c.max_temp}°C (2 readings in a row)")
        self._log(f"Cooldown after test: {c.cooldown} s")
        if self.concurrent > 1:
            self._log(f"Concurrency: yes ({self.concurrent} nodes at once)")
        self._log(f"Notes: {c.notes}")
        self._log("----------------------------------------------------")
        self._log(f"OS: {n.os_image} | Kernel: {n.kernel} | "
                  f"Arch: {n.architecture} | Runtime: {n.runtime}")
        self._log(f"CPU capacity: {n.capacity_cpu}  Allocatable RAM: {n.allocatable_mem_mib} MiB")
        if self._watch_ip:
            self._log(f"Network watch: {self._watch_name} ({self._watch_ip})")
        if self._baseline and self._baseline.pl1_w:
            pl2 = f", PL2 {self._baseline.pl2_w:.0f} W" if self._baseline.pl2_w else ""
            self._log(f"Power limits: PL1 {self._baseline.pl1_w:.0f} W{pl2}")
        self._log("=== HARDWARE ===")
        self._log(hardware)
        if self._smart_lines:
            self._log("=== DISK HEALTH (SMART) ===")
            for line in self._smart_lines:
                self._log(line)
        self._log("=== METRICS LOG (TIME | CPU/RAM | TEMPERATURES | CLOCK) ===")

    def _print_summary(self) -> None:
        c, n = self.cfg, self.node
        ram = (f"{c.ram_pct} % of free memory (determined exactly after the probe starts)"
               if c.ram_pct else "no")
        if c.stepped:
            load_rows = [
                ("Profile", f"stepped {describe_steps(c.steps)} % for "
                           f"{describe_duration(c.step_time)}"),
                ("Test duration", f"{describe_duration(c.duration)} in total"),
            ]
        elif c.net:
            load_rows = [
                ("Profile", f"network test (iperf3 + ping), {c.net_mode} network, {c.net_time} s per test"),
                ("Peer", c.net_peer or "chosen automatically"),
                ("Test duration", f"{describe_duration(c.duration)} in total (+ installing iperf3)"),
            ]
        elif c.gpu:
            load_rows = [
                ("Profile", f"GPU test (gpu-burn), {c.gpu_mem_pct} % of the GPU memory"
                           f"{', double precision' if c.gpu_double else ''}"),
                ("Test duration", f"{describe_duration(c.duration)} in total (+ preparing gpu-burn)"),
            ]
        elif c.disk:
            load_rows = [
                ("Profile", f"disk benchmark (fio) - {len(disk_jobs(c.disk_read_only))} jobs of "
                           f"{describe_duration(c.disk_job_time)}, {c.disk_size} MiB file"
                           f"{', read only' if c.disk_read_only else ''}"),
                ("Test duration", f"{describe_duration(c.duration)} in total (+ fio installation)"),
            ]
        elif c.spike:
            load_rows = [
                ("Profile", f"spike - {c.spike_cycles}x "
                           f"({describe_duration(c.spike_low_time)} at {SPIKE_LOW_PCT} % / "
                           f"{describe_duration(c.spike_high_time)} at {c.spike_target} %)"),
                ("Test duration", f"{describe_duration(c.duration)} in total"),
            ]
        else:
            load_rows = [
                ("Test duration", describe_duration(c.duration)),
                ("CPU load", f"{c.cpu_load} %"),
                ("RAM load", ram),
                ("Disk load", "yes" if c.hdd else "no"),
            ]
        rows = [
            ("Node", n.name + (" (master)" if n.is_control_plane else "")),
            *load_rows,
            ("Stop at", (f"GPU {c.gpu_max_temp} °C (warning from {c.gpu_max_temp - gpumod.GPU_WARN_MARGIN} °C) · "
                         f"CPU {c.max_temp} °C" if c.gpu else f"{c.max_temp} °C") + " (2 readings in a row)"),
            ("Cooldown after test", f"{c.cooldown} s of measuring" if c.cooldown else "off"),
            ("Log", self.log_path or "no"),
        ]
        self.out("-" * 52)
        self.out("TEST SETTINGS")
        for line in ui.kv_block(rows, label_w=21):       # the classic rows; two columns / wrapped for the terminal width
            self.out(line)
        self.out("  (preparing the pods before the load starts takes about 1–3 min extra)")
        self.out("-" * 52)

    def _start_probe(self) -> None:
        self.kube.apply(probe_pod(self.node.name, self.names, self.deadline))
        if not self.kube.wait_ready(self.names.probe, 60):
            raise RunnerError("The temperature probe (temp-probe) did not come up.")
        self._resolve_watch()

    def _resolve_watch(self) -> None:
        """--net-watch: which node is pinged during the test (auto = the master, or a worker when testing the master)."""
        wanted = self.cfg.net_watch
        if not wanted:
            return
        try:
            if wanted == "auto":
                others = [self.kube.get_node(n) for n in self.kube.list_node_names() if n != self.node.name]
                ready = sorted((n for n in others if n.ready and n.internal_ip),
                               key=lambda n: (not n.is_control_plane, n.name) if not self.node.is_control_plane
                               else (n.is_control_plane, n.name))
                target = ready[0] if ready else None
            elif wanted == self.node.name:
                self.out("⚠️  --net-watch: the watched node is the tested node itself, ignoring.")
                return
            else:
                target = self.kube.get_node(wanted)
        except KubectlError as exc:
            self.out(f"⚠️  --net-watch: could not look up the node ({exc}), continuing without it.")
            return
        if target is None or not target.internal_ip:
            self.out("⚠️  --net-watch: no other node with an address was found, continuing without it.")
            return
        self._watch_ip, self._watch_name = target.internal_ip, target.name
        self.out(f"📡 Network watch: pinging {target.name} ({target.internal_ip}) during the test.")

    def _probe_script(self) -> str:
        return (f'PING_TARGET="{self._watch_ip}"\n' if self._watch_ip else "") + PROBE_SCRIPT

    def _check_sensor(self) -> ProbeData:
        """Without a CPU sensor the overheating protection does not work -> warn."""
        data = parse_probe_output(self.kube.exec(self.names.probe, self._probe_script()))
        if data.cpu_temp is not None:
            if data.cpu_temp >= self.cfg.max_temp:
                raise RunnerError(
                    f"Before the test the node is already at {data.cpu_temp}°C (limit "
                    f"{self.cfg.max_temp}°C). Let it cool down or check the cooling.")
            self.out(f"🌡️  CPU sensor found ({data.cpu_temp}°C). "
                     f"Automatic stop at {self.cfg.max_temp}°C is active.")
            return data
        self.out("⚠️  No CPU temperature sensor was found on the node!")
        self.out("    Automatic stop on overheating will NOT work.")
        if self.allow_no_sensor:
            return data
        if self.confirm_no_sensor:
            self._close_screen()                       # the question needs the plain terminal
            confirmed = self.confirm_no_sensor()
            self._open_screen()
            if confirmed:
                return data
        raise RunnerError("The test without temperature protection was not confirmed "
                          "(use --allow-no-sensor if you want it).")

    def _resolve_ram_target(self, probe: ProbeData) -> Optional[int]:
        """RAM test target from the node's FREE memory (MemAvailable), with a 512 MiB reserve."""
        pct = self.cfg.ram_pct
        if not pct:
            return None
        if probe.mem_available_mib:
            avail, source = probe.mem_available_mib, "MemAvailable"
            target = calc_ram_target_mib(avail, 0, pct, reserve_mib=512)
        else:                                            # fallback: kubectl top
            top = parse_top(self.kube.top_node(self.node.name))
            if top is None:
                self.out("⚠️  Cannot determine free RAM, turning the RAM test off.")
                return None
            avail, source = self.node.allocatable_mem_mib - top.mem_mib, "kubectl top"
            target = calc_ram_target_mib(self.node.allocatable_mem_mib, top.mem_mib, pct)
        if target <= 0:
            self.out(f"⚠️  Little free memory ({avail} MiB), turning the RAM test off.")
            return None
        self.out(f"💡 RAM test: {target} MiB ({pct} % of {avail} MiB free, source: {source}).")
        log.info("RAM target %s MiB (free %s MiB, source %s)", target, avail, source)
        self._log(f"RAM target: {target} MiB ({pct} % of {avail} MiB free, source: {source})")
        return target

    def _resolve_net_peer(self) -> NodeInfo:
        """The other end of the network test: --net-peer, or a Ready worker (a master only when nothing else exists)."""
        wanted = self.cfg.net_peer
        if wanted:
            if wanted == self.node.name:
                raise RunnerError("The network peer must be a different node than the tested one.")
            return self.kube.get_node(wanted)
        others = [self.kube.get_node(n) for n in self.kube.list_node_names() if n != self.node.name]
        ready = sorted((n for n in others if n.ready), key=lambda n: (n.is_control_plane, n.name))
        if not ready:
            raise RunnerError("No other Ready node to test the network against (use --net-peer).")
        return ready[0]

    def _start_net_server(self) -> None:
        """Network test: iperf3 server pod on the peer node; sets net_target/net_port."""
        peer = self._resolve_net_peer()
        host = self.cfg.net_mode == "host"
        if host and not peer.internal_ip:
            raise RunnerError(f"Node {peer.name} has no InternalIP, use --net-mode pod.")
        self.net_peer = peer
        self.net_port = netmod.net_port(self.names.run_id, self.kube.used_node_ports())
        if (peer.is_control_plane or self.node.is_control_plane) and not self.cfg.net_rate:
            self.cfg.net_rate = netmod.NET_RATE_CAP_MASTER
            self.out(f"ℹ️  The master takes part: TCP tests capped at {netmod.NET_RATE_CAP_MASTER} Mbit/s.")
        self.out(f"⏳ Starting the iperf3 server on {peer.name} ({self.cfg.net_mode} network)...")
        self.kube.apply(net_server_pod(peer.name, self.names, self.deadline, self.net_port, host))
        if not self.kube.wait_ready(self.names.hw, 120):
            raise RunnerError("The iperf3 server pod timed out while starting.")
        for _ in range(90):
            text = self.kube.logs(self.names.hw)
            if "Server listening" in text:
                break
            if "NET-SERVER-FAILED" in text:
                raise RunnerError("iperf3 could not be installed on the peer node (no internet / apt?).")
            time.sleep(2)
        else:
            raise RunnerError("The iperf3 server did not start listening.")
        if "service" in self.cfg.net_extra:
            if host:
                self.out("ℹ️  The extra 'service' needs --net-mode pod, skipping it.")
            else:
                self._service_name = f"net-svc-{self.names.run_id}"
                self.kube.apply(net_service(self._service_name, self.names.run_id, self.net_port))
                self._service_ip = self.kube.service_ip(self._service_name)
                if not self._service_ip:
                    self.out("⚠️  The Service got no address, skipping the Service test.")
        self.net_target = peer.internal_ip if host else self.kube.pod_ip(self.names.hw)
        if not self.net_target:
            raise RunnerError("The address of the iperf3 server could not be determined.")
        self.out(f"🌐 Network peer: {peer.name} ({self.net_target}:{self.net_port})")
        self._log(f"Network peer: {peer.name} ({self.net_target}), mode {self.cfg.net_mode}"
                  + (f", TCP capped at {self.cfg.net_rate} Mbit/s" if self.cfg.net_rate else ""))

    def _start_stress(self) -> None:
        if self.cfg.net:
            self._start_net_server()
        cmd = build_stress_command(self.cfg, self.ram_target_mib, self.net_target, self.net_port,
                                   self._service_ip)
        limit = gpumod.GPU_LIMIT_MIB if self.cfg.gpu else stress_memory_limit_mib(self.ram_target_mib)
        log.info("stress-ng command: %s | memory limit %s MiB", cmd, limit)
        disk = self.cfg.disk
        gpu = self.cfg.gpu
        self.kube.apply(stress_pod(self.node.name, self.names, self.deadline, cmd, limit,
                                   package="git make g++" if gpu else "fio" if disk else netmod.packages(self.cfg.net_extra) if self.cfg.net else "stress-ng",
                                   image=(self.cfg.gpu_image or gpumod.GPU_IMAGE_DEFAULT) if gpu else "",
                                   gpu=gpu, setup=gpumod.build_gpu_setup(self.cfg.gpu_mem_pct) if gpu else "",
                                   scratch_mib=self.cfg.disk_size + 256 if disk else 0,
                                   host_network=self.cfg.net and self.cfg.net_mode == "host",
                                   gate_seconds=(self.gate_timeout + POD_DEADLINE_MARGIN + (GPU_DEADLINE_EXTRA if self.cfg.gpu else 0))
                                   if self.gate_wait else 0))
        self.out("⏳ Waiting for the pod to start...")
        if self.cfg.gpu:
            self.out("⏳ Pulling the CUDA image can take several minutes the first time (about 3 GB)...")
        if not self.kube.wait_ready(self.names.stress, 900 if self.cfg.gpu else 120):
            raise RunnerError("The stress-test pod timed out while starting." + (
                " Is the GPU free (nvidia.com/gpu), is the image pullable, does runtime 'nvidia' exist? "
                f"Check: kubectl describe pod {self.names.stress}" if self.cfg.gpu else ""))
        self.out(f"⏳ Waiting for {'gpu-burn to be built' if self.cfg.gpu else 'fio to be installed' if self.cfg.disk else 'iperf3 to be installed' if self.cfg.net else 'stress-ng to be installed'} and started...")
        self._step("install")
        tool = 'gpu-burn' if self.cfg.gpu else 'fio' if self.cfg.disk else 'iperf3' if self.cfg.net else 'stress-ng'
        ready_marker = READY_MARKER if self.gate_wait else STARTED_MARKER
        for _ in range(420 if self.cfg.gpu else 90):
            logs = self.kube.logs(self.names.stress)
            if ready_marker in logs or STARTED_MARKER in logs:
                break
            if FAILED_MARKER in logs:
                raise RunnerError(f"{tool} could not be started.")
            time.sleep(2)
        else:
            raise RunnerError(f"{tool} could not be started.")
        if not self.gate_wait:
            return
        self._wait_for_gate()
        for _ in range(60):                                   # the load starts right after /tmp/go appears
            logs = self.kube.logs(self.names.stress)
            if STARTED_MARKER in logs:
                return
            if FAILED_MARKER in logs:
                break
            time.sleep(0.5)
        raise RunnerError(f"{tool} could not be started.")

    def _wait_for_gate(self) -> None:
        """Synchronised start: says READY to the parent and waits for its GO (all nodes start together)."""
        self._step("ready")
        ready, go = self.start_gate + ".ready", self.start_gate + ".go"
        with open_private(ready, "w") as fh:
            fh.write(time.strftime("%H:%M:%S") + "\n")
        self.out("🚦 READY - waiting for the other nodes, the load starts together.")
        self._log(f"[{time.strftime('%H:%M:%S')}] READY, waiting for the start of all nodes")
        end = time.monotonic() + self.gate_timeout
        while not os.path.exists(go):
            if time.monotonic() >= end:
                raise RunnerError("The other nodes did not become ready in time, the test was not started.")
            time.sleep(0.1)
        self.kube.exec(self.names.stress, f"touch {GATE_FILE}", timeout=30)
        self.out("🟢 GO - the load is starting on all nodes.")
        self._log(f"[{time.strftime('%H:%M:%S')}] GO")

    @staticmethod
    def _elapsed(started_at: Optional[float]) -> str:
        if started_at is None:
            return "0 s"
        return format_duration(round(time.monotonic() - started_at))

    def _outcome(self, lines: list[str]) -> str:
        """'ok' / 'failed' / 'unknown'; a multi-stage test is ok only when ALL stages have finished."""
        if self.cfg.net:
            if any(line.startswith("NET-FAILED") for line in lines):
                return "failed"
            return "ok" if "NET-DONE" in lines else "unknown"
        if self.cfg.gpu:
            if any(line.startswith("GPU-FAILED") for line in lines):
                return "failed"
            return "ok" if "GPU-DONE" in lines else "unknown"
        if self.cfg.disk:
            if any(line.startswith("DISK-FAILED") for line in lines):
                return "failed"
            return "ok" if "DISK-DONE" in lines else "unknown"
        outcome = parse_stressng_outcome(lines)
        expected_stages = (len(self.cfg.steps) if self.cfg.stepped
                          else self.cfg.spike_cycles * 2 if self.cfg.spike else None)
        if expected_stages and outcome == "ok" \
                and count_completed_runs(lines) < expected_stages:
            return "unknown"
        return outcome

    def _on_net_line(self, monitor: Monitor, line: str) -> None:
        """Markers of the network script: a new job, a job result, the end or a failure."""
        kind, _, rest = line.partition(" ")
        stamp = time.strftime("%H:%M:%S")
        if kind == "NET-JOB":
            index = rest.split()[0].split("/")[0]
            monitor.stage = int(index) if index.isdigit() else 0
            msg = f"[{stamp}] ▶ Network job {rest}"
        elif kind == "NET-RESULT":
            name, _, payload = rest.partition(" ")
            result = netmod.parse_payload(name, payload)
            if result is None:
                msg = f"[{stamp}] ⚠️  {name}: the output could not be read"
            else:
                self.net_results.append(result)
                self._log(result.log_line())
                msg = f"[{stamp}] 🌐 {result.line()}"
                if name == "link-end":
                    start = next((r for r in self.net_results if r.name == "link"), None)
                    if start:
                        errors = netmod.link_errors(start, result)
                        self.net_results.append(errors)
                        self._log(errors.log_line())
                        msg += f"\n[{stamp}] 🌐 {errors.line()}"
        elif kind == "NET-SKIPPED":
            name, _, reason = rest.partition(" ")
            why = "no answer" if not reason.strip() else reason.strip()[:90]
            msg = (f"[{stamp}] ⚠️  {name} skipped: {why} - UDP is probably blocked by a firewall "
                   f"(allow UDP 30000-32767 or use --net-extra no-udp), the test continues")
        elif kind == "NET-FAILED":
            msg = f"[{stamp}] ❌ network job {rest} failed (is the peer reachable? firewall?)"
        else:
            return
        self.out(msg)
        if kind != "NET-RESULT":
            self._log(msg)

    def _on_gpu_line(self, monitor: Monitor, line: str) -> None:
        """Markers of the gpu-burn script: its final lines, the end or a failure."""
        kind, _, rest = line.partition(" ")
        stamp = time.strftime("%H:%M:%S")
        if kind == "GPU-PERF":
            found = re.search(r"\((\d+) Gflop/s\).*?errors:\s*(\d+)", rest)
            if not found:
                return
            text = f"{found.group(1)} Gflop/s · errors {found.group(2)}"
            if isinstance(self._screen, ui.GpuScreen):
                self._screen.set_perf(text)
            msg = f"[{stamp}] 🚀 gpu-burn performance: {text}"
        elif kind == "GPU-INFO":
            msg = f"[{stamp}] 🎮 {clean_text(rest, 200)}"
        elif kind == "GPU-FAILED":
            msg = f"[{stamp}] ❌ gpu-burn: {clean_text(rest, 200) or 'failed'}"
        else:
            return
        self.out(msg)
        self._log(msg)

    def _on_disk_line(self, monitor: Monitor, line: str) -> None:
        """Markers of the fio loop: a new job, a job result (JSON), the end or a failure."""
        kind, _, rest = line.partition(" ")
        if kind == "DISK-JOB":
            index = rest.split()[0].split("/")[0]
            monitor.stage = int(index) if index.isdigit() else 0
            msg = f"[{time.strftime('%H:%M:%S')}] ▶ Disk job {rest}"
        elif kind == "DISK-RESULT":
            name, _, payload = rest.partition(" ")
            result = parse_fio_json(name, payload)
            if result is None:
                msg = f"[{time.strftime('%H:%M:%S')}] ⚠️  {name}: fio output could not be read"
            else:
                self.disk_results.append(result)
                msg = f"[{time.strftime('%H:%M:%S')}] 💽 {result.line()}"
                self._log(result.log_line())
        elif kind == "DISK-FAILED":
            msg = f"[{time.strftime('%H:%M:%S')}] ❌ fio job {rest} failed"
        else:
            return
        self.out(msg)
        if kind != "DISK-RESULT":
            self._log(msg)

    def _on_stage(self, monitor: Monitor, index: int, count: int, percent: int) -> None:
        """Marker of a new stage from the pod output: switches the measurement and announces the stage."""
        monitor.stage = index
        if self.cfg.spike:
            low = index % 2 == 1     # odd stages are the low phase, even ones the high (spike) phase
            stage_time = self.cfg.spike_low_time if low else self.cfg.spike_high_time
            label = "▶ Low" if low else "▶ Spike!"
        else:
            stage_time = self.cfg.step_time
            label = "▶ Stage"
        msg = (f"[{time.strftime('%H:%M:%S')}] {label} {index}/{count}: {percent} % "
               f"({describe_duration(stage_time)})")
        self.out(msg)
        self._log(msg)
        log.info("Stage %s/%s: %s %%", index, count, percent)

    def _cooldown(self, monitor: Optional[Monitor]) -> None:
        """After the load ends it keeps measuring temperature and clock for a while (cooldown)."""
        seconds = self.cfg.cooldown
        if seconds <= 0 or monitor is None:
            return
        monitor.phase = "cooldown"
        self._phase = "cooldown"
        if self._screen:
            self._screen.begin_cooldown(seconds)
        self.out(f"❄️  The load has ended. For another {seconds} s I keep measuring the CPU clock and temperature "
                 f"(cooldown, Ctrl+C = skip).")
        self._log(f"[{time.strftime('%H:%M:%S')}] Cooldown: measuring for another {seconds} s")
        end = time.monotonic() + seconds
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.5, left))
        except KeyboardInterrupt:
            log.info("Cooldown skipped by the user/signal")
            self.out("↪️  Cooldown skipped.")

    def _emit_results(self, monitor: Optional[Monitor], stress_lines: list[str]) -> None:
        """Test summary to the terminal and to the log (from the measured data)."""
        if monitor is None:
            return
        monitor.stop()
        monitor.join(timeout=10)                   # let the measurement in progress finish
        self._close_screen()
        if monitor.phase == "cooldown":
            monitor.final_sample()                 # the last value exactly at the end of the cooldown
        metrics = parse_stressng_metrics(stress_lines)
        if self.cfg.stepped:
            stage_targets = tuple(self.cfg.steps)
        elif self.cfg.spike:
            stage_targets = tuple(
                SPIKE_LOW_PCT if i % 2 == 0 else self.cfg.spike_target
                for i in range(self.cfg.spike_cycles * 2))
        else:
            stage_targets = None
        stage_metrics = ({i: parse_stressng_metrics(part)
                          for i, part in split_stage_lines(stress_lines).items()}
                         if stage_targets else None)
        if self.cfg.net or self.cfg.disk or self.cfg.gpu:          # no CPU load here: only the node's state as information
            lines = self._light_summary(monitor.samples)
        else:
            lines = build_summary(
                monitor.samples, self._baseline, metrics, warn_temp=WARN_TEMP,
                cooldown_requested=self.cfg.cooldown, stage_targets=stage_targets,
                stage_metrics=stage_metrics)
        shown = list(lines)                        # what the terminal gets: the same, laid out for its width
        if self.net_results:
            notes = netmod.findings(self.net_results)
            tail = [f"  ⚠️  {n}" for n in notes] or ["  ✅ Nothing suspicious in the network results."]
            lines += ["NETWORK TEST (iperf3 + ping)", *[f"  {r.line()}" for r in self.net_results], *tail]
            shown += ["NETWORK TEST (iperf3 + ping)", *ui.label_rows([f"  {r.line()}" for r in self.net_results], label_w=13), *tail]
        if self.cfg.gpu:
            self.gpu_summary = gpumod.summarize(monitor.samples)
            gpu_lines = gpumod.summary_lines(monitor.samples)
            lines += ["GPU TEST (gpu-burn + nvidia-smi)", *gpu_lines]
            shown += ["GPU TEST (gpu-burn + nvidia-smi)", *ui.label_rows(gpu_lines, label_w=26)]
        if self.disk_results:
            lines += ["DISK BENCHMARK (fio, direct I/O)", *[f"  {r.line()}" for r in self.disk_results]]
            shown += ["DISK BENCHMARK (fio, direct I/O)", *ui.label_rows([f"  {r.line()}" for r in self.disk_results], label_w=11)]
        self.metrics = metrics
        self.stats = run_stats(monitor.samples,
                               self._baseline.cpu_temp if self._baseline else None,
                               WARN_TEMP, stage_targets, stage_metrics)
        if ui.adaptive():
            self.out("\n".join(shown))                # one block: a wide terminal puts the short rows in two columns
        else:
            for line in shown:
                self.out(line)
        for line in lines:
            self._log(line)
        log.info("Test summary (%d samples):\n%s", len(monitor.samples), "\n".join(lines))

    def _light_summary(self, samples: list) -> list[str]:
        """Header of the network / disk test summary: what the node did meanwhile (information only)."""
        test = [s for s in samples if s.phase == "test"]
        title = ("NETWORK TEST SUMMARY" if self.cfg.net else "GPU TEST SUMMARY" if self.cfg.gpu
                 else "DISK BENCHMARK SUMMARY")
        lines = ["=" * 52, title, "=" * 52]
        if not test:
            return lines + ["No measurement could be obtained during the test."]
        cpu = [s.cpu_pct for s in test if s.cpu_pct is not None]
        ram = [s for s in test if s.mem_used_mib is not None]
        temps = [s.cpu_temp for s in test if s.cpu_temp is not None]
        if cpu:
            lines.append(f"Node CPU load (info):     avg {sum(cpu) / len(cpu):.0f} % | max {max(cpu):.0f} %")
        if ram:
            top = max(ram, key=lambda s: s.mem_used_mib)
            lines.append(f"Node RAM (info):          max {top.mem_used_mib} MiB ({top.mem_used_pct:.0f} %)")
        if temps:
            base = f"   (idle before test {self._baseline.cpu_temp} °C)" if self._baseline and self._baseline.cpu_temp is not None else ""
            lines.append(f"CPU temperature (info):   max {max(temps)} °C{base}")
        return lines

    def preflight(self) -> None:
        """Checks whose failure you want to see right away in the terminal (before detaching)."""
        self._check_other_tests()
        self._check_capacity()

    # --- signals and cleanup -------------------------------------------------------
    @staticmethod
    def _install_signal_handlers() -> None:
        def handler(signum, frame):   # noqa: ARG001
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, handler)

    def _cleanup(self) -> None:
        """Deletes only the pods of THIS run (other concurrent tests are not touched)."""
        self.kube.delete_pods(*self.names.all)
        if self._service_name:
            self.kube.delete_service(self._service_name)

    def _check_other_tests(self) -> None:
        """Concurrent tests are fine on different nodes, not on the same one."""
        others = [p for p in self.kube.list_tool_pods()
                  if p.run_id != self.names.run_id and p.phase in ("Running", "Pending")]
        log.info("Other running pods of the tool: %s", others)
        same = [p for p in others if p.node == self.node.name]
        if same:
            raise RunnerError(
                f"On node {self.node.name} another test is running (pod {same[0].name}). "
                f"Two tests on one node would distort each other's results. "
                f"Wait for it to finish, or stop it.")
        for pod in others:
            self.out(f"ℹ️  Another test is running on node {pod.node} (pod {pod.name}), "
                     f"this one is not affected by it.")

    def _check_capacity(self) -> None:
        """The node should not already be busy (other pods) - it would skew the results."""
        if self.skip_capacity_check:
            return
        try:
            top = parse_top(self.kube.top_node(self.node.name))
        except KubectlError as exc:
            self.out(f"⚠️  Could not determine node usage ({exc}), continuing.")
            return
        if top is None:
            self.out("⚠️  Could not determine node usage (kubectl top), continuing.")
            return
        cpu_pct = parse_pct(top.cpu_pct)
        mem_pct = parse_pct(top.mem_pct)
        values = [v for v in (cpu_pct, mem_pct) if v is not None]
        if not values:
            return
        busy = max(values)
        note = f"CPU {top.cpu_pct}, RAM {top.mem_pct}"
        if busy >= self.max_busy_pct:
            msg = (f"Node {self.node.name} is already busy ({note}), "
                   f"the limit for starting a test is {self.max_busy_pct} %.")
            if self.allow_busy_node:
                self.out(f"⚠️  {msg} Continuing (--allow-busy-node).")
                return
            raise RunnerError(f"{msg} Wait until it frees up, or use --allow-busy-node.")
        self.out(f"✅ Node usage OK before the test ({note}).")

    def _export_results(self) -> None:
        """JSON/CSV export next to the log and the check against the node's baseline (never breaks the run)."""
        if not self.log_path or not os.path.exists(self.log_path):
            return
        try:
            run = read_log(self.log_path)
            if not any(s.phase == "test" for s in run.samples):
                return
            check = None
            if self.cfg.baseline_check:
                check = baseline.check_run(run, log_root_of(os.path.abspath(self.log_path)))
            if check is not None:
                for line in baseline.baseline_report(check):
                    self.out(line)
            for path in write_exports(run, self.log_path, self.cfg.export,
                                      extra={"baseline_check": check} if check else None):
                self.out(f"📄 Export saved to: {os.path.abspath(path)}")
        except Exception:                                  # noqa: BLE001 - the export is a bonus, not part of the test
            log.exception("Export / baseline check failed")

    # --- main run ----------------------------------------------------------------
    def run(self) -> int:
        self._install_signal_handlers()
        monitor: Optional[Monitor] = None
        logs_proc = None
        code = EXIT_OK
        started_at: Optional[float] = None
        stress_lines: list[str] = []
        log.info("Test start: node=%s, %s", self.node.name, self.cfg)
        log.info("RAM test=%s %%, interval=%ss, remaining_every=%s, log=%s",
                 self.cfg.ram_pct, self.interval, self.remaining_every, self.log_path)
        self._print_summary()
        log.info("Pod names of this run: %s", self.names)
        try:
            self._open_screen()
            self._step("check")
            self._check_other_tests()
            self._check_capacity()
            self.kube.delete_finished_tool_pods()   # leftovers (only finished ones, not running)
            # temperature is checked before detecting the hardware, so that an overheated
            # node does not needlessly wait on apt/dmidecode (fail fast)
            self._step("probe")
            self._start_probe()
            probe_data = self._check_sensor()
            self._baseline = probe_data
            if self.cfg.smart:
                self._step("smart")
            self._check_smart()
            self._step("hw")
            hardware = self._collect_hardware()
            log.info("Node hardware detected:\n%s", hardware)
            self.out(f"🖥️  NODE HARDWARE {self.node.name}:\n{hardware}")
            cpu_line = cpu_summary(hardware)
            if cpu_line and self._screen and not self.cfg.gpu:
                self._screen.set_subtitle(f"{ui.ICON_CPU} {cpu_line}")

            if self.cfg.gpu:
                self._check_gpu(hardware)
            if self.log_path:
                self._write_log_header(hardware)
                self.out(f"📊 Metrics are being saved to: {os.path.abspath(self.log_path)}")
            self.ram_target_mib = self._resolve_ram_target(probe_data)
            self._step("pod")
            self._start_stress()
            if self.cfg.gpu:
                self._gpu_info()
            log.info("stress-ng is running, the monitor is starting")
            started_at = time.monotonic()
            self._mark_started()
            ends = time.strftime("%H:%M:%S",
                                 time.localtime(time.time() + self.cfg.duration))
            self.out(f"🔥 Test running {describe_duration(self.cfg.duration)}, "
                     f"ends around {ends} "
                     f"(automatic stop at {self.cfg.max_temp}°C"
                     f"{f', GPU {self.cfg.gpu_max_temp}°C' if self.cfg.gpu else ''})")
            self.out("-" * 52)
            if self._screen:
                self._screen.begin_test()
            if self.cfg.gpu:
                self.out(f"[{time.strftime('%H:%M:%S')}] ▶ gpu-burn started ({self.cfg.gpu_mem_pct} % of the GPU memory, "
                         f"{'double' if self.cfg.gpu_double else 'single'} precision)")

            monitor = Monitor(
                self.kube, self.node.name, OverheatGuard(self.cfg.max_temp),
                emit=self._emit_measurement,
                on_abort=lambda: self.kube.delete_pods(self.names.stress),
                probe_pod=self.names.probe,
                initial=probe_data,
                interval=self.interval,
                total_seconds=self.cfg.total_duration,
                remaining_every=self.remaining_every,
                emit_info=self._emit_info,
                ping_target=self._watch_ip,
                gpu_pod=self.names.stress if self.cfg.gpu else "",
                gpu_guard=gpumod.GpuGuard(self.cfg.gpu_max_temp) if self.cfg.gpu else None)
            monitor.start()

            logs_proc = self.kube.stream_logs(self.names.stress)
            for line in logs_proc.stdout:
                line = line.rstrip("\n")
                stress_lines.append(line)              # kept in full for outcome/metrics parsing
                if self.cfg.net and line.startswith("NET-"):
                    self._on_net_line(monitor, line)
                    continue
                if self.cfg.gpu and line.startswith("GPU-"):
                    self._on_gpu_line(monitor, line)
                    if line.startswith(("GPU-DONE", "GPU-FAILED")):
                        break                        # the pod idles on (the cooldown reads the GPU through it)
                    continue
                if self.cfg.disk and line.startswith("DISK-"):
                    self._on_disk_line(monitor, line)
                    continue
                stage = STAGE_RE.match(line) if self.cfg.multi_stage else None
                if stage:                            # the stage marker is not printed raw
                    self._on_stage(monitor, int(stage.group(1)), int(stage.group(2)),
                                   int(stage.group(3)))
                    continue
                # stress-ng's own chatter (dispatching hogs, per-run start/end, metrics table, ...)
                # is noisy, especially with many short spike cycles - keep the terminal to our own
                # output (headers, stage/measurement lines, the final summary).
                if line.startswith("stress-ng:") or line in (STARTED_MARKER, FAILED_MARKER):
                    continue
                self.out(line)
            if self.cfg.gpu and logs_proc.poll() is None:
                logs_proc.terminate()
            logs_proc.wait()
            elapsed = time.monotonic() - started_at
            outcome = self._outcome(stress_lines)
            log.info("Log stream ended after %.1f s, stress-ng result: %s", elapsed, outcome)

            if monitor.aborted:
                limit = self.cfg.gpu_max_temp if monitor.abort_kind == "GPU" else self.cfg.max_temp
                what = ("a GPU failure (nvidia-smi did not respond repeatedly, the GPU cannot be watched)"
                        if monitor.abort_temp is None else
                        f"overheating ({monitor.abort_kind} {monitor.abort_temp}°C ≥ {limit}°C)")
                msg = (f"Test stopped because of {what} "
                       f"after {self._elapsed(started_at)} of {format_duration(self.cfg.duration)}.")
                self.out(f"🛑 {msg}")
                self._log(f"[{time.strftime('%H:%M:%S')}] 🛑 {msg}")
                code = EXIT_OVERHEAT
                log.error("Test terminated by overheating: %s", msg)
            elif outcome == "failed":
                msg = (f"{'The network test' if self.cfg.net else 'The disk benchmark' if self.cfg.disk else 'The GPU test' if self.cfg.gpu else 'stress-ng'} "
                       f"ended with an error (see the output above).")
                self.out(f"❌ {msg}")
                self._log(f"[{time.strftime('%H:%M:%S')}] ❌ {msg}")
                code = EXIT_ERROR
                log.error(msg)
            elif outcome == "ok" or elapsed >= self.cfg.duration * 0.98:
                msg = f"Test completed. Actual run time: {self._elapsed(started_at)}."
                self.out(f"✅ {msg}")
                self._log(f"[{time.strftime('%H:%M:%S')}] ✅ {msg}")
            else:
                # stress-ng did not print the final message and the time has not run out yet:
                # someone deleted / terminated the pod from outside, or the log stream failed
                phase = self.kube.pod_phase(self.names.stress)
                state = (f"pod phase: {phase}" if phase
                         else "the pod no longer exists (deleted from outside)")
                msg = (f"Test ended PREMATURELY after {self._elapsed(started_at)} "
                       f"of {format_duration(self.cfg.duration)} ({state}). "
                       f"The load pod was terminated in a way other than by stress-ng itself. "
                       f"Another run of the tool or `kubectl delete` most likely deleted it.")
                self.out(f"⚠️  {msg}")
                self._log(f"[{time.strftime('%H:%M:%S')}] ⚠️ {msg}")
                code = EXIT_PREMATURE
                log.error("Premature end: %s", msg)

            self._cooldown(monitor)                 # a minute of measuring after the load ends
            self._emit_results(monitor, stress_lines)
        except KeyboardInterrupt:
            log.warning("Test interrupted by the user/signal")
            msg = (f"Test interrupted after {self._elapsed(started_at)} "
                   f"of {format_duration(self.cfg.duration)}.")
            self.out(f"\n🛑 {msg} Deleting the pods...")
            self._log(f"[{time.strftime('%H:%M:%S')}] 🛑 {msg}")
            code = EXIT_INTERRUPTED
            try:                                    # show what was measured up to the interruption
                self._emit_results(monitor, stress_lines)
            except Exception:                       # noqa: BLE001 - the summary must not break the cleanup
                log.exception("The summary after the interruption could not be built")
        except (RunnerError, KubectlError) as exc:
            log.error("Test failed: %s", exc)
            self.out(f"❌ {exc}")
            self._log(f"[{time.strftime('%H:%M:%S')}] ❌ {exc}")
            code = EXIT_ERROR
        finally:
            log.info("Pod cleanup and shutdown, exit code %s", code)
            if monitor is not None:
                monitor.stop()
            if logs_proc is not None and logs_proc.poll() is None:
                logs_proc.terminate()
            self._close_screen()
            self._cleanup()
            self._export_results()
            if self.log_path:
                self.out(f"📁 Log saved to: {os.path.abspath(self.log_path)}")
        return code
