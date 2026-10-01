"""Concurrent test of workers (--parallel): every node runs in its own subprocess.

The parent only starts the nodes and watches them: it reads the progress from their logs (live table), regularly
measures the API response time (`kubectl get --raw /readyz`) and on a persistently slow response or
outage stops all tests. The master is never tested at the same time as the workers, but after
them and alone (done by the shared SeriesRunner). The summary is assembled from the node logs.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import ui
from .kube import Kubectl, KubectlError
from .logparse import _STAGE_RE, _parse_sample_line, read_log
from .models import WARN_TEMP, NodeInfo, StressConfig
from .parsing import clean_text, format_duration
from .paths import open_private
from .runner import EXIT_ERROR, EXIT_INTERRUPTED, GATE_TIMEOUT, prep_fraction
from .series import (PREP_SECONDS, _STATUS, NodeOutcome, SeriesOptions, SeriesRunner, estimate_seconds,
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
    ram_pct: Optional[float] = None


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
                info.ram_pct = sample.mem_used_pct
                continue
        if info.stage is None:
            match = _STAGE_RE.search(line)
            if match:
                info.stage = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if info.has_sample and info.stage is not None:
            break
    return info


def describe_state(info: LiveInfo, code: Optional[int], gate: str = "") -> str:
    if code is not None:
        return "done: OK" if code == 0 else _STATUS.get(code, "ERROR")
    if gate == "ready":
        return "READY (waiting)"
    if not info.has_sample:
        return "preparing"
    if info.cooling:
        return "cooldown"
    if info.stage:
        return f"▶ Stage {info.stage[0]}/{info.stage[1]} ({info.stage[2]} %)"
    return "load"


def format_row(name: str, info: LiveInfo, code: Optional[int], remaining: Optional[float],
               gate: str = "") -> str:
    """One row of the live table."""
    cpu = f"{info.cpu:.0f} %" if info.cpu is not None else "-"
    temp = f"{info.temp} °C" if info.temp is not None else "-"
    freq = f"{info.freq} MHz" if info.freq is not None else "-"
    rest = "" if code is not None or remaining is None else f"left ~{format_duration(round(remaining))}"
    return (f"{name:<26}{describe_state(info, code, gate):<24}CPU {cpu:>6}  {temp:>7}  {freq:>9}  {rest}"
            ).rstrip()


def frame_row(name: str, info: LiveInfo, code: Optional[int], bar: tuple, gate: str, on: bool,
              prep_label: str = "", width: int = 112) -> str:
    """One node of the framed table: icons, state, bar of the phase, CPU, temperature, clock, RAM.

    `bar` = (phase, fraction, time text) - see ui.phase_bar."""
    def val(text: str, code_: Optional[str] = None) -> str:
        return ui.paint(text, code_, on) if code_ else text

    if code is not None:
        ok = code == 0
        state = ui.paint(f"{ui.ICON_OK if ok else ui.ICON_FAIL} " + describe_state(info, code),
                         ui.GREEN if ok else ui.RED, on)
    elif gate == "ready":
        state = ui.paint(f"{ui.ICON_READY} READY", ui.YELLOW, on)
    elif not info.has_sample:
        state = ui.paint(f"{ui.ICON_TIME} {prep_label or 'preparing'}", ui.GREY, on)
    elif info.cooling:
        state = ui.paint(f"{ui.ICON_COOL} cooldown", ui.CYAN, on)
    elif info.stage:
        state = ui.paint(f"{ui.ICON_STAGE} {info.stage[0]}/{info.stage[1]} · {info.stage[2]}%", ui.GREEN, on)
    else:
        state = ui.paint(f"{ui.ICON_STAGE} load", ui.GREEN, on)
    phase, fraction, left = bar
    meter = ui.phase_bar(phase, fraction, 12, on) + f" {min(100, int(fraction * 100)):>3}%"
    cpu = f"{info.cpu:.0f}%" if info.cpu is not None else "-"
    temp = f"{info.temp}°C" if info.temp is not None else "-"
    temp_col = ui.temp_code(info.temp) if info.temp is not None else None
    freq = f"{info.freq} MHz" if info.freq is not None else "-"
    ram = f"{info.ram_pct:.0f}%" if info.ram_pct is not None else "-"
    pct = f"{min(100, int(fraction * 100)):>3}%"
    if width < 70:                                # tiny window: name, state and temperature
        return " ".join([f"{ui.ICON_NODE} " + ui.paint(ui.fit(name, 10) + " " * max(0, 10 - len(name)), ui.BOLD, on),
                         ui.fit(state, 11) + " " * max(0, 11 - ui.visible_len(ui.fit(state, 11))), pct,
                         val(f"{temp:>5}", temp_col)]).rstrip()
    if width < 97:                                # narrow window: name, state, bar, CPU and temperature
        return " ".join([f"{ui.ICON_NODE} " + ui.paint(ui.fit(name, 14) + " " * max(0, 14 - len(name)), ui.BOLD, on),
                         state + " " * max(0, 14 - ui.visible_len(state)), meter,
                         f"{ui.ICON_CPU}{cpu:>4}", f"{ui.ICON_TEMP}" + val(f"{temp:>5}", temp_col)]).rstrip()
    cells = [f"{ui.ICON_NODE} " + ui.paint(ui.fit(name, 18) + " " * max(0, 18 - len(name)), ui.BOLD, on),
             state + " " * max(0, 15 - ui.visible_len(state)),
             meter + " " * max(0, 18 - ui.visible_len(meter))]
    if width >= 108:                              # the time left of the node is in the bar row of a narrower window
        cells.append(f"{ui.ICON_TIME} {left:>7}")
    cells += [f"{ui.ICON_CPU} {cpu:>4}", f"{ui.ICON_TEMP} " + val(f"{temp:>5}", temp_col),
              f"{ui.ICON_CLOCK} {freq:>8}", f"{ui.ICON_RAM} {ram:>4}"]
    return " ".join(cells).rstrip()


def read_started(log_file: str) -> Optional[float]:
    """Epoch seconds at which the node's own load started (written by its subprocess), or None while it prepares."""
    try:
        return float(open(log_file + ".gate.started", encoding="utf-8").read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_prep(log_file: str, now: Optional[float] = None) -> Optional[tuple]:
    """(fraction, label) of the node's preparation from its status file (written by the subprocess), or None."""
    try:
        base, share, typical, stamp, label = open(log_file + ".gate.prep", encoding="utf-8").read().split(None, 4)
        now = time.time() if now is None else now
        return prep_fraction(float(base), float(share), float(typical), now - float(stamp)), label.strip()
    except (OSError, ValueError):
        return None


def build_frame(title: str, status: Sequence[str], rows: Sequence[str], width: int, on: bool,
                extra: Sequence[Sequence[str]] = ()) -> list[str]:
    return ui.box(title, [list(status), list(rows), *[list(e) for e in extra]], on, width)


_EVENT_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\]")


def tail_detail(path: str, events: int = 3) -> tuple:
    """(cpu history, temperature history, the last event lines) from the end of a node log - for the tall window."""
    cpu, temp, found = [], [], []
    for raw in read_tail(path).splitlines():
        line = raw.strip()
        parsed = _parse_sample_line(line)
        if parsed:
            cpu.append(parsed[1].cpu_pct)
            temp.append(parsed[1].cpu_temp)
        elif _EVENT_LINE.match(line):
            found.append(clean_text(line, 200))
    return cpu, temp, found[-events:]


def tall_sections(plans: dict, width: int, on: bool, budget: int) -> list:
    """A tall window (>= 40 lines) opens, under the table, a block per node: the history of CPU and temperature and its latest
    events. Blocks are added whole while they fit into `budget` lines (a block costs its rows + 1 for the rule)."""
    from .dashboard import _spark
    room = width - 4
    out = []
    for name, (_node, cfg, log_file, _console) in plans.items():
        cpu, temp, found = tail_detail(log_file)
        if not cpu and not found:
            continue
        spark = max(8, min(40, (room - 24) // 2))
        block = [f"{ui.paint('▸ ' + name, ui.BOLD, on)}   CPU {_spark(cpu, spark, 100, on, ui.GREEN)}   "
                 f"temp {_spark(temp, spark, getattr(cfg, 'max_temp', 85) or 85, on, ui.YELLOW, 25)}"]
        block += [ui.paint("  " + line, ui.GREY, on) for line in found]
        if budget < len(block) + 1:
            break
        budget -= len(block) + 1
        out.append(block)
    return out


class LiveTable:
    """Node status table: redrawn in place in a terminal, otherwise printed once per `interval`."""

    def __init__(self, out: Callable[[str], None], tty: bool, interval: float = 30.0) -> None:
        self.out = out
        self.tty = tty
        self.interval = interval
        self._canvas = ui.Canvas(sys.stdout)
        self._last: Optional[float] = None

    def update(self, lines: Sequence[str], now: Optional[float] = None, force: bool = False,
               frame: Optional[Sequence[str]] = None) -> None:
        """`lines` = plain rows (used without a terminal); `frame` = the same as a ready framed block (terminal)."""
        now = time.monotonic() if now is None else now
        if self.tty:
            if self._last is not None and now - self._last < (1 if frame else 2) and not force:
                return
            shown = list(frame) if frame else [ui.style_line(line) for line in lines]
            self._canvas.draw(shown)
        else:
            if self._last is not None and now - self._last < self.interval and not force:
                return
            self.out(f"[{time.strftime('%H:%M:%S')}] node status:")
            for line in lines:
                self.out(f"  {line}")
        self._last = now

    def close(self) -> None:
        self._canvas.reset()                   # the table stays on the screen, writing continues below it


# --- subprocesses --------------------------------------------------------------------------------

def child_args(cfg: StressConfig, options: SeriesOptions, log_file: str, concurrent: int,
               gate: str = "", ready_timeout: int = 0, rolling: bool = False) -> list[str]:
    """Arguments of the subprocess that tests one worker (no questions, with a fixed path to the log)."""
    args = ["--node", cfg.node, "--yes", "--non-interactive", "--no-background",
            "--log-file", log_file, "--max-temp", str(cfg.max_temp),
            "--cooldown", str(cfg.cooldown), "--interval", str(options.interval),
            "--remaining-every", str(options.remaining_every),
            "--concurrent", str(concurrent), "--notes", cfg.notes, "--export", cfg.export]
    if gate:
        args += ["--start-gate", gate, "--ready-timeout", str(ready_timeout)]
        if rolling:
            args.append("--no-gate-wait")
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
    elif cfg.gpu:
        args += ["--profile", "gpu", "--time", str(cfg.duration), "--gpu-max-temp", str(cfg.gpu_max_temp),
                 "--gpu-mem-pct", str(cfg.gpu_mem_pct)]
        if cfg.gpu_double:
            args.append("--gpu-double")
        if cfg.gpu_image:
            args += ["--gpu-image", cfg.gpu_image]
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

    def __init__(self, *args, api_limit: float = 3.0, sync_start: bool = True,
                 ready_timeout: int = GATE_TIMEOUT, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.api_limit = api_limit
        self.sync_start = sync_start            # all nodes start the load together (after all are READY)
        self.ready_timeout = ready_timeout
        self._prep = 0                          # preparation time not counted in the estimate (after a synchronised start)

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

    @staticmethod
    def _cleanup_gate(log_file: str) -> None:
        for suffix in (".gate.ready", ".gate.go", ".gate.prep", ".gate.started"):
            try:
                os.remove(log_file + suffix)
            except OSError:
                pass

    def _rows(self, plans: dict, procs: dict, now: float, waiting_gate: bool) -> list[str]:
        """Plain rows (no terminal). Every node counts its own time from the start of its own load."""
        rows = []
        for name, (_node, cfg, log_file, _console) in plans.items():
            code = procs[name].poll()
            began = read_started(log_file)
            remaining = None if began is None else max(cfg.total_duration + cfg.cooldown - (time.time() - began), 0.0)
            gate = "ready" if waiting_gate and os.path.exists(log_file + ".gate.ready") else ""
            rows.append(format_row(name, tail_status(log_file), code, remaining, gate))
        return rows

    def _node_bar(self, cfg: StressConfig, info: LiveInfo, code: Optional[int], elapsed: Optional[float],
                  waiting_gate: bool, ready: bool, waited: float, prep: Optional[tuple] = None) -> tuple:
        """(phase, fraction, time text) of a node: preparing / waiting for the others -> load -> cooldown -> done.

        `elapsed` = seconds since the node's OWN load started (None while it is still preparing)."""
        if code is not None:
            return "done", 1.0, "done"
        if elapsed is None:
            if ready:
                return "wait", 1.0, "READY"
            if prep:
                return "wait", prep[0], ""
            if waiting_gate:
                return "wait", min(waited / self.ready_timeout, 1.0), f"max {ui.clock(self.ready_timeout - waited)}"
            return "wait", 0.0, ""
        total, cool = cfg.total_duration, cfg.cooldown
        if info.cooling or elapsed >= total:
            done = max(elapsed - total, 0.0) if not (info.cooling and elapsed < total) else 0.0
            return "cool", (done / cool if cool else 1.0), ui.clock(cool - done)
        return "test", elapsed / total, ui.clock(total - elapsed)

    def _frame(self, plans: dict, procs: dict, started: float, now: float, waiting_gate: bool,
               banner: str) -> list[str]:
        """The framed live table of all workers (terminal)."""
        on = ui.color_enabled()
        waited = now - started                       # since the subprocesses were started
        wall = time.time()
        rows, ready, fractions, begun, nodes = [], 0, [], 0, []
        for name, (_node, cfg, log_file, _console) in plans.items():
            code = procs[name].poll()
            is_ready = waiting_gate and os.path.exists(log_file + ".gate.ready")
            ready += is_ready
            info = tail_status(log_file)
            began = read_started(log_file)
            elapsed = None if began is None else max(wall - began, 0.0)
            begun += began is not None
            prep = None if (began is not None or is_ready) else read_prep(log_file)
            bar = self._node_bar(cfg, info, code, elapsed, waiting_gate, is_ready, waited, prep)
            nodes.append((cfg, elapsed, code))
            fractions.append(1.0 if is_ready else prep[0] if prep else 0.0)
            rows.append(frame_row(name, info, code, bar, "ready" if is_ready else "", on,
                                  prep[1] if prep else "", width=ui.term_width(sys.stdout, 112)))
        width = ui.term_width(sys.stdout, 112)
        length = max(10, min(40, width - 56))
        if waiting_gate:
            overall = sum(fractions) / max(len(fractions), 1)
            status = [f"{ui.ICON_READY} waiting until every node is READY: {ready}/{len(plans)}",
                      ui.phase_bar("wait", overall, length, on)
                      + f" {int(overall * 100):>3} %  READY {ready}/{len(plans)}  "
                        f"{ui.ICON_TIME} max {ui.clock(self.ready_timeout - waited)} left"]
        else:
            per_node = []
            for cfg, elapsed, code in nodes:
                span = cfg.total_duration + cfg.cooldown
                per_node.append(1.0 if code is not None else 0.0 if elapsed is None else min(elapsed / span, 1.0))
            overall = sum(per_node) / max(len(per_node), 1)
            left = max((cfg.total_duration + cfg.cooldown - (e or 0) for cfg, e, c in nodes if c is None and e is not None),
                       default=0)
            everyone = all(c is not None for _cfg, _e, c in nodes)
            if everyone:
                phase, head = "done", f"{ui.ICON_OK} finished"
            elif begun < len(plans):
                phase, head = "test", (f"{ui.ICON_GO} rolling start · {begun}/{len(plans)} nodes started - every node runs "
                                       f"its own full time from its own start")
            else:
                cooling = all(e is not None and e >= c2.total_duration for c2, e, c in nodes if c is None)
                phase, head = ("cool", f"{ui.ICON_COOL} cooldown · the loads have ended, still measuring") if cooling else \
                    ("test", f"{ui.ICON_GO} running · all {len(plans)} nodes started")
            status = [head, ui.phase_bar(phase, overall, length, on)
                      + f" {min(100, int(overall * 100)):>3} %  {ui.ICON_TIME} ~{ui.clock(left)} left"
                      + ("  (all nodes)" if len(plans) > 1 else "")]
        extra = []
        height = ui.term_rows(sys.stdout)
        if height is not None and ui.height_mode(height) == "tall":
            used = len(build_frame("", status, rows, width, on))
            extra = tall_sections(plans, width, on, height - 1 - used)
        return build_frame(f"PARALLEL TEST · {len(plans)} nodes", status, rows, width, on, extra)

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
                self._cleanup_gate(log_file)
                handle = open_private(console, "a")
                handles.append(handle)
                proc = subprocess.Popen(
                    [sys.executable, "-m", "stress_test",
                     *child_args(cfg, opts, log_file, len(workers),
                                 gate=log_file + ".gate" if len(workers) > 1 else "",
                                 ready_timeout=self.ready_timeout, rolling=not self.sync_start)],
                    stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True, env=self._child_env())
                procs[node.name] = proc
                self._log(f"Node {node.name}: subprocess PID {proc.pid}, log {log_file}, "
                          f"output {console}")
            self._say(f"▶ Started {len(workers)} workers at once: "
                      + ", ".join(n.name for n in workers))
            gated = self.sync_start and len(workers) > 1
            gate_open = not gated
            self._prep = PREP_SECONDS if gated else 0
            barrier_from = time.monotonic()
            banner = ""
            announced: set[str] = set()
            begun: set[str] = set()
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
                    if not gate_open and not table.tty:          # text mode: say at once who is READY
                        for n, (_n, _c, lf, _o) in plans.items():
                            if n not in announced and os.path.exists(lf + ".gate.ready"):
                                announced.add(n)
                                self._say(f"[{time.strftime('%H:%M:%S')}] 🚦 {n} READY "
                                          f"({len(announced)}/{len(plans)})")
                    if not gate_open:
                        waiting = [n for n, (_n, _c, lf, _o) in plans.items()
                                   if procs[n].poll() is None and not os.path.exists(lf + ".gate.ready")]
                        late = now - barrier_from > self.ready_timeout
                        if late and waiting:
                            self._log(f"Not ready in {self.ready_timeout} s, left out: {', '.join(waiting)}")
                            self._terminate({n: procs[n] for n in waiting})
                        if late or not waiting:
                            gate_open = True
                            ready = [n for n, (_n, _c, lf, _o) in plans.items()
                                     if procs[n].poll() is None and os.path.exists(lf + ".gate.ready")]
                            for n in ready:
                                open_private(plans[n][2] + ".gate.go", "w").close()
                            started = time.monotonic()
                            self._log(f"[{time.strftime('%H:%M:%S')}] GO for {len(ready)} nodes: {', '.join(ready)}")
                            banner = f"{ui.ICON_GO} GO - load started on {len(ready)} nodes at the same moment"
                            if not table.tty:
                                self._say(f"[{time.strftime('%H:%M:%S')}] {banner}")
                    if not gated and not table.tty:              # rolling start, text mode: say when a node's load starts
                        for n, (_n, _c, lf, _o) in plans.items():
                            if n not in begun and read_started(lf) is not None:
                                begun.add(n)
                                self._say(f"[{time.strftime('%H:%M:%S')}] 🟢 {n} started its load "
                                          f"({len(begun)}/{len(plans)})")
                    table.update(self._rows(plans, procs, now, not gate_open), now,
                                 frame=self._frame(plans, procs, started, now, not gate_open, banner))
                    if now >= next_api:
                        next_api = now + api_every
                        if guard.check(self.kube):
                            self.abort_reason = (f"API response {guard.last_seconds:.1f} s "
                                                 f"(limit {guard.limit:g} s), stopping "
                                                 f"all tests")
                            break
            except KeyboardInterrupt:
                interrupted = True
            table.update(self._rows(plans, procs, time.monotonic(), not gate_open), force=True,
                         frame=self._frame(plans, procs, started, time.monotonic(), not gate_open, banner))
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
            for _n, _c, lf, _o in plans.values():
                self._cleanup_gate(lf)
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
