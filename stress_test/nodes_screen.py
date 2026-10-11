"""NODES screen (menu 8 ▸ list nodes, `--nodes-view`): one node at a time with everything the dashboard knows about it.

A bar with the node names on top (the selected one highlighted), the rest of the window is the information of that node:
hardware, Kubernetes, history graphs, pods, problems. Tab / arrows switch the node, PgUp / PgDn scroll when the window is low.
The data come from the dashboard's collector (read-only, probe pods only while the screen is open).

Layout by the width of the window: up to 119 columns one column, 120 - 229 two columns, 230 and more three columns.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from . import ui
from . import dashboard as d
from .dashboard import ClusterView, NodeView
from .kube import Kubectl

TWO_COLUMNS, THREE_COLUMNS = 120, 230


@dataclass
class NodesState:
    window_pick: list = field(default_factory=list)   # the kinds offered after key w (see Dashboard)
    name: str = ""                 # the selected node (by name: it stays selected when the list changes)
    scroll: int = 0
    interval: float = 5
    paused: bool = False


def node_index(view: ClusterView, state: NodesState) -> int:
    for i, n in enumerate(view.nodes):
        if n.name == state.name:
            return i
    return 0


def _chip(n: NodeView, selected: bool, on: bool) -> str:
    dot = ui.paint("●", ui.GREEN if n.ready else ui.RED, on)
    role = "M " if n.master else ""
    text = f" {dot} {role}{n.name} "
    return ui.paint(f"▶{text}◀", ui.BOLD, on) if selected else f" {text} "


def node_bar(view: ClusterView, index: int, room: int, on: bool) -> list:
    """The node names in one row (a window around the selected one when they do not all fit)."""
    nodes = view.nodes
    chips = [_chip(n, i == index, on) for i, n in enumerate(nodes)]
    if sum(ui.visible_len(c) for c in chips) <= room:
        return ["".join(chips)]
    left, right = index, index
    used = ui.visible_len(chips[index])
    while True:                                              # grow the window to both sides while it fits
        grew = False
        if left > 0 and used + ui.visible_len(chips[left - 1]) <= room - 8:
            left -= 1
            used += ui.visible_len(chips[left])
            grew = True
        if right < len(chips) - 1 and used + ui.visible_len(chips[right + 1]) <= room - 8:
            right += 1
            used += ui.visible_len(chips[right])
            grew = True
        if not grew:
            break
    return [ui.paint("◀ " if left else "  ", ui.GREY, on) + "".join(chips[left:right + 1]) + ui.paint(f" ▶ {index + 1}/{len(nodes)}" if right < len(chips) - 1
                                                                                                    else f"   {index + 1}/{len(nodes)}", ui.GREY, on)]


def history_rows(n: NodeView, width: int, on: bool) -> list:
    """Sparklines of what the dashboard has collected since it was opened."""
    rows = [d._title(f"{n.name} · history", on)]
    spark = max(8, min(60, width - 24))
    series = [("CPU", n.h_cpu, 100.0, 0.0, ui.GREEN, lambda v: f"{v:.0f}%"),
              ("temp", n.h_temp, 100.0, 20.0, ui.YELLOW, lambda v: f"{v:.0f}°C"),
              ("RAM", n.h_mem, 100.0, 0.0, ui.CYAN if hasattr(ui, "CYAN") else ui.GREEN, lambda v: f"{v:.0f}%"),
              ("clock", n.h_freq, None, 0.0, ui.GREY, lambda v: f"{v:.0f} MHz"),
              ("power", n.h_pwr, None, 0.0, ui.YELLOW, lambda v: f"{v:.0f} W"),
              ("net", n.h_net, None, 0.0, ui.GREEN, lambda v: d._rate(v) + "/s"),
              ("GPU", n.h_gpu, 100.0, 0.0, ui.GREEN, lambda v: f"{v:.0f}°C")]
    for label, values, top, floor, code, fmt in series:
        data = [v for v in values if v is not None]
        if not data:
            continue
        rows.append(f"{ui.paint(f'{label:<5}', ui.GREY, on)} {d._spark(values, spark, top or max(data) or 1.0, on, code, floor)} {fmt(data[-1])}"
                    f"  {ui.paint('max ' + fmt(max(data)), ui.GREY, on)}")
    if len(rows) == 1:
        rows.append(ui.paint("collecting the first samples…", ui.GREY, on))
    return rows


def problem_rows(view: ClusterView, n: NodeView, on: bool) -> list:
    rows = [d._title(f"{n.name} · problems", on)]
    mine = [p for p in view.problems if p[0] == n.name]
    flags = d._flags(n, on)
    if not n.ready:
        rows.append(ui.paint("✗ the node is NotReady - only the data from the Kubernetes API are shown", ui.RED, on))
    elif not n.probe_ok:
        rows.append(ui.paint("· the probe pod has not answered yet - hardware data are missing", ui.GREY, on))
    if n.unschedulable:
        rows.append(ui.paint("· cordoned: nothing new is scheduled here; the probe pod was placed directly and keeps working", ui.GREY, on))
    if flags.strip():
        rows.append(d._kv("state", flags, on))
    for _node, tag, reason, restarts in mine[:6]:
        rows.append(f"{ui.paint('✗', ui.RED, on)} {tag[:40]} {ui.paint(reason[:20], ui.YELLOW, on)} ↻{restarts}")
    if not mine and n.ready:
        rows.append(ui.paint("✓ every pod on the node is running", ui.GREEN, on))
    return rows


def _wrapped(block: list, width: int) -> list:
    return [part for row in block for part in (ui.wrap(row, width, "", "  ") if ui.visible_len(row) > width else [row])]


def node_body(view: ClusterView, n: NodeView, room: int, on: bool) -> list:
    """The information of one node, laid out in 1 - 3 columns by the width."""
    ncols = 3 if room >= THREE_COLUMNS - 4 else 2 if room >= TWO_COLUMNS - 4 else 1
    col = (room - 3 * (ncols - 1)) // ncols
    hw, k8s = d.block_hw(n, on), d.block_k8s(n, on)
    hist, prob = history_rows(n, col, on), problem_rows(view, n, on)
    pods = d.block_pods(n, on, 40)
    if ncols == 1:
        return _wrapped([*prob, "", *hw, "", *k8s, "", *hist, "", *pods], room)
    if ncols == 2:
        cols = [[*prob, "", *hw, "", *hist], [*k8s, "", *pods]]
    else:
        cols = [[*prob, "", *hw], [*k8s, "", *hist], pods]
    return d.join_n([_wrapped(c, col) for c in cols], [col] * ncols, on)


def render_nodes(view: ClusterView, state: NodesState, columns: int, lines: int, on: bool, status: str = "") -> list:
    """The whole screen as lines (pure, like dashboard.render)."""
    width = max(30, columns)
    room = width - 4
    keys = ui.paint("Tab ↑↓ ←→ node   PgUp PgDn scroll   p pause   +/- interval   r refresh   e screen   w window   q back" if width >= 96 else
                    "Tab ↑↓ node  PgUp/Dn  p  r  e  w  q" if width >= 40 else "Tab ↑↓ PgUp q", ui.GREY, on)
    if not view.nodes:
        head = ui.paint("waiting for the first data…" if not view.error else f"⚠️ kubectl: {view.error}", ui.GREY, on)
        return ui.box("🖥 NODES", [[head], [keys]], on, width)
    i = node_index(view, state)
    n = view.nodes[i]
    state.name = n.name
    ready = sum(1 for x in view.nodes if x.ready)
    title = (f"🖥 NODES · {i + 1}/{len(view.nodes)} · {ready} Ready" + (f" · {ui.paint('PAUSED', ui.YELLOW, on)}" if state.paused else "")
             + f" · {state.interval:g} s · {time.strftime('%H:%M:%S')}")
    head = node_bar(view, i, room, on)
    if view.error:
        head.append(ui.paint(f"⚠️ kubectl: {view.error}", ui.RED, on))
    if status:
        head.append(ui.paint(status, ui.GREY, on))
    role = ui.paint("master" if n.master else "worker", ui.BOLD, on)
    head.append(f"{ui.paint(n.name, ui.BOLD, on)}  {role}  {ui.paint('Ready' if n.ready else 'NotReady', ui.GREEN if n.ready else ui.RED, on)}"
                f"  {n.arch}  {n.ip}")
    body = node_body(view, n, room, on)
    base = len(ui.box(title, [head, [""], [keys]], on, width)) - 1
    avail = max(3, lines - 1 - base)
    foot = [keys]
    if len(body) > avail:
        avail = max(2, avail - 1)
        state.scroll = max(0, min(state.scroll, len(body) - avail))
        foot.append(ui.paint(f"↕ rows {state.scroll + 1}-{min(len(body), state.scroll + avail)} of {len(body)}  (PgUp PgDn, make the window taller)", ui.GREY, on))
    else:
        state.scroll = 0
    return ui.box(title, [head, body[state.scroll:state.scroll + avail], foot], on, width)


class NodesScreen(d.Dashboard):
    """The dashboard's loop (collector thread, redraw, resize) with the NODES layout and keys."""

    def __init__(self, kube: Kubectl, only: Optional[list] = None, stream=None, keys=None, keep_hours: int = 0) -> None:
        super().__init__(kube, only, stream, keys, keep_hours)
        self.nodes_state = NodesState()
        self.state = self.nodes_state                      # the shared parts of Dashboard use interval / paused

    def draw(self) -> None:
        columns, lines = self._size()
        if self.status and time.monotonic() > self._status_until:
            self.status = ""
        frame = render_nodes(self.collector.view, self.nodes_state, columns, lines, ui.color_enabled(), self.status)[:max(10, lines - 1)]
        if self.nodes_state.window_pick:                             # the question of key w replaces the screen until it is answered
            from . import notify
            frame = notify.pick_lines(self.nodes_state.window_pick, ui.color_enabled(), max(40, min(columns - 1, 100)))
        self._frame, self._frame_size = frame, (columns, lines)
        self.canvas.draw(frame)

    def window_argv(self) -> list:
        argv = super().window_argv()
        argv[argv.index("--dashboard")] = "--nodes-view"
        return argv

    def handle(self, key: str) -> bool:
        s, nodes = self.nodes_state, self.collector.view.nodes
        if s.window_pick:
            return not self._pick_key(key)
        key = key.lower() if len(key) == 1 else key
        if key == "w":                                                    # the screen moves to a new window
            return not self._w_key()
        i = node_index(self.collector.view, s)

        def go(step: int = 0, to: Optional[int] = None) -> None:
            if nodes:
                s.name = nodes[to if to is not None else (i + step) % len(nodes)].name
                s.scroll = 0

        if key in ("q", "esc"):
            return False
        if key in ("tab", "down", "right"):
            go(1)
        elif key in ("btab", "up", "left"):
            go(-1)
        elif key == "home":
            go(to=0)
        elif key == "end":
            go(to=len(nodes) - 1)
        elif key == "pgup":
            s.scroll = max(0, s.scroll - 8)
        elif key == "pgdn":
            s.scroll += 8                                   # render clamps it to the end of the body
        elif key in ("+", "="):
            s.interval = d.INTERVALS[min(len(d.INTERVALS) - 1, d._nearest(s.interval) + 1)]
        elif key in ("-", "_"):
            s.interval = d.INTERVALS[max(0, d._nearest(s.interval) - 1)]
        elif key == "p":
            s.paused = not s.paused
        elif key == "r":
            self._wake.set()
        elif key == "e":
            if self._frame:
                written = ui.save_screen(self._frame, "nodes", *self._frame_size)
                self._say(f"📸 screen saved: {written[0].parent.name}/{written[0].name}" if written else "❌ the screen could not be saved (scr/ is not writable)")
        return True


def run(only: Optional[list] = None, keep_hours: int = 0) -> int:
    """`--nodes-view` / menu 8 ▸ list nodes."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        ui.emit("❌ The nodes screen needs a terminal (stdin and stdout); --list-nodes prints a plain table.")
        return 1
    return NodesScreen(Kubectl(), only, keep_hours=keep_hours).run()
