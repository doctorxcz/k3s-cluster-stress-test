"""Course of one test: hardware -> probe -> stress-ng -> monitor -> cleanup."""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Callable, Optional

from .kube import Kubectl, KubectlError
from .manifests import (FAILED_MARKER, PROBE_SCRIPT, STARTED_MARKER,
                        build_stress_command, hw_pod, probe_pod, stress_memory_limit_mib,
                        stress_pod)
from .models import MAX_NODE_BUSY_PCT, WARN_TEMP, NodeInfo, PodNames, ProbeData, StressConfig
from .monitor import Monitor, OverheatGuard
from .parsing import (STAGE_RE, calc_ram_target_mib, count_completed_runs,
                      describe_duration, describe_steps, format_duration,
                      parse_pct, parse_probe_output, parse_stressng_outcome, parse_top,
                      split_stage_lines)
from .paths import open_private
from .summary import (RunStats, StressMetric, build_summary, parse_stressng_metrics,
                      run_stats)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_OVERHEAT = 3
EXIT_PREMATURE = 4      # the test ended earlier than it should (the pod disappeared/was terminated)
EXIT_INTERRUPTED = 130

POD_DEADLINE_MARGIN = 600   # s extra for pulling the image and apt install


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
                 skip_capacity_check: bool = False) -> None:
        self.kube = kube
        self.node = node
        self.cfg = cfg
        self.ram_target_mib = ram_target_mib
        self.log_path = log_path
        self.interval = interval
        self.remaining_every = remaining_every
        self.allow_no_sensor = allow_no_sensor
        self.confirm_no_sensor = confirm_no_sensor
        self.out = out
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
        self.deadline = cfg.duration + POD_DEADLINE_MARGIN

    # --- output and log -------------------------------------------------------
    def _log(self, text: str) -> None:
        if self.log_path:
            with open_private(self.log_path, "a") as fh:      # permissions 0600
                fh.write(text + "\n")

    def _emit_measurement(self, line: str) -> None:
        prefix = "❄️  " if self._phase == "cooldown" else "⏳ "
        self.out(prefix + line)
        self._log(("[cooldown] " if self._phase == "cooldown" else "") + line)

    def _emit_info(self, line: str) -> None:
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
        info = (self.kube.logs(self.names.hw).strip() if phase == "Succeeded"
                else f"Hardware detection failed (pod phase: {phase or 'unknown'}).")
        self.kube.delete_pods(self.names.hw)
        return info

    def _write_log_header(self, hardware: str) -> None:
        n, c = self.node, self.cfg
        self._log("=== KUBERNETES STRESS-NG LOG ===")
        self._log(f"Node: {n.name}")
        self._log(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"Test duration: {describe_duration(c.duration)}")
        if c.stepped:
            self._log(f"Profile: stepped {describe_steps(c.steps)} % for "
                      f"{describe_duration(c.step_time)}")
            self._log(f"CPU load: gradually {describe_steps(c.steps)} %")
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
        self._log("=== HARDWARE ===")
        self._log(hardware)
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
            ("Stop at", f"{c.max_temp} °C (2 readings in a row)"),
            ("Cooldown after test", f"{c.cooldown} s of measuring" if c.cooldown else "off"),
            ("Log", self.log_path or "no"),
        ]
        self.out("-" * 52)
        self.out("TEST SETTINGS")
        for key, value in rows:
            self.out(f"  {key + ':':<21}{value}")
        self.out("  (preparing the pods before the load starts takes about 1–3 min extra)")
        self.out("-" * 52)

    def _start_probe(self) -> None:
        self.kube.apply(probe_pod(self.node.name, self.names, self.deadline))
        if not self.kube.wait_ready(self.names.probe, 60):
            raise RunnerError("The temperature probe (temp-probe) did not come up.")

    def _check_sensor(self) -> ProbeData:
        """Without a CPU sensor the overheating protection does not work -> warn."""
        data = parse_probe_output(self.kube.exec(self.names.probe, PROBE_SCRIPT))
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
        if self.confirm_no_sensor and self.confirm_no_sensor():
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

    def _start_stress(self) -> None:
        cmd = build_stress_command(self.cfg, self.ram_target_mib)
        limit = stress_memory_limit_mib(self.ram_target_mib)
        log.info("stress-ng command: %s | memory limit %s MiB", cmd, limit)
        self.kube.apply(stress_pod(self.node.name, self.names, self.deadline, cmd, limit))
        self.out("⏳ Waiting for the pod to start...")
        if not self.kube.wait_ready(self.names.stress, 120):
            raise RunnerError("The stress-test pod timed out while starting.")
        self.out("⏳ Waiting for stress-ng to be installed and started...")
        for _ in range(90):
            logs = self.kube.logs(self.names.stress)
            if STARTED_MARKER in logs:
                return
            if FAILED_MARKER in logs:
                break
            time.sleep(2)
        raise RunnerError("stress-ng could not be started.")

    @staticmethod
    def _elapsed(started_at: Optional[float]) -> str:
        if started_at is None:
            return "0 s"
        return format_duration(round(time.monotonic() - started_at))

    def _outcome(self, lines: list[str]) -> str:
        """'ok' / 'failed' / 'unknown'; the stepped test is ok only when ALL stages have finished."""
        outcome = parse_stressng_outcome(lines)
        if self.cfg.stepped and outcome == "ok" \
                and count_completed_runs(lines) < len(self.cfg.steps):
            return "unknown"
        return outcome

    def _on_stage(self, monitor: Monitor, index: int, count: int, percent: int) -> None:
        """Marker of a new stage from the pod output: switches the measurement and announces the stage."""
        monitor.stage = index
        msg = (f"[{time.strftime('%H:%M:%S')}] ▶ Stage {index}/{count}: {percent} % "
               f"({describe_duration(self.cfg.step_time)})")
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
        if monitor.phase == "cooldown":
            monitor.final_sample()                 # the last value exactly at the end of the cooldown
        metrics = parse_stressng_metrics(stress_lines)
        stage_targets = tuple(self.cfg.steps) if self.cfg.stepped else None
        stage_metrics = ({i: parse_stressng_metrics(part)
                          for i, part in split_stage_lines(stress_lines).items()}
                         if stage_targets else None)
        lines = build_summary(
            monitor.samples, self._baseline, metrics, warn_temp=WARN_TEMP,
            cooldown_requested=self.cfg.cooldown, stage_targets=stage_targets,
            stage_metrics=stage_metrics)
        self.metrics = metrics
        self.stats = run_stats(monitor.samples,
                               self._baseline.cpu_temp if self._baseline else None,
                               WARN_TEMP, stage_targets, stage_metrics)
        for line in lines:
            self.out(line)
            self._log(line)
        log.info("Test summary (%d samples):\n%s", len(monitor.samples), "\n".join(lines))

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
            self._check_other_tests()
            self._check_capacity()
            self.kube.delete_finished_tool_pods()   # leftovers (only finished ones, not running)
            # temperature is checked before detecting the hardware, so that an overheated
            # node does not needlessly wait on apt/dmidecode (fail fast)
            self._start_probe()
            probe_data = self._check_sensor()
            self._baseline = probe_data
            hardware = self._collect_hardware()
            log.info("Node hardware detected:\n%s", hardware)
            self.out(f"🖥️  NODE HARDWARE {self.node.name}:\n{hardware}")
            if self.log_path:
                self._write_log_header(hardware)
                self.out(f"📊 Metrics are being saved to: {os.path.abspath(self.log_path)}")
            self.ram_target_mib = self._resolve_ram_target(probe_data)
            self._start_stress()
            log.info("stress-ng is running, the monitor is starting")
            started_at = time.monotonic()
            ends = time.strftime("%H:%M:%S",
                                 time.localtime(time.time() + self.cfg.duration))
            self.out(f"🔥 Test running {describe_duration(self.cfg.duration)}, "
                     f"ends around {ends} "
                     f"(automatic stop at {self.cfg.max_temp}°C)")
            self.out("-" * 52)

            monitor = Monitor(
                self.kube, self.node.name, OverheatGuard(self.cfg.max_temp),
                emit=self._emit_measurement,
                on_abort=lambda: self.kube.delete_pods(self.names.stress),
                probe_pod=self.names.probe,
                initial=probe_data,
                interval=self.interval,
                total_seconds=self.cfg.duration,
                remaining_every=self.remaining_every,
                emit_info=self._emit_info)
            monitor.start()

            logs_proc = self.kube.stream_logs(self.names.stress)
            for line in logs_proc.stdout:
                line = line.rstrip("\n")
                stress_lines.append(line)
                stage = STAGE_RE.match(line) if self.cfg.stepped else None
                if stage:                            # the stage marker is not printed raw
                    self._on_stage(monitor, int(stage.group(1)), int(stage.group(2)),
                                   int(stage.group(3)))
                    continue
                self.out(line)
            logs_proc.wait()
            elapsed = time.monotonic() - started_at
            outcome = self._outcome(stress_lines)
            log.info("Log stream ended after %.1f s, stress-ng result: %s", elapsed, outcome)

            if monitor.aborted:
                msg = (f"Test stopped because of overheating "
                       f"({monitor.abort_temp}°C ≥ {self.cfg.max_temp}°C) "
                       f"after {self._elapsed(started_at)} of {format_duration(self.cfg.duration)}.")
                self.out(f"🛑 {msg}")
                self._log(f"[{time.strftime('%H:%M:%S')}] 🛑 {msg}")
                code = EXIT_OVERHEAT
                log.error("Test terminated by overheating: %s", msg)
            elif outcome == "failed":
                msg = "stress-ng ended with an error (see the output above)."
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
            self._cleanup()
            if self.log_path:
                self.out(f"📁 Log saved to: {os.path.abspath(self.log_path)}")
        return code
