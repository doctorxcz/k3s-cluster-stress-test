"""Live STATUS screen (menu key W, `--status --live`): every running and planned test, refreshed every second.

It reads only local files - the registry of running tests (.logs/running/) and the result log of every test, which the runner
fills with a measurement every few seconds - so it asks the cluster for nothing and loads nothing. What it shows depends on the
test: a GPU test shows the GPU, a CPU test the CPU / RAM / clock, a disk or network test its jobs and results, a multi-node or
FULL test one row per node and the phase. The width decides how much (up to 90 / 91-160 / wider columns, like the dashboard).
Keys: ↑↓ select, Tab node, Enter log tail, x stop the test, +/- interval, p pause, q back.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import background, gpu as gpumod, schedule, ui
from .dashboard import INTERVALS, _bar, _nearest, _pct, _rate, _row, _spark, _temp, read_key
from .disk import parse_result_line as parse_disk_line
from .logparse import _STAGE_RE, _parse_sample_line
from .net import parse_result_line as parse_net_line
from .parsing import clean_text, format_duration

log = logging.getLogger(__name__)

TAIL_BYTES = 256 * 1024
ICONS = {"gpu": "🎮", "classic": "⚡", "stepped": "⚡", "spike": "⚡", "disk": "💾", "net": "📡", "selftest": "🧪", "?": "🧪"}
NAMES = {"gpu": "GPU", "classic": "CPU", "stepped": "CPU stepped", "spike": "CPU spike", "disk": "DISK", "net": "NET", "selftest": "FULL self-test"}
_EVENT_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]\s+(?:▶|🎮|🚀|💽|🌐|⚠️|❌|✅|🛑)")
_DISK_JOB_RE = re.compile(r"▶ Disk job (\d+)/(\d+)\s+(\S+)")
_NET_JOB_RE = re.compile(r"▶ Network job (\d+)/(\d+)\s+(\S+)")
_LIMIT_RE = re.compile(r"(?:Automatic stop at|GPU temperature limit):\s*(\d+)")
_PERF_RE = re.compile(r"gpu-burn performance:\s*(.+)$")


# ================================================================ reading a test log, incrementally =======================
class LogReader:
    """Follows one result log: the header once, then only the new bytes. Keeps the newest measurements and events."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.pos = 0
        self.header: dict = {}
        self.samples: deque = deque(maxlen=240)          # (clock s, Sample)
        self.events: deque = deque(maxlen=40)
        self.stage = ""
        self.job = ""
        self.job_no = (0, 0)
        self.disk_results: list = []
        self.net_results: list = []
        self.perf = ""
        self.ended = ""
        self.limits: list = []
        self._partial = ""
        self._first_test_clock: Optional[int] = None

    @property
    def last(self):
        return self.samples[-1][1] if self.samples else None

    @property
    def phase(self) -> str:
        if self.ended:
            return "ended"
        return "cooldown" if self.last is not None and self.last.phase == "cooldown" else "test" if self.samples else "preparing"

    def elapsed_test(self) -> Optional[int]:
        test = [c for c, s in self.samples if s.phase == "test"]
        if not test or self._first_test_clock is None:
            return None
        span = test[-1] - self._first_test_clock
        return span + 86400 if span < 0 else span

    def poll(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.pos:                                    # replaced / truncated
            self.__init__(self.path)
        try:
            with open(self.path, "rb") as fh:
                if self.pos == 0:
                    head = fh.read(8192).decode("utf-8", "replace").splitlines()
                    self._read_header(head)
                    self.pos = max(0, size - TAIL_BYTES) if size > TAIL_BYTES else 0
                fh.seek(self.pos)
                data = fh.read()
                self.pos += len(data)
        except OSError:
            return
        text = self._partial + data.decode("utf-8", "replace")
        lines = text.split("\n")
        self._partial = lines.pop()                            # an unfinished last line waits for the next poll
        for line in lines:
            self._line(line.strip())

    def _read_header(self, lines: list) -> None:
        for line in lines[:60]:
            key, _, value = line.partition(":")
            if key in ("Node", "Started", "Profile", "Test duration", "Notes", "Cooldown after test") and key not in self.header:
                self.header[key] = value.strip()
            found = _LIMIT_RE.search(line)
            if found:
                self.limits.append(int(found.group(1)))
            if line.startswith("=== HARDWARE"):
                break

    def _line(self, line: str) -> None:
        if not line:
            return
        parsed = _parse_sample_line(line[:4000])
        if parsed:
            clock, sample = parsed
            self.samples.append((clock, sample))
            if sample.phase == "test" and self._first_test_clock is None:
                self._first_test_clock = clock
            return
        stage = _STAGE_RE.search(line)
        if stage:
            self.stage = f"{stage.group(1)}/{stage.group(2)} · {stage.group(3)} %"
        m = _DISK_JOB_RE.search(line)
        if m:
            self.job, self.job_no = m.group(3), (int(m.group(1)), int(m.group(2)))
        m = _NET_JOB_RE.search(line)
        if m:
            self.job, self.job_no = m.group(3), (int(m.group(1)), int(m.group(2)))
        if line.startswith("Disk result:"):
            result = parse_disk_line(line)
            if result:
                self.disk_results.append(result)
        elif line.startswith("Net result:"):
            result = parse_net_line(line)
            if result:
                self.net_results.append(result)
        m = _PERF_RE.search(line)
        if m:
            self.perf = clean_text(m.group(1), 60)
        if "Test completed" in line or "Test interrupted" in line or "Test stopped" in line or "ended with an error" in line or "ended PREMATURELY" in line:
            self.ended = clean_text(line.split("]", 1)[-1].strip().lstrip("✅🛑❌⚠️ ").strip(), 90)
        if _EVENT_RE.match(line):
            self.events.append(clean_text(line, 200))


# ================================================================ the tests (registry + logs) =============================
@dataclass
class TestView:
    rec: dict
    readers: dict = field(default_factory=dict)         # node -> LogReader
    waiting: bool = False

    @property
    def id(self) -> str:
        return self.rec.get("run_id", "?")

    @property
    def profile(self) -> str:
        return self.rec.get("profile") or "classic"

    @property
    def nodes(self) -> list:
        return self.rec.get("nodes") or ([self.rec.get("node")] if self.rec.get("node") else [])

    @property
    def multi(self) -> bool:
        return self.profile == "selftest" or len(self.nodes) > 1 or self.rec.get("node") == "cluster"

    @property
    def title(self) -> str:
        return self.rec.get("title") or f"{NAMES.get(self.profile, self.profile)} · {', '.join(self.nodes)}"


class Watcher:
    """The data of the screen: the registry, the plans and a LogReader per test node."""

    def __init__(self) -> None:
        self.tests: list = []
        self.plans: list = []
        self._readers: dict = {}                         # path -> LogReader
        self.stamp = 0.0

    def _node_logs(self, rec: dict) -> dict:
        logs: dict = {}
        started = rec.get("started", 0) - 60
        nodes = rec.get("nodes") or []
        single = rec.get("log") and rec.get("kind") != "x" and not (len(nodes) > 1 or rec.get("node") == "cluster" or rec.get("profile") == "selftest")
        if single:
            logs[(nodes or [rec.get("node", "?")])[0]] = rec["log"]
            return logs
        folder = rec.get("log_dir") or (str(Path(rec["log"]).parent) if rec.get("log") else "")
        if not folder or not os.path.isdir(folder):
            return logs
        roots = [Path(folder)] + [p for p in Path(folder).glob("selftest-*") if p.is_dir()]
        for node in nodes:
            best = None
            for root in roots:
                try:
                    candidates = [p for p in root.glob(f"{node}-*.log") if p.stat().st_mtime >= started]
                except OSError:
                    continue
                for p in candidates:
                    if best is None or p.stat().st_mtime > best.stat().st_mtime:
                        best = p
            if best is not None:
                logs[node] = str(best)
        return logs

    def refresh(self) -> None:
        now = time.time()
        views = []
        for rec in background.list_running():
            if rec.get("scheduled_at") and rec["scheduled_at"] > now:
                continue
            tv = TestView(rec)
            for node, path in self._node_logs(rec).items():
                reader = self._readers.get(path)
                if reader is None:
                    reader = self._readers[path] = LogReader(path)
                reader.poll()
                tv.readers[node] = reader
            views.append(tv)
        self.tests = sorted(views, key=lambda t: t.rec.get("started", 0))
        try:
            self.plans = schedule.list_plans()
        except Exception:                                      # noqa: BLE001
            self.plans = []
        self.stamp = now


# ================================================================ drawing =================================================
def _kv(label: str, value: str, on: bool) -> str:
    return f"{ui.paint(label, ui.GREY, on)} {value}"


def _icon(tv: TestView) -> str:
    return ICONS.get(tv.profile, "🧪")


def progress_of(tv: TestView) -> tuple:
    """(fraction 0..1, label) of a test."""
    rec = tv.rec
    if tv.profile == "selftest" and rec.get("phases"):
        done = (rec.get("phase_no", 1) - 1) / rec["phases"]
        return done, f"phase {rec.get('phase_no', 1)}/{rec['phases']}"
    if tv.multi:
        ended = sum(1 for r in tv.readers.values() if r.ended)
        total = max(1, len(tv.nodes))
        left = [r for r in tv.readers.values() if not r.ended]
        part = 0.0
        for r in left:
            e, d = r.elapsed_test(), rec.get("duration") or 0
            if e is not None and d:
                part += min(1.0, e / max(1, d / max(1, len(tv.nodes))))
        return min(1.0, (ended + part / max(1, len(left) or 1) * (1 if left else 0)) / total), f"{ended}/{total} nodes done"
    reader = next(iter(tv.readers.values()), None)
    if reader is None:
        return 0.0, "starting"
    duration = rec.get("duration") or 0
    elapsed = reader.elapsed_test()
    if reader.ended:
        return 1.0, "finished"
    if reader.phase == "cooldown":
        return 1.0, "cooldown"
    if elapsed is None or not duration:
        return 0.0, "preparing the node"
    return min(1.0, elapsed / duration), f"{format_duration(max(0, duration - elapsed))} left"


def _list_rows(w: Watcher, sel: int, on: bool, width: int) -> list:
    rows = []
    bar_w = 10 if width < 100 else 16
    for i, tv in enumerate(w.tests):
        frac, label = progress_of(tv)
        mark = ui.paint("▸", ui.YELLOW, on) if i == sel else " "
        where = "cluster" if tv.multi else (tv.nodes[0] if tv.nodes else "?")
        phase = tv.rec.get("phase", "") if tv.profile == "selftest" else ""
        reader = next(iter(tv.readers.values()), None) if not tv.multi else None
        code = ui.CYAN if reader is not None and reader.phase == "cooldown" else ui.GREEN
        fg = ui.paint(" (fg)", ui.GREY, on) if tv.rec.get("kind") == "foreground" else ""
        rows.append(f"{mark} {_icon(tv)} {NAMES.get(tv.profile, tv.profile):<13} {where[:24]:<24} {ui.paint(tv.id, ui.GREY, on)}{fg}  "
                    f"{ui.paint('█' * round(bar_w * frac) + '░' * (bar_w - round(bar_w * frac)), code, on)} {100 * frac:>3.0f}%  {label}"
                    + (f"  {ui.paint(phase[:48], ui.GREY, on)}" if phase and width >= 100 else ""))
    for p in w.plans[:4]:
        rows.append(f"  🕒 {ui.paint('planned', ui.GREY, on)} {schedule.describe_when(p['start_at'])}  {p['title']}  {ui.paint(p['id'], ui.GREY, on)}")
    return rows or [ui.paint("no test is running or planned  (start one from the menu - the screen fills in at once)", ui.GREY, on)]


def _limit_hint(reader: LogReader, key: int = 0) -> str:
    return f"stop {reader.limits[key]}" if len(reader.limits) > key else ""


def _node_rows(reader: LogReader, tv: TestView, on: bool, width: int, node: str) -> list:
    """The measurements of one node log, by the profile of the test."""
    s = reader.last
    if s is None:
        return [ui.paint("waiting for the first measurement (the node is being prepared)…", ui.GREY, on)]
    rows = []
    wide = width >= 100
    bar = 12 if width >= 130 else 8
    temp_limit = reader.limits[0] if reader.limits else 85
    if tv.profile == "gpu":
        gpu_limit = reader.limits[-1] if len(reader.limits) > 1 else 80
        hot = s.gpu_temp is not None and s.gpu_temp >= gpu_limit - 5
        rows.append(f"🔥 GPU {_temp(s.gpu_temp, on, gpu_limit)}  {ui.paint(f'stop {gpu_limit}', ui.GREY, on)}  "
                    + (_spark([x.gpu_temp for _c, x in reader.samples if x.gpu_temp is not None], 24, gpu_limit, on,
                              ui.YELLOW if hot else ui.GREEN, 20) if wide else ""))
        rows.append(f"📈 {s.gpu_sm_mhz if s.gpu_sm_mhz is not None else '?'} MHz   ⚡ load {s.gpu_util_pct if s.gpu_util_pct is not None else '?'} %   "
                    f"🧠 VRAM {s.gpu_mem_mib if s.gpu_mem_mib is not None else '?'} MiB   🔌 {('%.0f W' % s.gpu_power_w) if s.gpu_power_w is not None else 'power N/A'}"
                    + (f"   🌀 fan {s.gpu_fan_pct} %" if s.gpu_fan_pct is not None else ""))
        throttle = gpumod.throttle_names(s.gpu_throttle, True)
        rows.append(f"🧊 throttle {ui.paint(','.join(throttle), ui.YELLOW, on) if throttle else ui.paint('none', ui.GREEN, on)}"
                    + (f"   🚀 {reader.perf}" if reader.perf else ""))
        rows.append(ui.paint(f"💻 host  CPU {_pct(s.cpu_pct).strip()} · RAM {_pct(s.mem_used_pct).strip()} · {s.cpu_temp if s.cpu_temp is not None else '?'} °C"
                             f" · {s.freq_mhz if s.freq_mhz is not None else '?'} MHz", ui.GREY, on))
        return rows
    # CPU based (classic / stepped / spike / quick, also with RAM or disk load) and the host view of disk / network tests
    rows.append(f"⚡ CPU {_bar(s.cpu_pct, bar, on)} {_pct(s.cpu_pct)}   🧠 RAM {_bar(s.mem_used_pct, bar, on, 80, 95)} {_pct(s.mem_used_pct)}"
                + (f" ({s.mem_used_mib} MiB)" if s.mem_used_mib is not None and wide else ""))
    rows.append(f"🔥 TEMP {_temp(s.cpu_temp, on, temp_limit)}  {ui.paint(f'stop {temp_limit}', ui.GREY, on)}   📈 {s.freq_mhz if s.freq_mhz is not None else '?'} MHz"
                + (f"   🔌 {s.power_w:.1f} W" if s.power_w is not None else "")
                + (f"   📡 ping {s.ping_ms:.1f} ms" if s.ping_ms is not None else ""))
    if wide:
        hist_w = max(8, min(30, (width - 28) // 3))
        rows.append(_kv("history", f"CPU {_spark([x.cpu_pct for _c, x in reader.samples if x.cpu_pct is not None], hist_w, 100, on, ui.GREEN)}  "
                                   f"temp {_spark([x.cpu_temp for _c, x in reader.samples if x.cpu_temp is not None], hist_w, temp_limit, on, ui.YELLOW, 25)}  "
                                   f"clock {_spark([x.freq_mhz for _c, x in reader.samples if x.freq_mhz is not None], hist_w, max([x.freq_mhz for _c, x in reader.samples if x.freq_mhz is not None] or [1]), on, ui.CYAN)}", on))
        freqs = [x.freq_mhz for _c, x in reader.samples if x.freq_mhz is not None]
        if len(freqs) > 6:
            top = max(freqs)
            drop = 100 * (top - freqs[-1]) / top if top else 0
            rows.append(_kv("clock", f"{freqs[-1]} MHz now · {top} MHz max seen · " + (ui.paint(f"down {drop:.0f} % (throttling?)", ui.YELLOW, on)
                                                                                   if drop >= 15 else "steady"), on))
    if reader.stage:
        rows.append(f"🎯 stage {reader.stage}")
    return rows


def _results_rows(reader: LogReader, tv: TestView, on: bool, limit: int) -> list:
    rows = []
    if tv.profile == "disk":
        if reader.job:
            rows.append(f"💽 fio job {reader.job_no[0]}/{reader.job_no[1]}: {reader.job}")
        rows += [f"   ✓ {r.line()}" for r in reader.disk_results[-limit:]]
    elif tv.profile == "net":
        if reader.job:
            rows.append(f"🌐 job {reader.job_no[0]}/{reader.job_no[1]}: {reader.job}")
        rows += [f"   ✓ {r.line()}" for r in reader.net_results[-limit:]]
    return rows


def _events_rows(reader: LogReader, on: bool, limit: int, width: int) -> list:
    return [ui.paint(e, ui.GREY, on) for e in list(reader.events)[-limit:]]


def _multi_table(tv: TestView, state: "UiState", on: bool, width: int) -> list:
    rows = [ui.paint(_row(["NODE", "STATE", "CPU", "TEMP", "RAM", "GPU", "CLOCK"], [26, 10, 11, 5, 11, 12, 8]), ui.GREY, on)]
    for i, node in enumerate(tv.nodes):
        reader = tv.readers.get(node)
        mark = ui.paint("▸", ui.YELLOW, on) if i == state.node else " "
        if reader is None:
            rows.append(f"{mark} {_row([node, ui.paint('waiting', ui.GREY, on), '', '', '', '', ''], [26, 10, 11, 5, 11, 12, 8])}")
            continue
        s = reader.last
        state_txt = (ui.paint("done", ui.GREEN, on) if reader.ended and "completed" in reader.ended.lower() else
                     ui.paint("stopped", ui.RED, on) if reader.ended else
                     ui.paint(reader.phase, ui.CYAN if reader.phase == "cooldown" else ui.YELLOW, on))
        gpu = (f"{_temp(s.gpu_temp, on, 85)} {s.gpu_util_pct}%" if s is not None and s.gpu_temp is not None else "–")
        cells = [node, state_txt, f"{_bar(s.cpu_pct, 5, on)} {_pct(s.cpu_pct)}" if s else "", _temp(s.cpu_temp, on) if s else "",
                 f"{_bar(s.mem_used_pct, 5, on, 80, 95)} {_pct(s.mem_used_pct)}" if s else "", gpu,
                 f"{s.freq_mhz}MHz" if s is not None and s.freq_mhz is not None else ""]
        rows.append(f"{mark} {_row(cells, [26, 10, 11, 5, 11, 12, 8])}")
    return rows


@dataclass
class UiState:
    selected: int = 0
    node: int = 0
    interval: float = 1
    paused: bool = False
    log_view: bool = False
    scroll: int = 0                           # first visible body line of the narrow (card) layout
    confirm_stop: str = ""                    # run id waiting for y / n
    message: str = ""


def tail_rows(path: str, count: int, on: bool) -> list:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 64 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return [ui.paint("the log cannot be read", ui.RED, on)]
    return [clean_text(x, 220) for x in lines[-count:]]


COMPACT_BELOW = 60                            # narrower windows get cards stacked downwards instead of squeezed rows


def _wrapped(text: str, room: int, indent: str = "  ") -> list:
    return ui.wrap(text, max(10, room), indent=indent, hang=indent) or [""]


def _card_node(reader: LogReader, tv: TestView, on: bool, room: int) -> list:
    """One value per line (the narrow layout): nothing is cut, the card just gets taller."""
    s = reader.last
    if s is None:
        return [ui.paint("waiting for the first measurement…", ui.GREY, on)]
    out: list = []

    def add(label: str, value: str) -> None:
        out.extend(_wrapped(f"{label:<12}{value}", room, indent="  ")[:1] + _wrapped(value, room, indent=" " * 14)[1:]
                   if ui.visible_len(f"{label:<12}{value}") > room else [f"  {label:<12}{value}"])

    temp_limit = reader.limits[0] if reader.limits else 85
    if tv.profile == "gpu":
        gpu_limit = reader.limits[-1] if len(reader.limits) > 1 else 80
        add("🔥 GPU", f"{_temp(s.gpu_temp, on, gpu_limit)}  (stop {gpu_limit})")
        add("📈 clock", f"{s.gpu_sm_mhz if s.gpu_sm_mhz is not None else '?'} MHz")
        add("⚡ load", f"{s.gpu_util_pct if s.gpu_util_pct is not None else '?'} %")
        add("🧠 VRAM", f"{s.gpu_mem_mib if s.gpu_mem_mib is not None else '?'} MiB")
        add("🔌 power", f"{s.gpu_power_w:.0f} W" if s.gpu_power_w is not None else "N/A")
        if s.gpu_fan_pct is not None:
            add("🌀 fan", f"{s.gpu_fan_pct} %")
        throttle = gpumod.throttle_names(s.gpu_throttle, True)
        add("🧊 throttle", ",".join(throttle) if throttle else "none")
        if reader.perf:
            add("🚀 perf", reader.perf)
        add("💻 host", f"CPU {_pct(s.cpu_pct).strip()} · RAM {_pct(s.mem_used_pct).strip()}")
        return out
    add("⚡ CPU", f"{_bar(s.cpu_pct, 6, on)} {_pct(s.cpu_pct)}")
    add("🧠 RAM", f"{_bar(s.mem_used_pct, 6, on, 80, 95)} {_pct(s.mem_used_pct)}")
    add("🔥 temp", f"{_temp(s.cpu_temp, on, temp_limit)}  (stop {temp_limit})")
    add("📈 clock", f"{s.freq_mhz if s.freq_mhz is not None else '?'} MHz")
    if s.power_w is not None:
        add("🔌 power", f"{s.power_w:.1f} W")
    if s.ping_ms is not None:
        add("📡 ping", f"{s.ping_ms:.1f} ms")
    if reader.stage:
        add("🎯 stage", reader.stage)
    if tv.profile in ("disk", "net") and reader.job:
        add("🧪 job", f"{reader.job_no[0]}/{reader.job_no[1]} {reader.job}")
    for r in (reader.disk_results if tv.profile == "disk" else reader.net_results if tv.profile == "net" else [])[-3:]:
        out.extend(_wrapped("✓ " + r.line(), room, indent="  "))
    return out


def render_compact(w: Watcher, state: UiState, columns: int, lines: int, on: bool) -> list:
    """The narrow screen: every test is a card, the selected one is open and the rest of the window scrolls downwards."""
    width = max(24, columns)
    room = width - 4
    n = len(w.tests)
    state.selected = max(0, min(state.selected, n - 1)) if n else 0
    body: list = []
    if state.message:
        body += _wrapped(ui.paint(state.message, ui.YELLOW, on), room, "")
    if state.confirm_stop:
        body += _wrapped(ui.paint(f"Stop test {state.confirm_stop}? y = yes, other = no", ui.RED, on), room, "")
    if not n:
        body += _wrapped("No test is running or planned. Start one from the menu.", room, "")
    rule = ui.paint("─" * room, ui.GREY, on)
    chrome = 5                                              # top, title, 2 rules, bottom (+ the key row)
    height = max(4, lines - chrome - 1)
    tall = ui.height_mode(lines) == "tall"

    def short(tv: TestView) -> list:
        frac, _label = progress_of(tv)
        where = "cluster" if tv.multi else (tv.nodes[0] if tv.nodes else "?")
        return [f"  {_icon(tv)} {NAMES.get(tv.profile, tv.profile)[:12]} {100 * frac:>3.0f}% {where}"[:room * 3]]

    def card(tv: TestView, all_nodes: bool, node_idx: int) -> list:
        frac, label = progress_of(tv)
        where = "cluster" if tv.multi else (tv.nodes[0] if tv.nodes else "?")
        out = [rule, ui.paint(f"▸ {_icon(tv)} {NAMES.get(tv.profile, tv.profile)}  {tv.id}", ui.BOLD, on)]
        out += _wrapped(where, room)
        bar = max(6, min(20, room - 12))
        out.append(f"  {ui.paint('█' * round(bar * frac) + '░' * (bar - round(bar * frac)), ui.GREEN, on)} {100 * frac:.0f} %")
        out += _wrapped(label, room)
        if tv.profile == "selftest":
            out += _wrapped(clean_text(tv.rec.get("phase", "starting"), 100), room)
        if tv.multi:
            for j, node in enumerate(tv.nodes):
                reader = tv.readers.get(node)
                if all_nodes or j == min(node_idx, len(tv.nodes) - 1):
                    out += [rule, ui.paint(f"▸ {node}", ui.BOLD, on)]
                    out += _card_node(reader, tv, on, room) if reader else [ui.paint("  waiting", ui.GREY, on)]
                else:
                    s = reader.last if reader else None
                    out += _wrapped(f"{node}  " + (f"{_pct(s.cpu_pct).strip()} {s.cpu_temp if s.cpu_temp is not None else '?'}°" if s else "waiting"), room)
        else:
            reader = next(iter(tv.readers.values()), None)
            out += _card_node(reader, tv, on, room) if reader else [ui.paint("  starting: no log yet", ui.GREY, on)]
            if reader is not None and reader.ended:
                out += _wrapped("■ " + reader.ended, room)
        out.append(rule)
        return out

    parts = [short(tv) for tv in w.tests]
    if n:
        parts[state.selected] = card(w.tests[state.selected], False, state.node)
    used = len(body) + sum(len(x) for x in parts) + 3 * len(w.plans[:3])
    if tall:                                                # a tall window: open every card that fits, then every node
        for i, tv in enumerate(w.tests):
            if i == state.selected:
                continue
            opened = card(tv, False, 0)
            if used - len(parts[i]) + len(opened) <= height:
                used += len(opened) - len(parts[i])
                parts[i] = opened
        for i, tv in enumerate(w.tests):
            if tv.multi and len(parts[i]) > 1:
                opened = card(tv, True, state.node if i == state.selected else 0)
                if used - len(parts[i]) + len(opened) <= height:
                    used += len(opened) - len(parts[i])
                    parts[i] = opened
    for chunk in parts:
        body += chunk[1:] if chunk and body and chunk[0] == rule and body[-1] == rule else chunk
    if tall and n and used < height - 4:                    # still room: the history of the selected test and its events
        sel = w.tests[state.selected]
        reader = next(iter(sel.readers.values()), None) if not sel.multi else sel.readers.get(sel.nodes[min(state.node, len(sel.nodes) - 1)] if sel.nodes else "")
        if reader is not None:
            temps = [x.cpu_temp for _c, x in reader.samples if x.cpu_temp is not None]
            cpu = [x.cpu_pct for _c, x in reader.samples if x.cpu_pct is not None]
            tail = [ui.paint("▸ history", ui.BOLD, on)]
            if cpu:
                tail.append(f"  CPU  {_spark(cpu, room - 8, 100, on, ui.GREEN)}")
            if temps:
                tail.append(f"  temp {_spark(temps, room - 8, reader.limits[0] if reader.limits else 85, on, ui.YELLOW, 25)}")
            if tv_gpu := [x.gpu_temp for _c, x in reader.samples if x.gpu_temp is not None]:
                tail.append(f"  GPU  {_spark(tv_gpu, room - 8, reader.limits[-1] if reader.limits else 80, on, ui.YELLOW, 25)}")
            space = height - used - len(tail) - 1
            if space >= 2 and reader.events:
                tail.append(ui.paint("▸ events", ui.BOLD, on))
                for e in list(reader.events)[-min(space - 1, 30):]:
                    tail += _wrapped(ui.paint(e, ui.GREY, on), room, "")[:2]
            body += tail
    for p in w.plans[:3]:
        body += _wrapped(f"🕒 {schedule.describe_when(p['start_at'])} {p['title']}", room)
    state.scroll = max(0, min(state.scroll, max(0, len(body) - height)))
    view = body[state.scroll:state.scroll + height]
    if state.scroll > 0 and view:
        view[0] = ui.paint("↑ more (k)", ui.GREY, on)
    if state.scroll + height < len(body) and view:
        view[-1] = ui.paint("↓ more (j)", ui.GREY, on)
    keys = ui.paint("↑↓ test  j/k scroll  x stop  e screen  q" if room >= 40 else "↑↓ j/k x e q", ui.GREY, on)
    return ui.box(f"🔭 STATUS · {n}" + (" · PAUSED" if state.paused else ""), [view or [""], [keys]], on, width)


def _detail_section(sel: TestView, state: UiState, tiers: int, on: bool, width: int) -> list:
    """The detail block of one test (title, the table of nodes or the measurements)."""
    detail = [ui.paint(f"▸ {sel.title}", ui.BOLD, on)]
    if sel.multi:
        detail += _multi_table(sel, state, on, width)
        if sel.profile == "selftest":
            detail.append(_kv("phase", clean_text(sel.rec.get("phase", "starting"), 100), on))
        node = sel.nodes[min(state.node, len(sel.nodes) - 1)] if sel.nodes else ""
        reader = sel.readers.get(node)
        if reader is not None and tiers > 1:
            detail += ["", ui.paint(f"▸ {node}", ui.BOLD, on), *_node_rows(reader, sel, on, width, node)]
    else:
        reader = next(iter(sel.readers.values()), None)
        if reader is not None:
            detail += _node_rows(reader, sel, on, width, "")
            detail += _results_rows(reader, sel, on, 6 if tiers > 1 else 3)
            if reader.ended:
                detail.append(ui.paint(f"■ {reader.ended}", ui.GREEN if "completed" in reader.ended.lower() else ui.YELLOW, on))
        else:
            detail.append(ui.paint("starting: the log is not there yet", ui.GREY, on))
    return detail


def render(w: Watcher, state: UiState, columns: int, lines: int, on: bool) -> list:
    """The whole screen as lines (pure: easy to test for every width and height)."""
    if columns < COMPACT_BELOW:
        return render_compact(w, state, columns, lines, on)
    width = max(30, columns)
    tiers = 1 if columns <= 90 else 2 if columns <= 160 else 3
    n = len(w.tests)
    state.selected = max(0, min(state.selected, n - 1)) if n else 0
    title = (f"🔭 STATUS · {n} test{'s' if n != 1 else ''} running" + (f" · {len(w.plans)} planned" if w.plans else "")
             + (f" · {ui.paint('PAUSED', ui.YELLOW, on)}" if state.paused else "") + f" · {state.interval:g} s · {time.strftime('%H:%M:%S')}")
    keys = ui.paint("↑↓ test  Tab node  Enter log  x stop  e screen  +/- interval  p pause  q back" if tiers > 1 else "↑↓  Tab  Enter log  x stop  e  +/-  q", ui.GREY, on)
    head = [ui.paint(state.message, ui.YELLOW, on)] if state.message else []
    if state.confirm_stop:
        head = [ui.paint(f"Stop test {state.confirm_stop}? (y = stop it, anything else = no)", ui.RED, on)]
    sel = w.tests[state.selected] if n else None
    if state.log_view and sel is not None:
        node = sel.nodes[min(state.node, len(sel.nodes) - 1)] if sel.nodes else ""
        reader = sel.readers.get(node)
        rows = tail_rows(reader.path, max(8, lines - 8), on) if reader else [ui.paint("no log yet", ui.GREY, on)]
        return ui.box(title + " · log", [[ui.paint(f"▸ {sel.title} · {node}", ui.BOLD, on)], rows, [ui.paint("Enter/Esc back   q back", ui.GREY, on)]], on, width)
    sections = [*([head] if head else []), _list_rows(w, state.selected, on, width)]
    ev_at = None
    if sel is not None:
        sections.append(_detail_section(sel, state, tiers, on, width))
        reader = next(iter(sel.readers.values()), None) if not sel.multi else sel.readers.get(sel.nodes[min(state.node, len(sel.nodes) - 1)] if sel.nodes else "")
        if reader is not None and tiers >= 2 and reader.events:
            room = max(3, lines - 14 - sum(len(s) for s in sections)) if tiers == 3 else 4
            ev_at = len(sections)
            sections.append([ui.paint("▸ events", ui.BOLD, on), *_events_rows(reader, on, min(room, 12), width)])
        if tiers == 3 and not sel.multi and reader is not None and reader.header:
            h = reader.header
            sections.append([_kv("started", h.get("Started", "?"), on) + "   " + _kv("duration", h.get("Test duration", "?"), on) + "   "
                             + _kv("cooldown", h.get("Cooldown after test", "?"), on) + "   " + _kv("log", reader.path, on)])
    if sel is not None and ui.height_mode(lines) == "tall":      # a tall window: open everything that fits, instead of scrolling
        budget = lines - 1 - len(ui.box(title, [*sections, [keys]], on, width))
        for other in w.tests:
            if other is sel:
                continue
            extra = _detail_section(other, UiState(node=0), tiers, on, width)
            if budget >= len(extra) + 1:
                sections.append(extra)
                budget -= len(extra) + 1
        if ev_at is not None and budget >= 3:
            at = ev_at
            reader = next(iter(sel.readers.values()), None) if not sel.multi else sel.readers.get(sel.nodes[min(state.node, len(sel.nodes) - 1)] if sel.nodes else "")
            if reader is not None:
                sections[at] = [ui.paint("▸ events", ui.BOLD, on), *_events_rows(reader, on, len(sections[at]) - 1 + budget, width)]
    sections.append([keys])
    return ui.box(title, sections, on, width)


# ================================================================ the screen (keys, redraw) ===============================
class Screen:
    def __init__(self, stream=None, keys: Optional[Callable] = None) -> None:
        self.w = Watcher()
        self.stream = stream or sys.stdout
        self.state = UiState()
        self.canvas = ui.Canvas(self.stream)
        self._keys = keys

    def _size(self) -> tuple:
        try:
            s = os.get_terminal_size(self.stream.fileno())
            return s.columns, s.lines
        except (OSError, ValueError, AttributeError):
            return 100, 40

    def draw(self) -> None:
        columns, lines = self._size()
        frame = render(self.w, self.state, columns, lines, ui.color_enabled())[:max(10, lines - 1)]
        self._frame, self._size_seen = frame, (columns, lines)
        self.canvas.draw(frame)

    def handle(self, key: str) -> bool:
        s, tests = self.state, self.w.tests
        if s.confirm_stop:
            target, s.confirm_stop = s.confirm_stop, ""
            if key in ("y", "Y"):
                ok, message = background.stop(target, wait_s=3.0)
                s.message = ("✅ " if ok else "⚠️ ") + message
            else:
                s.message = "not stopped"
            return True
        s.message = ""
        if key in ("q", "Q", "esc"):
            if s.log_view:
                s.log_view = False
                return True
            return False
        if key == "up":
            s.selected, s.node, s.scroll = max(0, s.selected - 1), 0, 0
        elif key == "down":
            s.selected, s.node, s.scroll = min(max(0, len(tests) - 1), s.selected + 1), 0, 0
        elif key in ("j", "J", "pgdn"):
            s.scroll += 8 if key == "pgdn" else 1
        elif key in ("k", "K", "pgup"):
            s.scroll = max(0, s.scroll - (8 if key == "pgup" else 1))
        elif key == "home":
            s.scroll = 0
        elif key == "tab" and tests:
            s.node = (s.node + 1) % max(1, len(tests[min(s.selected, len(tests) - 1)].nodes))
        elif key == "enter":
            s.log_view = not s.log_view
        elif key in ("+", "="):
            s.interval = INTERVALS[min(len(INTERVALS) - 1, _nearest(s.interval) + 1)]
        elif key in ("-", "_"):
            s.interval = INTERVALS[max(0, _nearest(s.interval) - 1)]
        elif key in ("p", "P"):
            s.paused = not s.paused
        elif key in ("e", "E") and getattr(self, "_frame", None):          # the screen exactly as drawn -> scr/
            written = ui.save_screen(self._frame, "status", *self._size_seen)
            s.message = f"📸 screen saved: {written[0].parent.name}/{written[0].name}" if written else "❌ the screen could not be saved"
        elif key in ("x", "X") and tests:
            s.confirm_stop = tests[min(s.selected, len(tests) - 1)].id
        return True

    def run(self) -> int:
        old = None
        if self._keys is None:
            import termios
            import tty
            try:
                old = termios.tcgetattr(0)
                tty.setcbreak(0)
            except (termios.error, OSError, ValueError):
                ui.emit("❌ The live status needs a terminal.")
                return 1
        try:
            self.stream.write("\x1b[?25l")
            while True:
                if not self.state.paused:
                    self.w.refresh()
                self.draw()
                wait = min(self.state.interval, 1.0) if self.state.paused else self.state.interval
                key = self._keys(wait) if self._keys else read_key(0, wait)
                if key is None:
                    continue
                if not self.handle(key):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stream.write("\x1b[?25h")
            self.canvas.reset()
            self.stream.flush()
            if old is not None:
                import termios
                termios.tcsetattr(0, termios.TCSADRAIN, old)
        return 0


def run() -> int:
    """`--status --live` / menu W."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        ui.emit(background.format_running(background.list_running()))
        return 0
    return Screen().run()
