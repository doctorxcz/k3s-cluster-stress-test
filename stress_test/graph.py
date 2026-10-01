"""Fine graphs for the dashboard page `g` (2026-10-01).

* a Braille canvas: one character holds 2 x 4 dots, so a graph has twice the width and up to four times the height of the old
  block bars; a fallback with block characters stays for fonts without Braille (key b),
* axes with the values in a small (superscript) font, a limit line, the part above the limit in red, min-max bands so that a
  short peak never disappears when old data is averaged,
* time windows (1 min .. all) with scrolling, a cursor that reads the values at one moment,
* one graph per quantity, or all of them over each other in one graph (combined view, an experiment),
* the data of the graph as CSV / JSON.

Everything here is pure (data in, lines out), so it is tested for every width and height.
"""
from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import ui

# colours of the series (256-colour codes, like the rest of the GUI) and of the cursor
MAGENTA, BLUE, WHITE = "38;5;176", "38;5;110", "38;5;255"
WINDOWS = (60, 300, 1200, 3600, 0)                              # seconds; 0 = everything that is kept
WINDOW_NAMES = ("1 min", "5 min", "20 min", "1 h", "all")
_SUP = str.maketrans("0123456789+-.%abcdefghijklmnoprstuvwxyz", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻·%ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ")
_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80}
_BLOCKS = " ▁▂▃▄▅▆▇█"


SMALL_FONT = False        # the small (superscript) font was hard to read (2026-10-01): off, the numbers are normal characters


def small(text) -> str:
    """The small (superscript) form of a text: `71°` -> `⁷¹°`. Characters without a small form stay as they are."""
    return str(text).translate(_SUP)


def mini(text) -> str:
    """The text for labels: normal characters (the small font can be switched on with SMALL_FONT)."""
    return small(text) if SMALL_FONT else str(text)


def num(value: Optional[float], digits: int = 0, unit: str = "") -> str:
    """A number for a label; a rate (B/s) gets k / M so that it stays short."""
    if value is None:
        return "–"
    if unit == "B/s":
        for suffix, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
            if abs(value) >= div:
                return f"{value / div:.1f}{suffix}" if abs(value) < 10 * div else f"{value / div:.0f}{suffix}"
    return f"{value:.{digits}f}"


# ================================================================ data: from samples to columns ============================
@dataclass
class Series:
    """One quantity: `avg`, `lo`, `hi` are per drawing column, already scaled to 0..1 (None = no data there)."""

    name: str
    unit: str
    color: str
    avg: list
    lo: list
    hi: list
    top: float
    floor: float = 0.0
    limit: Optional[float] = None            # in the unit of the series (a line, and red above it)
    raw: list = field(default_factory=list)  # (time, value) pairs of the window - for the cursor
    fill: bool = False


def window_range(times: Sequence[float], seconds: int, offset: int) -> tuple:
    """(start, end) of the shown window. `offset` = how many seconds the window was moved back from the newest sample."""
    if not times:
        now = time.time()
        return now - (seconds or 60), now
    end = times[-1] - max(0, offset)
    start = times[0] if seconds == 0 else end - seconds
    return (start, end) if end > start else (start, start + 1)


def resample(times: Sequence[float], values: Sequence[Optional[float]], start: float, end: float, n: int) -> tuple:
    """(avg, lo, hi) lists of `n` columns over [start, end]: the samples of a column are averaged, their extremes are kept; a column without
    a sample is interpolated between its neighbours while they are close (up to 4 sample spacings), otherwise it stays empty."""
    avg: list = [None] * n
    lo: list = [None] * n
    hi: list = [None] * n
    span = max(end - start, 1e-9)
    sums = [0.0] * n
    count = [0] * n
    for t, v in zip(times, values):
        if v is None or t < start or t > end:
            continue
        i = min(n - 1, int((t - start) / span * n))
        sums[i] += v
        count[i] += 1
        lo[i] = v if lo[i] is None else min(lo[i], v)
        hi[i] = v if hi[i] is None else max(hi[i], v)
    known = [i for i in range(n) if count[i]]
    for i in known:
        avg[i] = sums[i] / count[i]
    if len(times) > 1:
        gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
        spacing = gaps[len(gaps) // 2] if gaps else 0.0
        step = span / n
        for a, b in zip(known, known[1:]):
            if b - a > 1 and (b - a) * step <= 4 * max(spacing, step):
                for i in range(a + 1, b):
                    f = (i - a) / (b - a)
                    avg[i] = lo[i] = hi[i] = avg[a] + (avg[b] - avg[a]) * f
    return avg, lo, hi


def make_series(name: str, unit: str, color: str, times: Sequence[float], values: Sequence[Optional[float]], window: tuple, n: int, top: float,
                floor: float = 0.0, limit: Optional[float] = None, fill: bool = False) -> Series:
    start, end = window
    avg, lo, hi = resample(times, values, start, end, n)
    scale = lambda v: None if v is None else max(0.0, min(1.0, (v - floor) / max(top - floor, 1e-9)))       # noqa: E731
    raw = [(t, v) for t, v in zip(times, values) if v is not None and start <= t <= end]
    return Series(name, unit, color, [scale(v) for v in avg], [scale(v) for v in lo], [scale(v) for v in hi], top, floor, limit, raw, fill)


def value_at(series: Series, moment: float) -> Optional[float]:
    """The value of the sample nearest to `moment` (None when there is none within 3 s or the window is empty)."""
    if not series.raw:
        return None
    t, v = min(series.raw, key=lambda p: abs(p[0] - moment))
    gaps = [b[0] - a[0] for a, b in zip(series.raw, series.raw[1:]) if b[0] > a[0]]
    spacing = sorted(gaps)[len(gaps) // 2] if gaps else 3.0
    return v if abs(t - moment) <= max(3.0, 2 * spacing) else None


# ================================================================ drawing ==================================================
def _braille(series: list, cells_w: int, cells_h: int, cursor_px: Optional[int], limit_rows: list) -> list:
    """Rows of (character, colour) pairs from the series, with dotted limit lines and an optional cursor."""
    pw, ph = 2 * cells_w, 4 * cells_h
    bits = [[0] * cells_w for _ in range(cells_h)]
    color: list = [[None] * cells_w for _ in range(cells_h)]

    def put(px: int, py: int, code: str) -> None:
        if 0 <= px < pw and 0 <= py < ph:
            cx, cy = px // 2, py // 4
            bits[cy][cx] |= _BIT[(px % 2, py % 4)]
            color[cy][cx] = code

    def ypx(v: float) -> int:
        return round((1.0 - v) * (ph - 1))

    for norm, code in limit_rows:
        for px in range(0, pw, 2):
            put(px, ypx(norm), code)
    for s in series:
        prev = None
        for px in range(pw):
            v = s.avg[px]
            if v is None:
                prev = None
                continue
            ys = [ypx(s.lo[px]), ypx(s.hi[px]), ypx(v)] + ([prev] if prev is not None else [])
            top_y, bottom_y = min(ys), max(ys)
            if s.fill:
                bottom_y = ph - 1
            over = s.limit is not None and s.top > s.floor and (v * (s.top - s.floor) + s.floor) > s.limit
            for py in range(top_y, bottom_y + 1):
                put(px, py, ui.RED if over else s.color)
            prev = ypx(v)
    if cursor_px is not None:
        for py in range(0, ph, 2):
            put(cursor_px, py, WHITE)
    return [[(chr(0x2800 + bits[r][c]) if bits[r][c] else " ", color[r][c]) for c in range(cells_w)] for r in range(cells_h)]


def _blocks(series: list, cells_w: int, cells_h: int, cursor_px: Optional[int], limit_rows: list) -> list:
    """The fallback for fonts without Braille: bars of eighth blocks for the first series, a dot for the others."""
    grid: list = [[(" ", None)] * cells_w for _ in range(cells_h)]
    for norm, code in limit_rows:
        row = min(cells_h - 1, max(0, round((1.0 - norm) * (cells_h - 1))))
        grid[row] = [("┄", code) if ch == " " else (ch, c) for ch, c in grid[row]]
    for k, s in enumerate(series):
        for x in range(cells_w):
            px = min(len(s.avg) - 1, x * len(s.avg) // max(1, cells_w))
            v = s.avg[px]
            if v is None:
                continue
            code = ui.RED if s.limit is not None and (v * (s.top - s.floor) + s.floor) > s.limit else s.color
            level = round(v * cells_h * 8)
            if k == 0:
                for r in range(cells_h):
                    cell = max(0, min(8, level - (cells_h - 1 - r) * 8))
                    if cell:
                        grid[r][x] = (_BLOCKS[cell], code)
            else:
                grid[min(cells_h - 1, max(0, cells_h - 1 - (level - 1) // 8 if level else cells_h - 1))][x] = ("•", s.color)
    if cursor_px is not None:
        x = min(cells_w - 1, cursor_px * cells_w // max(1, 2 * cells_w))
        for r in range(cells_h):
            grid[r][x] = ("│" if grid[r][x][0] == " " else grid[r][x][0], WHITE if grid[r][x][0] == " " else grid[r][x][1])
    return grid


def draw(series: list, cells_w: int, cells_h: int, on: bool, braille: bool = True, cursor: Optional[int] = None,
         limit_rows: Optional[list] = None) -> list:
    """The plot as strings (`cells_w` characters each). `cursor` = a column of the plot (0..cells_w-1)."""
    limit_rows = limit_rows or []
    if braille:
        grid = _braille(series, cells_w, cells_h, None if cursor is None else cursor * 2, limit_rows)
    else:
        grid = _blocks(series, cells_w, cells_h, None if cursor is None else cursor * 2, limit_rows)
    return ["".join(ui.paint(ch, code, on) if code and ch != " " else ch for ch, code in row) for row in grid]


def with_axis(plot: list, top_label: str, mid_label: str, bottom_label: str, on: bool, gutter: int) -> list:
    """The plot with a vertical axis: the labels (small font, grey) at the top, in the middle and at the bottom."""
    out = []
    last = len(plot) - 1
    for i, row in enumerate(plot):
        label = top_label if i == 0 else bottom_label if i == last else mid_label if i == last // 2 and last >= 4 else ""
        out.append(ui.paint(mini(label).rjust(gutter), ui.GREY, on) + ui.paint("┤" if label else "│", ui.GREY, on) + row)
    return out


def time_axis(window: tuple, seconds: int, offset: int, cells_w: int, gutter: int, on: bool, cursor: Optional[int] = None) -> list:
    """The row under the plots: `└──── -20 min ──── -10 min ──── now`, or clock times after the window was moved back."""
    start, end = window
    if offset or seconds == 0:
        labels = [time.strftime("%H:%M:%S", time.localtime(start)), time.strftime("%H:%M:%S", time.localtime((start + end) / 2)),
                  time.strftime("%H:%M:%S", time.localtime(end))]
    else:
        labels = [f"-{_dur(seconds)}", f"-{_dur(seconds // 2)}", "now"]
    row = [" "] * cells_w
    for text, where in ((mini(labels[0]), 0), (mini(labels[1]), max(0, cells_w // 2 - len(labels[1]) // 2)), (mini(labels[2]), max(0, cells_w - len(labels[2])))):
        for k, ch in enumerate(text):
            if where + k < cells_w:
                row[where + k] = ch
    axis = ui.paint(" " * gutter + "└" + "─" * cells_w, ui.GREY, on)
    return [axis, ui.paint(" " * (gutter + 1) + "".join(row), ui.GREY, on)]


def _dur(seconds: int) -> str:
    return f"{seconds // 3600} h" if seconds >= 3600 and seconds % 3600 == 0 else f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def header(s: Series, on: bool, stats: bool = True) -> str:
    """The line above a graph: a colour mark, the name and the unit, and the numbers in the small font - all in the colour of the series."""
    known = [v for _t, v in s.raw]
    line = ui.paint("■ ", s.color, on) + ui.paint(f"{s.name} {s.unit}".strip(), s.color + ";1" if on else "", on)
    if known and stats:
        u = s.unit
        line += "  " + ui.paint(f"now {num(known[-1], 0, u)}", s.color + ";1" if on else "", on) + ui.paint(
            f"   min {num(min(known), 0, u)}   max {num(max(known), 0, u)}   avg {num(sum(known) / len(known), 0, u)}", s.color, on)
    elif not known:
        line += ui.paint("  no data in this window", ui.GREY, on)
    return line


# ================================================================ the page =================================================
@dataclass
class GraphState:
    window: int = 1                      # index in WINDOWS
    offset: int = 0                      # seconds the window is moved back
    cursor: Optional[int] = None         # a plot column, None = off
    combined: bool = False
    braille: bool = False                # blocks are the default (easier to look at); b switches to Braille dots
    fill: bool = False


def node_series(n, state: GraphState, cells_w: int, window: tuple) -> list:
    """The quantities of a node that have data, as Series for the shown window (separate view: one graph each)."""
    t = list(n.h_t)
    spec = [("CPU", "%", ui.GREEN, n.h_cpu, 100.0, 0.0, None), ("TEMP", "°C", ui.YELLOW, n.h_temp, 90.0, 25.0, 80.0),
            ("RAM", "%", ui.CYAN, n.h_mem, 100.0, 0.0, 90.0), ("CLOCK", "MHz", MAGENTA, n.h_freq, None, 0.0, None),
            ("POWER", "W", BLUE, n.h_pwr, None, 0.0, None)]
    if n.gpu is not None:
        spec += [("GPU TEMP", "°C", ui.YELLOW, n.h_gpu, 90.0, 25.0, 80.0), ("GPU LOAD", "%", ui.GREEN, n.h_gu, 100.0, 0.0, None)]
    if n.nics:
        spec.append(("NET ↓+↑", "B/s", ui.CYAN, n.h_net, None, 0.0, None))
    out = []
    for name, unit, color, hist, top, floor, limit in spec:
        values = list(hist)
        if len(values) != len(t):                                   # the lists are filled together; a mismatch means no usable history
            values = values[-len(t):] if len(values) > len(t) else [None] * (len(t) - len(values)) + values
        known = [v for v in values if v is not None]
        if top is None:
            top = max(known + [1.0]) * 1.1
        elif known and max(known) > top:
            top = max(known)
        out.append(make_series(name, unit, color, t, values, window, cells_w * 2, top, floor, limit, state.fill))
    return out


def cursor_moment(window: tuple, cursor: Optional[int], cells_w: int) -> Optional[float]:
    if cursor is None:
        return None
    start, end = window
    return start + (end - start) * (min(cursor, cells_w - 1) + 0.5) / max(1, cells_w)


def readout(series: list, moment: Optional[float], on: bool, room: int = 100) -> list:
    """The lines of the cursor: the time, and the value of every quantity at that moment (wrapped to the width)."""
    if moment is None:
        return [ui.paint(mini("cursor: , and . move it, c hides it"), ui.GREY, on)]
    parts = [ui.paint("⏱ " + time.strftime("%H:%M:%S", time.localtime(moment)), WHITE + ";1" if on else "", on)]
    for s in series:
        v = value_at(s, moment)
        parts.append(ui.paint(f"{s.name} {num(v, 1 if s.top <= 10 else 0, s.unit)}{'' if v is None else s.unit}", s.color, on))
    lines, current = [], ""
    for part in parts:
        if current and ui.visible_len(current) + 2 + ui.visible_len(part) > room:
            lines.append(current)
            current = part
        else:
            current = part if not current else current + "  " + part
    return lines + [current]


def page(n, state: GraphState, interval: float, columns: int, lines: int, on: bool) -> tuple:
    """(title, sections) of the graph page of a node - the content for `ui.box`."""
    room = columns - 4
    t = list(n.h_t)
    window = window_range(t, WINDOWS[state.window % len(WINDOWS)], state.offset)
    seconds = WINDOWS[state.window % len(WINDOWS)]
    probe = node_series(n, state, 4, window)                         # the labels of the vertical axis decide the width of its column
    gutter = max([4] + [len(num(s.top, 0, s.unit)) for s in probe]) + 1
    cells_w = max(8, room - gutter - 1)
    all_series = node_series(n, state, cells_w, window)
    state.cursor = None if state.cursor is None else max(0, min(state.cursor, cells_w - 1))
    moment = cursor_moment(window, state.cursor, cells_w)
    title = f"📈 {n.name} · history · {WINDOW_NAMES[state.window % len(WINDOWS)]} · {state.fill and 'area' or 'line'} · {interval:g} s"
    usable = [s for s in all_series if s.raw]
    body: list = []
    ro = readout(usable, moment, on, room)
    avail = max(3, lines - 8 - len(ro) - 2)                         # the lines left for the graphs: frame (7 + 1 spare), the readout, the time axis
    if not t or not usable:
        body = [ui.paint("no data yet - the history fills in while the dashboard runs", ui.GREY, on)]
    elif state.combined:
        use = [s for s in usable if s.name in ("CPU", "TEMP", "CLOCK", "POWER", "GPU TEMP")] or usable
        rows = max(3, min(24, avail - len(use) - 1))
        body.append(ui.paint(mini("each line has its own scale (0-100 % of its range), the numbers are in the legend"), ui.GREY, on))
        for s in use:
            body.append(header(s, on))
        plot = draw(use, cells_w, rows, on, state.braille, state.cursor)
        body += with_axis(plot, "max", "", "min", on, gutter)
        body += time_axis(window, seconds, state.offset, cells_w, gutter, on)
    else:
        shown = usable[:max(1, avail // 3)]                         # a graph needs its header and at least two rows
        left = avail - (1 if len(shown) < len(usable) else 0)
        rows = max(2, min(8, left // len(shown) - 1))
        for s in shown:
            body.append(header(s, on))
            limit_rows = [] if s.limit is None else [((s.limit - s.floor) / max(s.top - s.floor, 1e-9), ui.RED)]
            plot = draw([s], cells_w, rows, on, state.braille, state.cursor, [(v, c) for v, c in limit_rows if 0.0 <= v <= 1.0])
            body += with_axis(plot, num(s.top, 0, s.unit), num((s.top + s.floor) / 2, 0, s.unit) if rows >= 4 else "", num(s.floor, 0, s.unit), on, gutter)
        body += time_axis(window, seconds, state.offset, cells_w, gutter, on)
        if len(shown) < len(usable):
            body.append(ui.paint(mini(f"{len(usable) - len(shown)} more graphs do not fit: make the window taller or press x"), ui.GREY, on))
    keys = ("↑↓ node  [ ] window  ← → time  , . cursor  b dots  x combined  a area  t text  s data  e screen  q" if room >= 98 else
            "↑↓ [ ] ← → , . b x a t s e q" if room >= 26 else "↑↓ [ ] , . q")
    sections = [body, ro, [ui.paint(keys, ui.GREY, on)]]
    return title, sections


# ================================================================ the graphs as a text file ================================
REPORT_WIDTH = 100
TABLE_COLUMNS = (("cpu %", "h_cpu", 0), ("temp °C", "h_temp", 0), ("ram %", "h_mem", 0), ("clock MHz", "h_freq", 0), ("power W", "h_pwr", 1),
                 ("gpu °C", "h_gpu", 0), ("gpu %", "h_gu", 0), ("net B/s", "h_net", -1))


def text_report(n, state: GraphState, interval: float, width: int = REPORT_WIDTH, moment: Optional[float] = None) -> str:
    """The graphs of a node as plain text for `cat` / `less` / `batcat`: every graph (not only what fits the window), a fixed width,
    blocks (they show in every font), no colours - and a table of the numbers under them."""
    seconds = WINDOWS[state.window % len(WINDOWS)]
    t = list(n.h_t)
    window = window_range(t, seconds, state.offset)
    start, end = window
    lines = [f"GRAPH {n.name}",
             f"saved {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(moment or time.time()))} · window {WINDOW_NAMES[state.window % len(WINDOWS)]} "
             f"({time.strftime('%H:%M:%S', time.localtime(start))} - {time.strftime('%H:%M:%S', time.localtime(end))}) · interval {interval:g} s · {len(t)} samples kept", ""]
    probe = node_series(n, state, 4, window)
    gutter = max([4] + [len(num(s.top, 0, s.unit)) for s in probe]) + 1
    cells_w = max(20, width - gutter - 1)
    series = [s for s in node_series(n, state, cells_w, window) if s.raw]
    if not t or not series:
        return "\n".join(lines + ["no data in this window - the history fills in while the dashboard runs"]) + "\n"
    for s in series:
        lines.append(header(s, False))
        limit_rows = [] if s.limit is None else [((s.limit - s.floor) / max(s.top - s.floor, 1e-9), ui.RED)]
        plot = draw([s], cells_w, 6, False, False, None, [(v, c) for v, c in limit_rows if 0.0 <= v <= 1.0])
        lines += with_axis(plot, num(s.top, 0, s.unit), num((s.top + s.floor) / 2, 0, s.unit), num(s.floor, 0, s.unit), False, gutter)
        lines += time_axis(window, seconds, state.offset, cells_w, gutter, False)
        lines.append("")
    span = max(end - start, 1.0)
    rows = max(2, min(60, int(span // 60) + 1))
    step = span / rows
    table = {}
    for name, attr, digits in TABLE_COLUMNS:
        values = list(getattr(n, attr))
        if len(values) != len(t):
            values = values[-len(t):] if len(values) > len(t) else [None] * (len(t) - len(values)) + values
        table[name] = resample(t, values, start, end, rows)[0]
    shown = [name for name, _a, _d in TABLE_COLUMNS if any(v is not None for v in table[name])]
    lines.append(f"TABLE (the average of every {_dur(int(round(step)))})")
    lines.append(f"{'time':<9}" + "".join(f"{name:>11}" for name in shown))
    digits_of = {name: d for name, _a, d in TABLE_COLUMNS}
    for i in range(rows):
        cells = []
        for name in shown:
            v = table[name][i]
            cells.append(f"{'-':>11}" if v is None else f"{num(v, max(0, digits_of[name]), 'B/s' if digits_of[name] < 0 else ''):>11}")
        lines.append(f"{time.strftime('%H:%M:%S', time.localtime(start + (i + 0.5) * step)):<9}" + "".join(cells))
    return "\n".join(line.rstrip() for line in lines) + "\n"


# ================================================================ the data as files ========================================
FIELDS = (("cpu_pct", "h_cpu"), ("temp_c", "h_temp"), ("ram_pct", "h_mem"), ("clock_mhz", "h_freq"), ("power_w", "h_pwr"),
          ("gpu_temp_c", "h_gpu"), ("gpu_load_pct", "h_gu"), ("net_bytes_per_s", "h_net"))


def export_rows(n) -> list:
    """Every kept sample of a node as a dict (time, the values - None where a quantity was not measured)."""
    t = list(n.h_t)
    columns = {name: list(getattr(n, attr)) for name, attr in FIELDS}
    rows = []
    for i, moment in enumerate(t):
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment)), "epoch": round(moment, 3)}
        for name, values in columns.items():
            offset = len(values) - len(t)
            j = i + offset
            row[name] = values[j] if 0 <= j < len(values) else None
        rows.append(row)
    return rows


def to_csv(rows: list) -> str:
    out = io.StringIO()
    fields = ["time", "epoch"] + [name for name, _a in FIELDS]
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})
    return out.getvalue()


def to_json(node: str, interval: float, rows: list) -> str:
    return json.dumps({"node": node, "interval_s": interval, "samples": rows}, ensure_ascii=False, indent=1) + "\n"
