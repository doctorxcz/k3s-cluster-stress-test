"""Concurrent test of workers (--parallel): every node runs in its own subprocess.

The parent only starts the nodes and watches them: it reads the progress from their logs (live table), regularly
measures the API response time (`kubectl get --raw /readyz`) and on a persistently slow response or
outage stops all tests. The master is never tested at the same time as the workers, but after
them and alone (done by the shared SeriesRunner). The summary is assembled from the node logs.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .kube import Kubectl, KubectlError
from .logparse import _STAGE_RE, _parse_sample_line, read_log
from .models import WARN_TEMP, NodeInfo, StressConfig
from .parsing import format_duration
from .paths import open_private
from .runner import EXIT_ERROR, EXIT_INTERRUPTED
from .series import (_STATUS, NodeOutcome, SeriesOptions, SeriesRunner, estimate_seconds,
                     plan_config)
from .summary import StressMetric, run_stats

log = logging.getLogger(__name__)

API_INTERVAL = 5.0            # s between API response checks
API_CONSECUTIVE = 2           # how many times in a row the response must be bad before everything is stopped
TERMINATE_WAIT = 90           # s for a graceful shutdown of the subprocesses (they delete pods)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ApiGuard:
    """Watches the API response: slow or failed responses `consecutive`× in a row = stop."""

    def __init__(self, limit: float = 3.0, consecutive: int = API_CONSECUTIVE) -> None:
        self.limit = limit
        self.consecutive = consecutive
        self.bad = 0
        self.last_seconds = 0.0

    def record(self, ok: bool, seconds: float) -> bool:
        """Records one measurement; True = the response is persistently bad, stop."""
        self.last_seconds = seconds
        if ok and seconds <= self.limit:
            self.bad = 0
        else:
            self.bad += 1
        return self.bad >= self.consecutive

    def check(self, kube: Kubectl) -> bool:
        started = time.monotonic()
        try:
            kube.run("get", "--raw", "/readyz", timeout=max(self.limit * 3, 10.0))
            ok = True
        except KubectlError as exc:
            log.warning("API check failed: %s", exc)
            ok = False
        stop = self.record(ok, time.monotonic() - started)
        log.debug("API response %.2f s (ok=%s), bad in a row %d", self.last_seconds, ok, self.bad)
        return stop


# --- progress of subprocesses from their logs -------------------------------------------------------

@dataclass
class LiveInfo:
    """The last known state of a node (from the end of its log)."""

    cpu: Optional[float] = None
    temp: Optional[int] = None
    freq: Optional[int] = None
    stage: Optional[tuple[int, int, int]] = None      # (stage, number of stages, target in %)
    cooling: bool = False
    has_sample: bool = False


def read_tail(path: str, size: int = 16384) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - size, 0))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def tail_status(path: str) -> LiveInfo:
    """The last reading and stage from the end of a node log (the log is read backwards)."""
    info = LiveInfo()
    for raw in reversed(read_tail(path).splitlines()):
        line = raw.strip()
        if not info.has_sample:
            parsed = _parse_sample_line(line)
            if parsed:
                sample = parsed[1]
                info.cpu, info.temp, info.freq = sample.cpu_pct, sample.cpu_temp, sample.freq_mhz
                info.cooling, info.has_sample = sample.phase == "cooldown", True
                continue
        if info.stage is None:
            match = _STAGE_RE.search(line)
            if match:
                info.stage = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if info.has_sample and info.stage is not None:
            break
    return info


def describe_state(info: LiveInfo, code: Optional[int]) -> str:
    if code is not None:
        return "done: OK" if code == 0 else _STATUS.get(code, "ERROR")
    if not info.has_sample:
        return "preparing"
    if info.cooling:
        return "cooldown"
    if info.stage:
        return f"▶ Stage {info.stage[0]}/{info.stage[1]} ({info.stage[2]} %)"
    return "load"


def format_row(name: str, info: LiveInfo, code: Optional[int], remaining: Optional[float]) -> str:
    """One row of the live table."""
    cpu = f"{info.cpu:.0f} %" if info.cpu is not None else "-"
    temp = f"{info.temp} °C" if info.temp is not None else "-"
    freq = f"{info.freq} MHz" if info.freq is not None else "-"
    rest = "" if code is not None or remaining is None else f"left ~{format_duration(round(remaining))}"
    return (f"{name:<26}{describe_state(info, code):<24}CPU {cpu:>6}  {temp:>7}  {freq:>9}  {rest}"
            ).rstrip()


class LiveTable:
    """Node status table: redrawn in place in a terminal, otherwise printed once per `interval`."""

    def __init__(self, out: Callable[[str], None], tty: bool, interval: float = 30.0) -> None:
        self.out = out
        self.tty = tty
        self.interval = interval
        self._height = 0
        self._last: Optional[float] = None

    def update(self, lines: Sequence[str], now: Optional[float] = None, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        if self.tty:
            if self._last is not None and now - self._last < 2 and not force:
                return
            move = f"\x1b[{self._height}A" if self._height else ""
            sys.stdout.write(move + "".join(f"\x1b[2K{line}\n" for line in lines))
            sys.stdout.flush()
            self._height = len(lines)
        else:
            if self._last is not None and now - self._last < self.interval and not force:
                return
            self.out(f"[{time.strftime('%H:%M:%S')}] node status:")
            for line in lines:
                self.out(f"  {line}")
        self._last = now

    def close(self) -> None:
        self._height = 0                       # the table stays on the screen, writing continues below it


# --- subprocesses --------------------------------------------------------------------------------

def child_args(cfg: StressConfig, options: SeriesOptions, log_file: str, concurrent: int) -> list[str]:
    """Arguments of the subprocess that tests one worker (no questions, with a fixed path to the log)."""
    args = ["--node", cfg.node, "--yes", "--non-interactive", "--no-background",
            "--log-file", log_file, "--max-temp", str(cfg.max_temp),
            "--cooldown", str(cfg.cooldown), "--interval", str(options.interval),
            "--remaining-every", str(options.remaining_every),
            "--concurrent", str(concurrent), "--notes", cfg.notes, "--export", cfg.export]
    if not cfg.baseline_check:
        args.append("--no-baseline-check")
    if cfg.net_watch:
        args += ["--net-watch", cfg.net_watch]
    if cfg.smart:
        args.append("--smart")
    if cfg.allow_bad_disk:
        args.append("--allow-bad-disk")
    if cfg.stepped:
        args += ["--profile", "stepped", "--steps", ",".join(str(s) for s in cfg.steps),
                 "--step-time", str(cfg.step_time)]
    elif cfg.net:
        args += ["--profile", "net", "--net-time", str(cfg.net_time), "--net-mode", cfg.net_mode,
                 "--net-rate", str(cfg.net_rate)]
        if cfg.net_peer:
            args += ["--net-peer", cfg.net_peer]
        if cfg.net_extra:
            args += ["--net-extra", ",".join(cfg.net_extra)]
    elif cfg.disk:
        args += ["--profile", "disk", "--disk-size", str(cfg.disk_size),
                 "--disk-job-time", str(cfg.disk_job_time)]
    elif cfg.spike:
        args += ["--profile", "spike", "--spike-target", str(cfg.spike_target),
                 "--spike-low-time", str(cfg.spike_low_time),
                 "--spike-high-time", str(cfg.spike_high_time),
                 "--time", str(cfg.duration)]
    else:
        args += ["--profile", "classic", "--time", str(cfg.duration),
                 "--cpu-load", str(cfg.cpu_load)]
        if cfg.ram_pct:
            args += ["--ram-pct", str(cfg.ram_pct)]
        if cfg.hdd:
            args.append("--hdd")
    if options.allow_no_sensor:
        args.append("--allow-no-sensor")
    if options.hw_privileged:
        args.append("--hw-privileged")
    if options.skip_hw:
        args.append("--no-hw")
    args += ["--max-busy-pct", str(options.max_busy_pct)]
    if options.allow_busy_node:
        args.append("--allow-busy-node")
    if options.skip_capacity_check:
        args.append("--no-capacity-check")
    return args


def outcome_from_log(node: NodeInfo, cfg: StressConfig, log_path: str,
                     code: Optional[int]) -> NodeOutcome:
    """A node result assembled from its log (the subprocess passes data only through the log)."""
    outcome = NodeOutcome(node.name, node.is_control_plane, code, log_path=log_path)
    try:
        run = read_log(log_path)
    except (OSError, ValueError) as exc:
        outcome.reason = f"could not read the log ({exc})"
        return outcome
    targets = run.stage_targets if cfg.multi_stage and run.stage_targets else None
    outcome.stats = run_stats(run.samples, run.baseline_temp, WARN_TEMP, stage_targets=targets)
    if cfg.multi_stage and run.stage_ops:
        outcome.metrics = [StressMetric("cpu", 0, 0.0, run.stage_ops[max(run.stage_ops)])]
    else:
        outcome.metrics = list(run.metrics)
    return outcome


class ParallelSeriesRunner(SeriesRunner):
    """Workers at once (subprocesses), the master after them and alone; watches the API response."""

    parallel = True

    def __init__(self, *args, api_limit: float = 3.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.api_limit = api_limit

    def _execute(self) -> tuple[list[NodeOutcome], bool]:
        workers = [n for n in self.nodes if not n.is_control_plane]
        masters = [n for n in self.nodes if n.is_control_plane]
        outcomes, interrupted = self._run_workers(workers) if workers else ([], False)
        if interrupted or self.abort_reason:
            return outcomes, interrupted
        try:
            for index, node in enumerate(masters, len(workers) + 1):
                outcome = self._test_node(index, node)
                outcomes.append(outcome)
                if outcome.code == EXIT_INTERRUPTED:
                    return outcomes, True
        except KeyboardInterrupt:
            return outcomes, True
        return outcomes, False

    # --- workers at once -----------------------------------------------------------------------
    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def _rows(self, plans: dict, procs: dict, started: float, now: float) -> list[str]:
        rows = []
        for name, (_node, cfg, log_file, _console) in plans.items():
            code = procs[name].poll()
            remaining = max(estimate_seconds(cfg) - (now - started), 0.0)
            rows.append(format_row(name, tail_status(log_file), code, remaining))
        return rows

    def _terminate(self, procs: dict) -> None:
        """Shuts the subprocesses down gracefully (SIGTERM = pod cleanup), forcibly after a while."""
        def signal_all(sig: int) -> None:
            for proc in procs.values():
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
        signal_all(signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_WAIT
        while time.monotonic() < deadline and any(p.poll() is None for p in procs.values()):
            time.sleep(0.3)
        signal_all(signal.SIGKILL)

    def _run_workers(self, workers: Sequence[NodeInfo]) -> tuple[list[NodeOutcome], bool]:
        opts = self.options
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        plans: dict[str, tuple[NodeInfo, StressConfig, str, str]] = {}
        procs: dict[str, subprocess.Popen] = {}
        handles = []
        started = time.monotonic()
        try:
            for node in workers:
                cfg, _messages = plan_config(self.template, node)
                log_file = str(opts.log_dir / f"{node.name}-{cfg.duration}s-{stamp}.log")
                console = log_file[:-4] + ".console.txt"
                plans[node.name] = (node, cfg, log_file, console)
                handle = open_private(console, "a")
                handles.append(handle)
                proc = subprocess.Popen(
                    [sys.executable, "-m", "stress_test",
                     *child_args(cfg, opts, log_file, len(workers))],
                    stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True, env=self._child_env())
                procs[node.name] = proc
                self._log(f"Node {node.name}: subprocess PID {proc.pid}, log {log_file}, "
                          f"output {console}")
            self._say(f"▶ Started {len(workers)} workers at once: "
                      + ", ".join(n.name for n in workers))
            interval = float(os.environ.get("STRESS_TEST_TABLE_INTERVAL", "30"))
            table = LiveTable(opts.out, sys.stdout.isatty(), interval)
            guard = ApiGuard(self.api_limit)
            api_every = float(os.environ.get("STRESS_TEST_API_INTERVAL", API_INTERVAL))
            next_api = time.monotonic() + api_every
            interrupted = False
            try:
                while any(p.poll() is None for p in procs.values()):
                    time.sleep(0.5)
                    now = time.monotonic()
                    table.update(self._rows(plans, procs, started, now), now)
                    if now >= next_api:
                        next_api = now + api_every
                        if guard.check(self.kube):
                            self.abort_reason = (f"API response {guard.last_seconds:.1f} s "
                                                 f"(limit {guard.limit:g} s), stopping "
                                                 f"all tests")
                            break
            except KeyboardInterrupt:
                interrupted = True
            table.update(self._rows(plans, procs, started, time.monotonic()), force=True)
            table.close()
            if self.abort_reason or interrupted:
                self._terminate(procs)
            for proc in procs.values():
                proc.wait()
        finally:
            if any(p.poll() is None for p in procs.values()):
                self._terminate(procs)
            for handle in handles:
                handle.close()
        outcomes = []
        for name, (node, cfg, log_file, _console) in plans.items():
            code = procs[name].returncode
            if code is None:
                code = EXIT_ERROR
            elif code < 0:
                code = EXIT_INTERRUPTED
            outcome = outcome_from_log(node, cfg, log_file, code)
            outcomes.append(outcome)
            self._log(f"[{time.strftime('%H:%M:%S')}] Node {node.name}: {outcome.status} "
                      f"(code {code}), log {log_file}")
        return outcomes, interrupted
