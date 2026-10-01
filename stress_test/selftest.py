"""FULL SELF-TEST: the whole cluster in one go (network, disks, CPU, RAM) and one big report.

The phases are the ordinary tests of this tool, started one after another with their normal options:
network first (hot nodes would skew it), then disks, CPU (stepped) and RAM. Between the phases it waits
until the nodes cool down. At the end the results of all phases are read back from the logs and put
into one report (text log + JSON + a framed verdict on the screen).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import statistics
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import ui
from . import gpu as gpumod
from .disk import jobs as disk_jobs
from .kube import Kubectl, KubectlError
from .logparse import read_log
from .manifests import PROBE_SCRIPT, probe_pod
from .models import DEFAULT_MAX_TEMP, MASTER_CPU_CAP, MASTER_MAX_TEMP, WARN_TEMP, PodNames
from .parsing import describe_duration, parse_probe_output
from .paths import make_private_dir, open_private, user_log_dir
from .summary import run_stats

log = logging.getLogger(__name__)

LEVELS = {
    # name: (net_time, disk_size MiB, disk_job_time, step_time, ram_time, cooldown) - all in seconds
    "quick": dict(net_time=5, disk_size=512, disk_job=10, step_time=60, ram_time=60, cooldown=60, gpu_time=60),
    "standard": dict(net_time=10, disk_size=1024, disk_job=15, step_time=120, ram_time=120, cooldown=120, gpu_time=120),
    "thorough": dict(net_time=20, disk_size=2048, disk_job=30, step_time=240, ram_time=300, cooldown=180, gpu_time=300),
}
STEPS = (25, 50, 75, 100)
RAM_PCT, RAM_CPU_PCT = 70, 50          # the RAM phase: 70 % of the free RAM and half of the CPU

# Waiting for the nodes to cool down between the phases. The target is the node's own idle temperature
# measured before anything started + COOL_MARGIN (never below COOL_FLOOR): a value the node can really reach in this room.
# The wait stops at once when it is reached, when the hottest node stopped falling (PLATEAU_WINDOW s with less than
# PLATEAU_DROP °C of improvement - the room / case limit), or after COOL_MAX_WAIT s at the latest.
COOL_MARGIN, COOL_FLOOR = 8, 45
COOL_MAX_WAIT, PLATEAU_WINDOW, PLATEAU_DROP, COOL_STEP = 600, 120, 1.0, 15

ACK_WORD = "START"


@dataclass
class Phase:
    key: str
    title: str
    argv: list
    seconds: int                      # estimate


@dataclass
class NodeReport:
    name: str
    master: bool = False
    idle: Optional[int] = None
    temp_max: Optional[int] = None
    throttling: bool = False
    throttle_text: str = ""
    cpu_ops: Optional[float] = None          # bogo ops/s of the stepped test (100 % stage)
    ram_max_mib: Optional[int] = None
    ram_max_pct: Optional[float] = None
    disk: dict = field(default_factory=dict)  # job name -> (MB/s, IOPS)
    disk_health: list = field(default_factory=list)
    specs: list = field(default_factory=list)            # the HARDWARE block of the log (CPU, RAM modules, GPU, disks...)
    net_avg: Optional[float] = None           # average Mbit/s of the uncapped pairs where the node takes part
    gpu: Optional[dict] = None                # GPU test summary (only nodes with an NVIDIA GPU)
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "FAIL" if self.problems else "WARN" if self.warnings else "OK"


# --- plan -----------------------------------------------------------------------------------

def level_settings(level: str) -> dict:
    return LEVELS[level]


def plan_phases(level: str, log_dir: Path, workers: int, with_master: bool, nodes_arg: str = "",
                gpu_nodes: Optional[list] = None, subset: bool = False, ram_nodes: str = "") -> list[Phase]:
    """The phases of the self-test with the command-line options of each (like a person would type them)."""
    s = level_settings(level)
    base = ["--yes", "--non-interactive", "--log-dir", str(log_dir), "--log-flat", "--notes", "full self-test"]
    scope = (["--nodes", nodes_arg, "--parallel"] if subset
             else ["--cluster" if with_master else "--workers", "--parallel"])      # subset: only the nodes given with --nodes
    n_total = workers + (1 if with_master else 0)
    pairs = n_total * (n_total - 1)
    n_jobs = len(disk_jobs())
    cpu_each = 90 + len(STEPS) * s["step_time"] + s["cooldown"]
    gpu_phases = [
        Phase(f"gpu-{name}", f"GPU burn on {name} (NVIDIA, alone - never together with the CPU load)",
              ["--profile", "gpu", "--node", name, "--time", str(s["gpu_time"]), "--cooldown", str(s["cooldown"]), *base],
              180 + s["gpu_time"] + s["cooldown"] + 300)      # + the first pull of the CUDA image and the gpu-burn build
        for name in (gpu_nodes or [])]
    phases = [
        Phase("net", "Network matrix (every node to every node)",
              ["--net-matrix", "--net-time", str(s["net_time"]), *(["--nodes", nodes_arg] if nodes_arg else []), *base],
              pairs * (s["net_time"] + 8) + 90),
        Phase("disk", "Disks (fio benchmark + SMART)",
              [*scope, "--profile", "disk", "--smart", "--allow-bad-disk", "--disk-size", str(s["disk_size"]),
               "--disk-job-time", str(s["disk_job"]), "--cooldown", "0", *base],
              120 + n_jobs * s["disk_job"] + (120 if with_master else 0)),
        Phase("cpu", "CPU: stepped 25 / 50 / 75 / 100 %, temperatures, clock, throttling",
              [*scope, "--profile", "stepped", "--steps", ",".join(str(x) for x in STEPS),
               "--step-time", str(s["step_time"]), "--cooldown", str(s["cooldown"]),
               "--hw-privileged", *base],          # a read-only privileged pod reads RAM modules, GPU, disks
              cpu_each + (cpu_each if with_master else 0)),
        Phase("ram", "RAM: allocation under a moderate CPU load (workers only)",
              [*(["--nodes", ram_nodes] if subset else ["--workers"]), "--parallel", "--profile", "classic", "--time", str(s["ram_time"]),
               "--cpu-load", str(RAM_CPU_PCT), "--ram-pct", str(RAM_PCT), "--cooldown", "0", *base],
              90 + s["ram_time"]),
    ]
    if n_total < 2:                       # the matrix needs two nodes
        phases = [ph for ph in phases if ph.key != "net"]
    cpu_at = next(i for i, ph in enumerate(phases) if ph.key == "cpu") + 1
    return phases[:cpu_at] + gpu_phases + phases[cpu_at:]


def estimate(phases: list[Phase]) -> tuple[int, int]:
    """(seconds of the phases, seconds of the longest possible cooling pauses between them)."""
    return sum(p.seconds for p in phases), COOL_MAX_WAIT * (len(phases) - 1)


# --- screens and confirmation -------------------------------------------------------------------

WARNINGS = (
    "This test runs the whole cluster at FULL POWER - nothing else should run on it.",
    "Services on the nodes will be slow or unavailable while the tests run; the cluster will NOT work normally.",
    "Every node is loaded to 100 % of CPU at the same time, plus disks, RAM and the network.",
    "Power draw of the whole rack rises sharply (PSU, UPS, circuit breaker) - check that the supply can carry it.",
    "Room temperature matters: check cooling and ventilation, the nodes will get hot.",
    "The internal network is saturated (every pair of nodes at line rate) - other traffic will suffer.",
    f"Tests run in parallel and start together; the master (if included) runs last, alone, with a CPU cap of {MASTER_CPU_CAP} %.",
    f"A node is stopped automatically at {DEFAULT_MAX_TEMP} °C ({MASTER_MAX_TEMP} °C on the master).",
    "Hardware details (RAM modules, GPU, disks) are read by a short READ-ONLY privileged pod, and disk health (SMART) by another one.",
)


def human(seconds: float) -> str:
    """'about 35 min' style duration."""
    return f"{int(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


def _box_width(columns: Optional[int], limit: int = 100) -> Optional[int]:
    """Width of a frame of this screen: the classic `limit` without a terminal, else what the terminal allows."""
    if columns is None and not ui.adaptive():
        return None
    return ui.avail(max(limit, 120) if ui.mode(columns) == "wide" else limit, columns)


def warning_box(on: bool, columns: Optional[int] = None) -> list[str]:
    width = _box_width(columns)
    rows = [ui.paint("READ THIS BEFORE YOU START", ui.RED, on)]
    for w in WARNINGS:                       # wrapped, so that nothing is cut at the edge of the frame
        if width is None:
            wrapped = textwrap.wrap(w, 92) or [w]
        else:
            wrapped = ui.wrap(w, width - 4 - 2)
        rows.append(f"{ui.paint('▸', ui.YELLOW, on)} {wrapped[0]}")
        rows += [f"  {part}" for part in wrapped[1:]]
    title = f"{ui.ICON_FAIL} FULL SELF-TEST - WHOLE CLUSTER AT FULL POWER"
    if width is not None and ui.visible_len(title) > width - 10:       # a short title for a narrow terminal
        title = f"{ui.ICON_FAIL} FULL SELF-TEST" if width < 60 else title
    return ui.box(title, [rows], on, width or 100)


def plan_box(phases: list[Phase], level: str, nodes: list, on: bool, columns: Optional[int] = None) -> list[str]:
    work, cool = estimate(phases)
    width = _box_width(columns)
    names = ", ".join(n.name + (" (master)" if n.is_control_plane else "") for n in nodes)
    rows = [f"{ui.ICON_NODE} " + names]
    plan = [f"{i}. {p.title:<70} ~{human(p.seconds)}" for i, p in enumerate(phases, 1)]
    tail = [f"{ui.ICON_TIME} about {human(work)} + cooling pauses between the phases "
            f"(at most {human(cool)}, usually much less)",
            f"level: {level} · results: one report (log + JSON) in the log folder"]
    if width is not None:                     # the terminal: everything wrapped to the frame, nothing cut
        room = width - 4
        rows = ui.wrap(names, room - 3, f"{ui.ICON_NODE} ", "   ")
        plan = [ln for i, p in enumerate(phases, 1)
                for ln in ui.wrap(f"{p.title}  ~{human(p.seconds)}", room - 3, f"{i}. ", "   ")]
        tail = [ln for part in tail for ln in ui.wrap(part, room, "", "  ")]
    return ui.box(f"{ui.ICON_STAGE} PLAN", [rows, plan, tail], on, width or 100)


def confirm(args: argparse.Namespace, ask: Callable, phases: list[Phase], level: str, nodes: list) -> bool:
    """Two confirmations: (1) the warnings were understood, (2) the word START is typed."""
    on = ui.color_enabled()
    print("\n" + "\n".join(warning_box(on)))
    print("\n" + "\n".join(plan_box(phases, level, nodes, on)) + "\n")
    if not (args.yes or ask("I understand: the cluster will be fully loaded and unusable meanwhile (y/n)", "n").lower() in ("y", "yes")):
        return False
    if getattr(args, "self_test_ack", False):
        return True
    if args.non_interactive:
        ui.emit(f"❌ Non-interactive start needs --self-test-ack (second confirmation).")
        return False
    return ask(f"Second confirmation - type {ACK_WORD} to begin", "").strip() == ACK_WORD


# --- temperatures and the cooling pause ---------------------------------------------------------

class Probes:
    """Short-lived temperature probe pods on the given nodes (they must be gone before a test starts)."""

    def __init__(self, kube: Kubectl, nodes: list) -> None:
        self.kube, self.nodes = kube, nodes
        self.names = {n.name: PodNames.new() for n in nodes}
        self.ready: list = []

    def __enter__(self) -> "Probes":
        for node in self.nodes:
            self.kube.apply(probe_pod(node.name, self.names[node.name], 1800))
        for node in self.nodes:
            try:
                if self.kube.wait_ready(self.names[node.name].probe, 90):
                    self.ready.append(node.name)
            except KubectlError:
                log.warning("Probe pod on %s did not start", node.name)
        return self

    def __exit__(self, *exc) -> None:
        self.kube.delete_pods(*[n.probe for n in self.names.values()])

    def temps(self) -> dict:
        out = {}
        for name in self.ready:
            try:
                out[name] = parse_probe_output(self.kube.exec(self.names[name].probe, PROBE_SCRIPT)).cpu_temp
            except KubectlError:
                out[name] = None
        return out


def cool_targets(idle: dict) -> dict:
    """Temperature each node should fall to: its own idle temperature + COOL_MARGIN, at least COOL_FLOOR."""
    return {name: max(temp + COOL_MARGIN, COOL_FLOOR) for name, temp in idle.items() if temp is not None}


def cooling_decision(history: list, targets: dict, now_temps: dict, elapsed: float) -> str:
    """'cooled' (all at the target), 'plateau' (stopped falling), 'timeout' or '' (keep waiting).

    `history` = [(elapsed, hottest excess over the target)] of the earlier readings."""
    excess = {n: (t - targets[n]) for n, t in now_temps.items() if t is not None and n in targets}
    if not excess or max(excess.values()) <= 0:
        return "cooled"
    if elapsed >= COOL_MAX_WAIT:
        return "timeout"
    old = [h for t, h in history if elapsed - t >= PLATEAU_WINDOW]
    if old and (old[-1] - max(excess.values())) < PLATEAU_DROP:
        return "plateau"
    return ""


def cool_wait(kube: Kubectl, nodes: list, idle: dict, out: Callable[[str], None], sleep: Callable = time.sleep) -> str:
    """Waits until the nodes are cool enough for the next phase; returns why it stopped."""
    targets = cool_targets(idle)
    if not targets:
        return "no sensor"
    on = ui.color_enabled()
    live = ui.LiveScreen.wanted()
    canvas, start, history = ui.Canvas(sys.stdout), time.monotonic(), []
    with Probes(kube, [n for n in nodes if n.name in targets]) as probes:
        first_excess = None
        while True:
            temps = probes.temps()
            elapsed = time.monotonic() - start
            why = cooling_decision(history, targets, temps, elapsed)
            excess = [t - targets[n] for n, t in temps.items() if t is not None and n in targets]
            worst = max(excess) if excess else 0
            first_excess = worst if first_excess is None else first_excess
            history.append((elapsed, worst))
            fraction = 1.0 if why == "cooled" else (1 - worst / first_excess) if first_excess and first_excess > 0 else 1.0
            width = ui.term_width(sys.stdout, 100 if ui.mode() != "wide" else 120)
            name_w = max(8, min(28, width - 32))
            rows = []
            for node in nodes:
                t = temps.get(node.name)
                if node.name not in targets or t is None:
                    continue
                ok = t <= targets[node.name]
                shown = ui.fit(node.name, name_w)
                rows.append(f"{ui.ICON_NODE} {shown}{' ' * max(0, name_w - ui.visible_len(shown))} {ui.ICON_TEMP} "
                            + ui.paint(f"{t:>3} °C", ui.GREEN if ok else ui.temp_code(t), on)
                            + f"  → {targets[node.name]} °C  " + (ui.ICON_OK if ok else ui.ICON_TIME))
            head = ("cooling pause - waiting until the nodes are near their idle temperature" if width >= 84
                    else "cooling pause - waiting for the nodes")
            status = [f"{ui.ICON_COOL} {head}",
                      ui.phase_bar("cool", max(0.0, min(1.0, fraction)), max(8, min(40, width - 34)), on)
                      + f" {ui.ICON_TIME} {ui.clock(elapsed)} / max {ui.clock(COOL_MAX_WAIT)}"]
            if live:
                canvas.draw(ui.box("COOLING", [status, rows], on, width))
            elif int(elapsed) % 60 < COOL_STEP:
                out(f"[{time.strftime('%H:%M:%S')}] cooling: " + ", ".join(
                    f"{n} {t}°C→{targets.get(n, '?')}" for n, t in temps.items()))
            if why:
                return why
            sleep(COOL_STEP)


# --- running the phases ---------------------------------------------------------------------

def run_phase(argv: list) -> int:
    """One phase = the normal command line of the tool (argparse errors do not stop the self-test)."""
    from . import cli
    try:
        return cli._main(cli.build_parser().parse_args(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


# --- report -------------------------------------------------------------------------------------

def collect(log_dir: Path, nodes: list) -> tuple[dict, dict]:
    """(NodeReport per node, net-matrix dict or {}) read back from the results of the phases."""
    reports = {n.name: NodeReport(n.name, master=n.is_control_plane) for n in nodes}
    matrix = {}
    for path in sorted(log_dir.glob("net-matrix-*.json")):
        try:
            matrix = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    for path in sorted(log_dir.glob("*.log")):
        if path.name.startswith(("net-matrix", "cluster-", "full-selftest")):
            continue
        try:
            run = read_log(str(path))
        except OSError:
            continue
        rep = reports.get(run.node)
        if rep is None:
            continue
        specs = hardware_block(path)
        if specs and (not rep.specs or len(specs) > len(rep.specs)):
            rep.specs = specs
        if run.profile == "gpu":
            summary = gpumod.summarize(run.samples)
            rep.gpu = dataclasses.asdict(summary) if summary else None
            continue
        if run.profile == "disk":
            rep.disk = {r.name: (r.mb_s, r.iops) for r in run.disk_results}
            rep.disk_health = list(run.disk_health)
            continue
        if not run.samples:
            continue
        targets = run.stage_targets or None
        stats = run_stats(run.samples, run.baseline_temp, WARN_TEMP, stage_targets=targets)
        if run.profile == "stepped":
            rep.idle = run.baseline_temp
            rep.temp_max = stats.temp_max
            rep.throttling, rep.throttle_text = stats.throttling, stats.throttling_text
            cpu = [m.ops_per_s for m in run.metrics if m.name == "cpu"] if run.metrics else []
            # the performance of the last (highest) stage is the most telling, else the overall number
            rep.cpu_ops = run.stage_ops[max(run.stage_ops)] if run.stage_ops else (cpu[0] if cpu else None)
        else:                                          # the RAM phase (classic with --ram-pct)
            ram = [s for s in run.samples if s.mem_used_mib is not None]
            if ram:
                top = max(ram, key=lambda s: s.mem_used_mib)
                rep.ram_max_mib, rep.ram_max_pct = top.mem_used_mib, top.mem_used_pct
            if stats.temp_max is not None and (rep.temp_max is None or stats.temp_max > rep.temp_max):
                rep.temp_max = stats.temp_max
    for pair in matrix.get("pairs", []):
        if pair.get("mbps") and not pair.get("capped"):
            for name in (pair["client"], pair["server"]):
                if name in reports:
                    reports[name].__dict__.setdefault("_net", []).append(pair["mbps"])
    for rep in reports.values():
        values = rep.__dict__.pop("_net", [])
        rep.net_avg = statistics.mean(values) if values else None
    return reports, matrix


def hardware_block(path) -> list:
    """The lines of the '=== HARDWARE ===' section of a test log."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("=== HARDWARE"):
            inside = True
            continue
        if inside and line.startswith("==="):
            break
        if inside and line.strip():
            lines.append(line.strip())
    return lines


_HDD_MODEL = re.compile(r"\b(ST\d{3,}[A-Z]{1,3}\d*|WD\d+[A-Z]{2,}|HTS\d+|MQ\d+|HDD|TOSHIBA [A-Z]{2}\d+|HGST|WDC)", re.IGNORECASE)
SLOW_RAND_MB, SLOW_RAND_IOPS = 5.0, 500          # random read below this = a mechanical or ailing disk


def slow_disk_note(rep: "NodeReport") -> str:
    """A warning with the disk name and the probable reason when the random I/O of the node is very slow."""
    rand = rep.disk.get("rand-read")
    if not rand or (rand[0] >= SLOW_RAND_MB and rand[1] >= SLOW_RAND_IOPS):
        return ""
    model = next((ln.split(":", 1)[1].split("|")[0].strip() for ln in rep.disk_health if ":" in ln and "|" in ln), "")
    if not model:
        model = next((ln.split("|")[-1].strip() for ln in rep.specs if ln.startswith("Disk ") and "HDD" in ln), "")
    hours = next((m.group(1) for ln in rep.disk_health for m in [re.search(r"\| (\d+) h\b", ln)] if m), "")
    mech = bool(_HDD_MODEL.search(model)) or any(ln.startswith("Disk ") and "| HDD |" in ln for ln in rep.specs)
    why = ("a mechanical hard disk (HDD): about 100-300 random IOPS is normal for it, an SSD does 10-100x more; the "
           "node is fine for light services but slow for databases and image pulls" if mech else
           "the disk is either mechanical, failing, in power-saving mode, or something else was using it during the test")
    age = f", {int(hours) // 8766} years of power-on time" if hours.isdigit() and int(hours) >= 8766 else ""
    return (f"slow disk{f' {model}' if model else ''}: random read {rand[0]:.1f} MB/s / {rand[1]:.0f} IOPS{age} - {why}")


def judge(reports: dict, matrix: dict, codes: dict, max_temp: int = DEFAULT_MAX_TEMP) -> None:
    """Problems (FAIL) and warnings per node."""
    for rep in reports.values():
        limit = MASTER_MAX_TEMP if rep.master else max_temp
        if rep.temp_max is not None and rep.temp_max >= limit - 2:
            rep.problems.append(f"reached {rep.temp_max} °C (limit {limit} °C)")
        elif rep.temp_max is not None and rep.temp_max >= WARN_TEMP:
            rep.warnings.append(f"hot: {rep.temp_max} °C")
        if rep.throttling:
            rep.warnings.append(f"CPU throttling ({rep.throttle_text})")
        for line in rep.disk_health:
            if "⚠" in line or line.strip().upper().startswith("WARNING"):
                text = line.strip().lstrip("⚠️ ").strip()
                rep.warnings.append(f"disk health: {text[8:].strip() if text.upper().startswith('WARNING') else text}")
        slow = slow_disk_note(rep)
        if slow:
            rep.warnings.append(slow)
        if rep.gpu:
            rep.warnings += [f"GPU: {f}" for f in rep.gpu.get("findings", [])]
        gpu_code = codes.get(f"gpu-{rep.name}")
        if gpu_code not in (None, 0):
            rep.problems.append(f"GPU test did not finish well (exit code {gpu_code}: overheating, missing driver/plugin or error)")
        if rep.net_avg is not None and rep.net_avg < 500:
            rep.warnings.append(f"slow network: {rep.net_avg:.0f} Mbit/s on average")
        if rep.cpu_ops is None and codes.get("cpu") is not None and not rep.master:
            rep.problems.append("no CPU test result")
    for skipped, why in (matrix.get("skipped") or {}).items():
        if skipped in reports:
            reports[skipped].problems.append(f"network test skipped: {why}")
    for note in matrix.get("findings") or []:
        for name, rep in reports.items():
            if note.startswith(name) and "was not tested" not in note:
                rep.warnings.append(note)


def report_lines(meta: dict, reports: dict, matrix: dict, codes: dict, phases: list[Phase]) -> list[str]:
    lines = ["=" * 76, "FULL SELF-TEST REPORT", "=" * 76,
             f"Started:   {meta['started']}", f"Finished:  {meta['finished']}",
             f"Level:     {meta['level']}    Nodes: {len(reports)}    Duration: {human(meta['seconds'])}", "",
             "PHASES"]
    for p in phases:
        code = codes.get(p.key)
        lines.append(f"  {'✅' if code == 0 else '⚠️' if code is None else '❌'} {p.title}"
                     + ("" if code in (0, None) else f" (exit code {code})"))
    lines += ["", "NODES", f"  {'node':<28}{'idle→max °C':<13}{'throttle':<10}{'cpu bogo/s':>11}{'RAM max':>10}"
              f"{'disk rd/wr MB/s':>18}{'net Mbit/s':>12}  verdict"]
    for rep in reports.values():
        temp = f"{rep.idle if rep.idle is not None else '?'}→{rep.temp_max if rep.temp_max is not None else '?'}"
        rd, wr = rep.disk.get("seq-read"), rep.disk.get("seq-write")
        disk = f"{rd[0]:.0f}/{wr[0]:.0f}" if rd and wr else (f"{rd[0]:.0f}/-" if rd else "-")
        lines.append(f"  {rep.name + (' (M)' if rep.master else ''):<28}{temp:<13}{'yes' if rep.throttling else 'no':<10}"
                     f"{(f'{rep.cpu_ops:.0f}' if rep.cpu_ops else '-'):>11}"
                     f"{(f'{rep.ram_max_pct:.0f} %' if rep.ram_max_pct is not None else '-'):>10}{disk:>18}"
                     f"{(f'{rep.net_avg:.0f}' if rep.net_avg else '-'):>12}  {rep.verdict}")
    gpu_rows = [r for r in reports.values() if r.gpu]
    if gpu_rows:
        lines += ["", "GPU (gpu-burn + nvidia-smi)"]
        for r in gpu_rows:
            g = r.gpu
            power = f"{g['avg_power']:.0f} W" if g.get("avg_power") is not None else "power N/A"
            lines.append(f"  {r.name:<28}max {g['max_temp'] if g['max_temp'] is not None else '?'} °C  "
                         f"clock {g['clock_min']}-{g['clock_max']} MHz  {power}  throttle {g['throttle_pct']:.0f} %")
    detail = [(r.name, t, "❌") for r in reports.values() for t in r.problems] + \
             [(r.name, t, "⚠️") for r in reports.values() for t in r.warnings]
    if detail:
        lines += ["", "FINDINGS"] + [f"  {icon} {name}: {text}" for name, text, icon in detail]
    if any(r.specs for r in reports.values()):
        lines += ["", "HARDWARE"]
        for rep in reports.values():
            if rep.specs:
                lines.append(f"  {rep.name}")
                lines += [f"    {ln}" for ln in rep.specs]
    disk_rows = [(r.name, r.disk) for r in reports.values() if r.disk]
    if disk_rows:
        lines += ["", "DISKS (MB/s | IOPS)"]
        for name, res in disk_rows:
            lines.append(f"  {name:<28}" + "  ".join(f"{k} {v[0]:.0f}|{v[1]:.0f}" for k, v in res.items()))
    if matrix.get("pairs"):
        lines += ["", "NETWORK MATRIX (Mbit/s, row = client, column = server; * = capped on purpose)"]
        names = [n for n in matrix.get("nodes", []) if n not in (matrix.get("skipped") or {})]
        lines.append("  " + " " * 28 + "".join(f"[{i}]".rjust(8) for i in range(1, len(names) + 1)))
        for i, client in enumerate(names, 1):
            cells = []
            for server in names:
                pair = next((p for p in matrix["pairs"] if p["client"] == client and p["server"] == server), None)
                cells.append("-".rjust(8) if client == server else
                             (f"{pair['mbps']:.0f}" + ("*" if pair["capped"] else "")).rjust(8) if pair and pair.get("mbps")
                             else "x".rjust(8))
            lines.append(f"  [{i}] {client:<24}" + "".join(cells))
    bad = sum(1 for r in reports.values() if r.verdict == "FAIL")
    warn = sum(1 for r in reports.values() if r.verdict == "WARN")
    lines += ["", "=" * 76,
              ("VERDICT: ✅ the whole cluster is healthy" if not bad and not warn else
               f"VERDICT: {'❌' if bad else '⚠️'} {bad} node(s) with problems, {warn} with warnings, "
               f"{len(reports) - bad - warn} OK"), "=" * 76]
    return lines


def framed_verdict(reports: dict, lines: list[str], on: bool, columns: Optional[int] = None) -> list[str]:
    """The end of the run on the screen: one row per node and the verdict."""
    width = _box_width(columns, 110)
    verdict = lines[-2]
    findings = [ln.strip() for ln in lines if ln.strip().startswith(("❌", "⚠️")) and ":" in ln][:8]
    bad = any(r.verdict == "FAIL" for r in reports.values())
    warn = any(r.verdict == "WARN" for r in reports.values())
    title = f"{ui.ICON_FAIL if bad else '⚠️' if warn else ui.ICON_OK} FULL SELF-TEST - RESULT"
    if width is not None:                                # the terminal: a table whose columns drop by priority
        room = width - 4
        cells = []
        for rep in reports.values():
            mark = {"OK": ui.paint("✅ OK", ui.GREEN, on), "WARN": ui.paint("⚠️ WARN", ui.YELLOW, on),
                    "FAIL": ui.paint("❌ FAIL", ui.RED, on)}[rep.verdict]
            rd = rep.disk.get("seq-read")
            cells.append([ui.ICON_NODE + " " + rep.name, mark,
                          f"{rep.temp_max} °C" if rep.temp_max is not None else "-",
                          f"{rep.cpu_ops:.0f}" if rep.cpu_ops else "-",
                          f"{rep.ram_max_pct:.0f} %" if rep.ram_max_pct is not None else "-",
                          f"{rd[0]:.0f} MB/s" if rd else "-",
                          f"{rep.net_avg:.0f}" if rep.net_avg else "-"])
        rows = ui.table([("node", 0, "<"), ("result", 0, "<"), ("temp", 1, ">"), ("cpu b/s", 2, ">"), ("RAM", 3, ">"),
                         ("disk rd", 4, ">"), ("net Mbit/s", 5, ">")], cells, room, ui.mode((columns or ui.cols())))
        sections = [rows, ui.wrap(verdict.replace("VERDICT: ", ""), room)]
        if findings:
            sections.insert(1, [ln for f in findings for ln in ui.wrap(f, room, "", "   ")])
        return ui.box(title, sections, on, width)
    rows = []
    for rep in reports.values():
        mark = {"OK": ui.paint("✅ OK  ", ui.GREEN, on), "WARN": ui.paint("⚠️ WARN", ui.YELLOW, on),
                "FAIL": ui.paint("❌ FAIL", ui.RED, on)}[rep.verdict]
        temp = f"{rep.temp_max} °C" if rep.temp_max is not None else "-"
        rd = rep.disk.get("seq-read")
        rows.append(f"{ui.ICON_NODE} {ui.fit(rep.name, 26):<26} {mark}  {ui.ICON_TEMP} {temp:>6}  "
                    f"{ui.ICON_CPU} {(f'{rep.cpu_ops:.0f}' if rep.cpu_ops else '-'):>7}  "
                    f"{ui.ICON_RAM} {(f'{rep.ram_max_pct:.0f}%' if rep.ram_max_pct is not None else '-'):>4}  "
                    f"💾 {(f'{rd[0]:.0f} MB/s' if rd else '-'):>9}  {ui.ICON_PING} {(f'{rep.net_avg:.0f}' if rep.net_avg else '-'):>5}")
    sections = [rows, [verdict.replace("VERDICT: ", "")]]
    if findings:
        sections.insert(1, findings)
    return ui.box(title, sections, on, 110)


# --- main -------------------------------------------------------------------------------------

def choose_level(args: argparse.Namespace, ask: Callable) -> str:
    if getattr(args, "self_test_level", None):
        return args.self_test_level
    if args.non_interactive:
        return "standard"
    ui.show_choices("HOW THOROUGH", [
        ("1", "quick", "about 15 min for a few nodes (short steps)"),
        ("2", "standard", "about 35 min (recommended)"),
        ("3", "thorough", "about 70 min (long steps, bigger disk test)")])
    while True:
        raw = ask("Level", "2")
        if raw in ("1", "2", "3"):
            return ("quick", "standard", "thorough")[int(raw) - 1]
        ui.warn("Invalid choice.")


def run(args: argparse.Namespace, kube: Optional[Kubectl] = None, ask: Optional[Callable] = None,
        runner: Callable[[list], int] = run_phase, waiter: Optional[Callable] = None) -> int:
    """--self-test: asks, confirms twice, runs all phases and writes the report. Returns an exit code."""
    from . import cli
    kube = kube or Kubectl()
    ask = ask or cli.ask
    try:
        available = [kube.get_node(n) for n in kube.list_node_names()]
    except KubectlError as exc:
        ui.emit(f"❌ {exc}")
        return cli.EXIT_ERROR
    nodes = [n for n in available if n.ready]
    subset = bool(getattr(args, "nodes", None))
    if subset:                                         # --nodes A,B: only these (a master is included only when named)
        wanted = [x.strip() for x in args.nodes.split(",") if x.strip()]
        unknown = [w for w in wanted if w not in {n.name for n in nodes}]
        if unknown:
            ui.emit(f"❌ Not a Ready node: {', '.join(unknown)}")
            return cli.EXIT_ERROR
        nodes = [n for n in nodes if n.name in wanted]
    workers = [n for n in nodes if not n.is_control_plane]
    masters = [n for n in nodes if n.is_control_plane]
    if len(nodes) < 1 or not workers:
        ui.emit("❌ The self-test needs at least one Ready worker.")
        return cli.EXIT_ERROR
    busy = [p for p in kube.list_tool_pods() if p.phase not in ("Succeeded", "Failed")]
    if busy:
        ui.emit(f"❌ A test of this tool is running right now ({', '.join(p.name for p in busy)}); wait for it.")
        return cli.EXIT_ERROR
    level = choose_level(args, ask)
    with_master = False
    if subset:
        with_master = bool(masters)
    elif masters:
        with_master = bool(args.include_master) or (not args.non_interactive and ask(
            f"Test the master ({masters[0].name}) too? It is the API of the cluster; it runs last, capped at "
            f"{MASTER_CPU_CAP} % CPU (y/n)", "n").strip().lower() in ("y", "yes"))
    chosen = workers + (masters if with_master else [])
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    directory = make_private_dir(user_log_dir(args.log_dir) / f"selftest-{stamp}")
    phases = plan_phases(level, directory, len(workers), with_master,
                         ",".join(n.name for n in chosen) if subset or len(chosen) != len(available) else "",
                         gpu_nodes=[n.name for n in chosen if n.gpu_count > 0],
                         subset=subset, ram_nodes=",".join(n.name for n in workers))
    if not confirm(args, ask, phases, level, chosen):
        ui.emit("Cancelled, nothing was started.")
        return 0
    started = time.time()
    waiter = waiter or (lambda idle: cool_wait(kube, chosen, idle, ui.emit))
    idle: dict = {}
    try:
        with Probes(kube, chosen) as probes:
            idle = probes.temps()
    except KubectlError as exc:
        log.warning("Idle temperatures could not be read: %s", exc)
    codes: dict = {}
    try:
        for index, phase in enumerate(phases, 1):
            ui.emit(f"\n{ui.ICON_STAGE} PHASE {index}/{len(phases)}: {phase.title}")
            codes[phase.key] = runner(phase.argv)
            if codes[phase.key] == cli.EXIT_INTERRUPTED:
                ui.emit("🛑 Interrupted - the report is built from what has finished.")
                break
            if index < len(phases):
                why = waiter(idle)
                ui.emit(f"{ui.ICON_COOL} Cooling pause finished ({why}).")
    except KeyboardInterrupt:
        ui.emit("\n🛑 Interrupted - the report is built from what has finished.")
    reports, matrix = collect(directory, chosen)
    judge(reports, matrix, codes)
    for name, temp in idle.items():
        if name in reports and reports[name].idle is None:
            reports[name].idle = temp
    meta = {"started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level, "seconds": int(time.time() - started)}
    lines = report_lines(meta, reports, matrix, codes, phases)
    log_path, json_path = directory / f"full-selftest-{stamp}.log", directory / f"full-selftest-{stamp}.json"
    with open_private(str(log_path), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open_private(str(json_path), "w") as fh:
        json.dump({"schema_version": 1, "kind": "full-selftest", **meta, "codes": codes, "net_matrix": matrix,
                   "nodes": {n: {**r.__dict__, "verdict": r.verdict} for n, r in reports.items()}}, fh, indent=1, default=str)
    print("\n" + "\n".join(framed_verdict(reports, lines, ui.color_enabled())))
    ui.emit(f"\n📁 Report: {log_path}\n📄 {json_path}")
    return 0 if all(r.verdict != "FAIL" for r in reports.values()) else cli.EXIT_ERROR
