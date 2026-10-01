"""What the user can choose from: the nodes that have result logs and the newest logs themselves (menu 7 DATA).

`--compare`, `--set-baseline` and `--export-log` take log names or node names, but nobody remembers them - the menu shows
a coarse list first (nodes with their newest test and baseline, then the numbered logs) and accepts the numbers from it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import ui
from .baseline import baseline_dir, load_baseline
from .logparse import read_log
from .paths import result_dirs
from .summary import run_stats

log = logging.getLogger(__name__)

SKIP = ("net-matrix", "cluster-", "full-selftest")          # logs that are not one node's test (matrix, series summary, FULL report)
_STAMP = re.compile(r"(\d{4}-\d\d-\d\d)_(\d\d)-(\d\d)-(\d\d)")
_CACHE: dict = {}                                            # (path, mtime) -> LogEntry: a log is parsed once


@dataclass
class LogEntry:
    path: str
    node: str
    when: str                 # "2026-10-01 10:15"
    profile: str
    duration: str
    max_temp: Optional[int]
    state: str                # done / stopped / ?
    baseline: bool = False

    @property
    def name(self) -> str:
        return Path(self.path).name


def _entry(path: Path) -> Optional[LogEntry]:
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _CACHE:
        return _CACHE[key]
    try:
        run = read_log(str(path))
        text = path.read_text(encoding="utf-8", errors="replace")
        top = run_stats(run.samples, run.baseline_temp).temp_max if run.samples else None
    except Exception:                                        # noqa: BLE001 - a bad log must not stop the menu
        return None
    stamp = _STAMP.search(path.name)
    when = f"{stamp.group(1)} {stamp.group(2)}:{stamp.group(3)}" if stamp else (run.started or "?")[:16]
    state = "done" if "Test completed" in text else "stopped" if ("Test stopped" in text or "Test interrupted" in text) else "?"
    entry = _CACHE[key] = LogEntry(str(path), run.node, when, run.profile, run.duration_text or "?", top, state)
    return entry


def scan(root, limit: int = 40) -> list:
    """The newest result logs of single nodes, newest first (`limit` of them)."""
    files = []
    seen = set()
    for folder in result_dirs(root):
        if folder.is_dir():
            for p in folder.glob("*.log"):
                if not p.name.startswith(SKIP) and p not in seen:
                    seen.add(p)
                    stamp = _STAMP.search(p.name)
                    files.append((stamp.group(0) if stamp else "", p.stat().st_mtime, p))
    files.sort(key=lambda t: (t[0], t[1]), reverse=True)
    out = []
    for _s, _m, path in files:
        entry = _entry(path)
        if entry is not None:
            out.append(entry)
        if len(out) >= limit:
            break
    for e in out:
        e.baseline = load_baseline(root, e.node) is not None
    return out


def nodes_of(entries: list) -> list:
    """[(node, how many of the listed logs, newest entry)] ordered by the newest log."""
    seen: dict = {}
    for e in entries:
        count, newest = seen.get(e.node, (0, e))
        seen[e.node] = (count + 1, newest)
    return [(n, c, e) for n, (c, e) in seen.items()]


def _temp(e: LogEntry, on: bool) -> str:
    return "  –" if e.max_temp is None else ui.paint(f"{e.max_temp:>3}°", ui.temp_code(e.max_temp), on)


def lines(entries: list, root, on: bool, width: int, rows: Optional[int] = None) -> list:
    """The catalog as framed lines: NODES (what each has and its baseline), then the numbered LOGS."""
    room = width - 4
    wide = room >= 72
    shown_logs = 40 if ui.height_mode(rows) == "tall" else 12
    if not entries:
        return ui.box("📚 NODES AND LOGS", [[ui.paint(f"No result logs yet in {root}.", ui.GREY, on),
                                              ui.paint("Run a test first, or type a path to a log.", ui.GREY, on)]], on, width)
    node_w = min(26, max(len(n) for n, _c, _e in nodes_of(entries)))
    nodes = [ui.paint("NODE".ljust(node_w) + "  LOGS  " + "NEWEST TEST".ljust(24) + ("  BASE  MAX" if wide else ""), ui.GREY, on)]
    for node, count, e in nodes_of(entries):
        row = f"{ui.fit(node, node_w).ljust(node_w)}  {count:>4}  {e.when} {e.profile[:7]:<7}"
        nodes.append(row + (f"  {'★' if e.baseline else ' ':<4}  {_temp(e, on)}" if wide else (" ★" if e.baseline else "")))
    logs = [ui.paint(f"{'#':>2}  {'WHEN':<16}  " + ("NODE".ljust(node_w) + "  " if wide else "") + f"{'TEST':<7} MAX  RESULT", ui.GREY, on)]
    for i, e in enumerate(entries[:shown_logs], 1):
        node = ui.fit(e.node, node_w).ljust(node_w) + "  " if wide else ""
        state = ui.paint(e.state.ljust(7), ui.GREEN if e.state == "done" else ui.YELLOW, on)
        logs.append(f"{ui.paint(str(i).rjust(2), ui.BOLD + ';' + ui.YELLOW, on)}  {e.when}  {node}{e.profile[:7]:<7} {_temp(e, on)}  {state}"
                    + ("" if wide else f" {ui.fit(e.node, max(8, room - 42))}"))
    if len(entries) > shown_logs:
        logs.append(ui.paint(f"… and {len(entries) - shown_logs} older (type a file name or a node)", ui.GREY, on))
    return ui.box("📚 NODES AND LOGS · ★ = has a baseline", [nodes, logs], on, width)


def looks_like_log(word: str) -> bool:
    """A log file (a path, `x.log`, or the name `<node>-120s-2026-10-01_10-00-00` with or without .log), not a node name."""
    return word.endswith(".log") or "/" in word or bool(re.search(r"-\d+s-\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", word))


def resolve(words: list, entries: list) -> list:
    """Numbers from the list become log paths; anything else (a node, a file name) stays as typed."""
    out = []
    for w in words:
        out.append(entries[int(w) - 1].path if w.isdigit() and 1 <= int(w) <= len(entries) else w)
    return out


def order_pair(words: list, entries: list) -> list:
    """Two numbers from the list are put older-first (the list is newest-first, people type them either way)."""
    if len(words) == 2 and all(w.isdigit() and 1 <= int(w) <= len(entries) for w in words):
        a, b = (entries[int(w) - 1] for w in words)
        return [x.path for x in sorted((a, b), key=lambda e: (e.when, e.path))]
    return resolve(words, entries)


def show(root, on: Optional[bool] = None, width: Optional[int] = None, rows: Optional[int] = None,
         printer: Callable = print) -> list:
    """Prints the catalog and returns the entries (so that the numbers can be resolved)."""
    on = ui.color_enabled() if on is None else on
    entries = scan(root)
    printer("\n" + "\n".join(lines(entries, root, on, width or ui.panel_width(), ui.term_rows() if rows is None else rows)))
    return entries


# ---------------------------------------------------------------- running tests, cluster nodes, plans (the other menu questions)

def running_entries() -> list:
    """Tests that run or wait for their start (the registry of .logs/running), oldest first."""
    from . import background
    try:
        return sorted(background.list_running(), key=lambda r: r.get("started", 0))
    except Exception:                                        # noqa: BLE001
        return []


def running_lines(recs: list, on: bool, width: int) -> list:
    import time
    now = time.time()
    if not recs:
        return ui.box("🛑 RUNNING TESTS", [[ui.paint("No test is running or waiting.", ui.GREY, on)]], on, width)
    rows = [ui.paint(f"{'#':>2}  {'ID':<8}{'WHAT':<22}{'LEFT':>8}", ui.GREY, on)]
    for i, r in enumerate(recs, 1):
        waiting = r.get("scheduled_at") and r["scheduled_at"] > now
        what = ui.fit(r.get("title") or f"{r.get('profile', 'test')} · {r.get('node', '?')}", 22)
        left = "🕒 waits" if waiting else ui.clock(max(0, r.get("started", now) + (r.get("duration") or 0) - now)) if r.get("duration") else "?"
        fg = " (fg)" if r.get("kind") == "foreground" else ""
        rows.append(f"{ui.paint(str(i).rjust(2), ui.BOLD + ';' + ui.YELLOW, on)}  {str(r.get('run_id', '?')):<8}{what:<22}{left:>8}{fg}  PID {r.get('pid', '?')}")
    return ui.box("🛑 RUNNING TESTS · pick one to stop", [rows], on, width)


def nodes_lines(nodes: list, temps: Optional[dict], on: bool, width: int) -> list:
    """nodes = [(name, is_master, ready)]: numbered, with the role, the state and the hottest temperature of the last test."""
    if not nodes:
        return []
    temps = temps or {}
    rows = [ui.paint(f"{'#':>2}  {'NODE':<26}{'ROLE':<8}STATE", ui.GREY, on)]
    for i, (name, master, ready) in enumerate(nodes, 1):
        temp = temps.get(name)
        tail = f"  🔥 {ui.paint(f'{temp} °C', ui.temp_code(temp), on)}" if temp is not None else ""
        rows.append(f"{ui.paint(str(i).rjust(2), ui.BOLD + ';' + ui.YELLOW, on)}  {ui.fit(name, 26).ljust(26)}{'master' if master else 'worker':<8}"
                    + (ui.paint("Ready", ui.GREEN, on) if ready else ui.paint("NOT Ready", ui.RED, on)) + tail)
    return ui.box("🖥 NODES · pick a number or type a name", [rows], on, width)


def pick(word: str, items: list) -> str:
    """A number from a printed list becomes the item (a string); anything else stays as typed."""
    word = word.strip()
    return items[int(word) - 1] if word.isdigit() and 1 <= int(word) <= len(items) else word
