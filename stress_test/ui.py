"""Terminal look of the main menu ("retro" theme): logo, heavy frame, colours.

Colours are used only on a terminal (never with NO_COLOR or when the output goes to a file); without them
the same layout is printed as plain text.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import unicodedata
from typing import Optional

WIDTH = 76
COMPACT_BELOW, WIDE_FROM, WIDE_MAX = 60, 100, 200      # terminal columns: compact < 60 <= normal < 100 <= wide (<= 200 used)
MIN_COLS = 30                                          # narrower terminals are laid out as if they had this many columns
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

GREEN, YELLOW, RED, GREY, BOLD, CYAN = "38;5;79", "38;5;214", "38;5;203", "38;5;245", "1", "38;5;117"


def color_enabled(is_tty: Optional[bool] = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() if is_tty is None else is_tty


def paint(text: str, code: str, on: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if on else text


def _cells(text: str):
    """(character, width in terminal cells) pairs: emoji take two cells, a character + U+FE0F too."""
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch in ("\ufe0f", "\u200d") or unicodedata.combining(ch):
            yield ch, 0
        elif unicodedata.east_asian_width(ch) in ("W", "F") or (i + 1 < len(chars) and chars[i + 1] == "\ufe0f"):
            yield ch, 2
        else:
            yield ch, 1


# Emoji that need the variation selector U+FE0F to look like emoji are drawn two cells wide by some terminals and one cell
# by others, which shifts the right border of a frame. Inside frames they are replaced by emoji that are always two cells.
_EMOJI_SAFE = {"🌡️": "🔥", "🖥️": "💻", "⚠️": "❗", "ℹ️": "📌", "⏱️": "⏳", "↪️": "⏩", "❄️": "🧊", "✔️": "✅", "🛑": "🛑"}


def safe_cells(text: str) -> str:
    """`text` with the emoji that have an unreliable width replaced (and any stray U+FE0F removed) - for frame rows."""
    for bad, good in _EMOJI_SAFE.items():
        if bad in text:
            text = text.replace(bad, good)
    return text.replace("\ufe0f", "")


def visible_len(text: str) -> int:
    """Width in terminal cells (colour codes take none, emoji take two)."""
    return sum(w for _, w in _cells(_ANSI.sub("", text)))


def fit(text: str, room: int) -> str:
    """Cuts `text` to `room` cells, keeps the colour codes, marks the cut with an ellipsis."""
    if visible_len(text) <= room:
        return text
    out, used = [], 0
    tokens = re.split(r"(\x1b\[[0-9;]*m)", text)
    for token in tokens:
        if _ANSI.fullmatch(token):
            out.append(token)
            continue
        for ch, w in _cells(token):
            if used + w > room - 1:
                return "".join(out) + "…" + ("\x1b[0m" if "\x1b[" in text else "")
            out.append(ch)
            used += w
    return "".join(out)


def logo(on: bool) -> str:
    """The icon as one line: gauge ends (green/red), the three cluster nodes (last one hot)."""
    return (paint("◖", GREEN, on) + paint("●", GREEN, on) + paint("▲", YELLOW, on)
            + paint("●", RED, on) + paint("◗", RED, on))


def _clip(text: str, room: int) -> str:
    return text if len(text) <= room else text[:max(0, room - 1)] + "…"


REPO_URL = "github.com/doctorxcz/k3s-cluster-stress-test"
# wide emoji (two cells in every terminal) used as icons of the live screens
ICON_NODE, ICON_CPU, ICON_TEMP, ICON_RAM, ICON_CLOCK = "💻", "⚡", "🔥", "🧠", "📈"
ICON_TIME, ICON_STAGE, ICON_READY, ICON_GO, ICON_COOL, ICON_POWER = "⏳", "🎯", "🚦", "🟢", "🧊", "🔌"
ICON_OK, ICON_FAIL, ICON_PING, ICON_GPU, ICON_FAN = "✅", "❌", "📡", "🎮", "🌀"


def _row(content: str, on: bool, edge: str = "┃") -> str:
    pad = max(0, WIDTH - 2 - visible_len(content))
    return f"{paint(edge, GREY, on)}{content}{' ' * pad}{paint(edge, GREY, on)}"


def _rule(left: str, right: str, on: bool, tag: str = "", width: int = WIDTH) -> str:
    """A horizontal line; `tag` is a small text sunk into it, right-aligned (the repo address above the version)."""
    if tag and width > len(tag) + 12:
        return paint(left + "━" * (width - 5 - len(tag)) + " ", GREY, on) + paint(tag, GREY, on) \
            + paint(" ━" + right, GREY, on)
    return paint(left + "━" * (width - 2) + right, GREY, on)


def header(title: str, version: str, cluster: str, on: bool) -> list:
    head = f" {logo(on)} {paint(title, BOLD, on)}"
    ver = paint(f"v{version} ", GREY, on)
    gap = max(1, WIDTH - 2 - visible_len(head) - visible_len(ver))
    return [
        _rule("┏", "┓", on, tag=REPO_URL),
        _row(head + " " * gap + ver, on),
        _row(" " + paint("› ", GREEN, on) + paint(cluster, GREY, on), on),
        _rule("┣", "┫", on),
    ]


def item(key: str, name: str, hint: str, on: bool) -> str:
    bar = paint("▁▃▅▇", GREEN, on)
    content = (f" {paint(key, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} "
               f"{paint(f'{name:<8}', BOLD, on)}{bar}  {paint(hint, GREY, on)}")
    return _row(content, on)


def footer(quit_label: str, on: bool) -> list:
    return [
        _row(f" {paint('0', BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {paint(quit_label, BOLD, on)}", on),
        _rule("┗", "┛", on),
    ]


def prompt_text(on: bool) -> str:
    return paint("stress", CYAN, on) + paint("@", GREY, on) + paint("k3s", GREEN, on) + paint(":~$", GREY, on)


# --- terminal width: three modes (compact / normal / wide) ----------------------------------------
def _env_cols() -> Optional[int]:
    """STRESS_TEST_COLUMNS = a fixed number of columns (tests, pyte drivers); None when not set / not a number."""
    raw = os.environ.get("STRESS_TEST_COLUMNS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def term_cols(stream=None) -> Optional[int]:
    """Columns of the terminal, read NOW (the window may have been resized). STRESS_TEST_COLUMNS overrides it;
    None = no terminal and no override (pipes, log files, tests): the plain layout of width WIDTH is used."""
    forced = _env_cols()
    if forced is not None:
        return forced
    stream = stream or sys.stdout
    try:
        if stream.isatty():
            cols = os.get_terminal_size(stream.fileno()).columns
            return cols if cols > 0 else None
    except (OSError, ValueError, AttributeError):
        pass
    return None


def adaptive() -> bool:
    """True when the layout follows the terminal width (a terminal, or STRESS_TEST_COLUMNS is set)."""
    return term_cols() is not None


def cols() -> int:
    """Columns to lay out for (the plain width + 1 without a terminal)."""
    return term_cols() or WIDTH + 1


def mode(columns: Optional[int] = None) -> str:
    """'compact' (< 60 columns), 'normal' (60..99) or 'wide' (>= 100)."""
    columns = cols() if columns is None else columns
    return "compact" if columns < COMPACT_BELOW else "wide" if columns >= WIDE_FROM else "normal"


def avail(limit: int = WIDE_MAX, columns: Optional[int] = None) -> int:
    """Width a line may have: one column less than the terminal (so it never wraps), at most `limit`.
    Without a terminal it is min(limit, WIDTH)."""
    columns = term_cols() if columns is None else columns
    if columns is None:
        return min(limit, WIDTH)
    return max(MIN_COLS, min(limit, columns - 1))


def panel_width(columns: Optional[int] = None) -> int:
    """Width of submenus / question panels: the normal look is WIDTH wide, a wide terminal gets more."""
    columns = term_cols() if columns is None else columns
    if columns is None:
        return WIDTH
    return avail(120 if mode(columns) == "wide" else WIDTH, columns)


def rule(char: str = "=", length: int = 52) -> str:
    """A horizontal rule of `length`, shortened to the terminal when the layout is adaptive."""
    return char * (min(length, avail()) if adaptive() else length)


def cut_word(word: str, room: int) -> str:
    """A word that does not fit is cut with an ellipsis: a path keeps its end (the file name) - `/home/…/node.log` -
    everything else (URLs, names) keeps its beginning."""
    room = max(room, 5)
    if visible_len(word) <= room:
        return word
    if "/" in word and not word.startswith(("http://", "https://")) and room >= 12:
        base = word[word.rindex("/"):]
        tail = base if len(base) <= room - 6 else word[-(room // 2):]
        head = word[:room - len(tail) - 1]
        return head + "…" + tail
    return fit(word, room)


def wrap(text: str, width: int, indent: str = "", hang: Optional[str] = None) -> list:
    """`text` wrapped at word boundaries to `width` cells. The first line starts with `indent`, the next ones with `hang`
    (default = indent: a hanging indent is a longer `hang`). A word that is wider than the line (URL, path) gets a line
    of its own and is cut with an ellipsis - it is never split."""
    hang = indent if hang is None else hang
    width = max(width, 12)
    lines, cur, has = [], indent, False
    for word in text.split():
        if visible_len(cur) + (1 if has else 0) + visible_len(word) <= width:
            cur += (" " if has else "") + word
            has = True
            continue
        if has:
            lines.append(cur)
            cur, has = hang, False
        room = width - visible_len(cur)
        cur += cut_word(word, room)
        has = True
    lines.append(cur)
    return [ln.rstrip() for ln in lines]


def columns2(left: list, right: list, col: int, sep: str = " │ ") -> list:
    """Two columns side by side (each line of the columns is cut to `col` cells and padded)."""
    out = []
    for i in range(max(len(left), len(right))):
        a = fit(left[i], col) if i < len(left) else ""
        b = fit(right[i], col) if i < len(right) else ""
        out.append(a + " " * max(0, col - visible_len(a)) + sep + b)
    return out


def grid(cells: list, width: Optional[int] = None, lead: str = "", gap: int = 3) -> list:
    """Short items (node names...) in as many columns as fit the width, filled column by column."""
    width = width or avail(130)
    cell_w = max((visible_len(c) for c in cells), default=1)
    per_row = max(1, (width - len(lead) + gap) // (cell_w + gap))
    if per_row == 1 or len(cells) <= 1:
        return [lead + fit(c, width - len(lead)) for c in cells]
    rows = -(-len(cells) // per_row)
    cols_ = [cells[i * rows:(i + 1) * rows] for i in range(per_row)]
    out = []
    for r in range(rows):
        out.append((lead + (" " * gap).join(c[r].ljust(cell_w) if r < len(c) else "" for c in cols_)).rstrip())
    return out


def kv_block(pairs: list, width: Optional[int] = None, md: Optional[str] = None, lead: str = "  ",
             label_w: int = 21, colon: bool = True) -> list:
    """Key / value rows (plain text). Without a terminal: `lead + 'Label:' padded to label_w + value` (the classic look).
    Adaptive: wide = two columns of pairs, normal = one column with the long values wrapped under the value column,
    compact = the label on its own line and the value indented below it."""
    if not adaptive() and width is None:
        return [lead + (f"{k + ':' if colon else k}".ljust(label_w)) + v for k, v in pairs]
    width = width or avail(130)
    md = md or mode()
    marks = [(k + ":" if colon else k) for k, _ in pairs]
    lw = min(max((len(m) for m in marks), default=0) + 2, max(label_w, 10) + 4)
    out = []
    if md == "wide" and len(pairs) >= 4:                  # the short pairs in two columns, the long ones below in full width
        half = (width - 3) // 2
        cells = [lead + m.ljust(lw) + v for m, (_k, v) in zip(marks, pairs)]
        short = [i for i, c in enumerate(cells) if visible_len(c) <= half]
        if len(short) >= 4:
            n = (len(short) + 1) // 2
            out = [ln.rstrip() for ln in columns2([cells[i] for i in short[:n]], [cells[i] for i in short[n:]], half, "   ")]
            pairs = [p for i, p in enumerate(pairs) if i not in short]
            marks = [m for i, m in enumerate(marks) if i not in short]
    for mark, (_k, value) in zip(marks, pairs):
        if md == "compact" or len(lead) + lw > width * 0.45:
            out.append(lead + mark)
            out += wrap(value, width, lead + "  ") if value else []
        else:
            out += wrap(value, width, lead + mark.ljust(lw), " " * (len(lead) + lw)) if value else [lead + mark]
    return out


def label_rows(lines: list, lead: str = "  ", label_w: int = 13, width: Optional[int] = None) -> list:
    """Lines 'name   text' (result tables of the net / disk test) re-laid out for the width: the text is wrapped under
    its column; in compact mode the name has its own line. Without a terminal the lines are returned unchanged."""
    if not adaptive() and width is None:
        return list(lines)
    pairs = []
    for line in lines:
        head, _, rest = line.strip().partition(" ")
        pairs.append((head, rest.strip()))
    return kv_block(pairs, width, lead=lead, label_w=label_w, colon=False)


def table(columns: list, rows: list, width: Optional[int] = None, md: Optional[str] = None, lead: str = "",
          gap: int = 2) -> list:
    """A text table that fits `width`. `columns` = [(title, priority, align)] with priority 0 = always shown, a bigger number =
    dropped earlier when the width is short ('<' or '>' = alignment). Rows are lists of strings. The first column is shortened
    with an ellipsis if even the important columns do not fit. Compact mode prints every row as a small card
    (first column, then 'title value' pieces) instead of columns."""
    width = width or avail()
    md = md or mode()
    if md == "compact":
        out = []
        for row in rows:
            out.append(lead + str(row[0]))
            bits = [f"{columns[i][0]} {row[i]}" for i in range(1, len(columns)) if str(row[i]) not in ("", "-")]
            out += wrap(" · ".join(bits), width, lead + "  ") if bits else []
        return out
    keep = list(range(len(columns)))

    def widths(ix):
        return {i: max(visible_len(columns[i][0]), *(visible_len(str(r[i])) for r in rows)) if rows else visible_len(columns[i][0])
                for i in ix}

    def total(ix):
        w = widths(ix)
        return len(lead) + sum(w.values()) + gap * (len(ix) - 1)

    while total(keep) > width:
        drop = [i for i in keep if columns[i][1] > 0]
        if not drop:
            break
        keep.remove(max(drop, key=lambda i: (columns[i][1], i)))
    w = widths(keep)
    excess = total(keep) - width
    if excess > 0:                                        # shorten the first column (names)
        w[keep[0]] = max(8, w[keep[0]] - excess)
    def cell(i, text):
        text = fit(str(text), w[i])
        pad = " " * max(0, w[i] - visible_len(text))
        return pad + text if columns[i][2] == ">" else text + pad
    head = lead + (" " * gap).join(cell(i, columns[i][0]) for i in keep)
    out = [head.rstrip(), lead + "─" * min(width - len(lead), visible_len(head) - len(lead))]
    out += [(lead + (" " * gap).join(cell(i, r[i]) for i in keep)).rstrip() for r in rows]
    return out


# --- submenus and questions -------------------------------------------------------
def _frame_row(content: str, on: bool, width: int) -> str:
    room = width - 4
    content = fit(safe_cells(content), room)
    return paint("┃", GREY, on) + " " + content + " " * max(0, room - visible_len(content)) + " " + paint("┃", GREY, on)


def panel(title: str, entries: list, on: bool, back: Optional[str] = None, width: Optional[int] = None,
          columns: Optional[int] = None) -> list:
    """A framed list of choices: entries are (key, label, hint); `back` = label of the 0 entry.
    The look follows the width: compact = the hint under the label, normal = hints beside the labels (wrapped under
    them), wide = two columns of choices when there are enough of them."""
    if width is None and columns is None and not adaptive():
        lines = [_rule("┏", "┓", on), _row(f" {logo(on)} {paint(title, BOLD, on)}", on), _rule("┣", "┫", on)]
        width = max((len(label) for _, label, _ in entries), default=0)
        for key, label, hint in entries:
            lines.append(_row(f" {paint(key, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} "
                              f"{paint(label.ljust(width), BOLD, on)}  {paint(_clip(hint, WIDTH - 12 - width), GREY, on)}".rstrip(), on))
        if back:
            lines.append(_row(f" {paint('0', BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {paint(back, BOLD, on)}", on))
        lines.append(_rule("┗", "┛", on))
        return lines
    columns = columns or cols()
    md = mode(columns)
    width = width or panel_width(columns)
    room = width - 4
    title_rows = wrap(title, room - 6)
    lines = [_rule("┏", "┓", on, width=width),
             _frame_row(f"{logo(on)} {paint(title_rows[0], BOLD, on)}", on, width)]
    lines += [_frame_row("      " + paint(part, BOLD, on), on, width) for part in title_rows[1:]]
    lines.append(_rule("┣", "┫", on, width=width))
    lw = min(max((visible_len(label) for _, label, _ in entries), default=0), max(8, room // 3))

    def entry_lines(i: int, space: int, stacked: bool) -> list:
        """The lines of one choice in `space` cells: hint beside the label, wrapped under it, or (compact) under the label."""
        key, label, hint = entries[i]
        hint = hint.strip()
        head = f"{paint(key, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} "
        name = paint(fit(label, max(6, space - 4)).ljust(0 if stacked else lw), BOLD, on)
        if not hint:
            return [head + name]
        own = visible_len(label) if stacked else lw                  # compact: the label is not padded to the column
        if 4 + own + 2 + visible_len(hint) <= space:
            return [head + name + "  " + paint(hint, GREY, on)]
        if not stacked and 4 + lw + 2 + 12 <= space * 0.6:
            parts = wrap(hint, space - 4 - lw - 2)
            return [head + name + "  " + paint(parts[0], GREY, on)] + [" " * (4 + lw + 2) + paint(x, GREY, on) for x in parts[1:]]
        return [head + name] + ["    " + paint(x, GREY, on) for x in wrap(hint, space - 4)]

    half = (room - 3) // 2
    if md == "wide" and len(entries) >= 5:                # two columns of choices, each column stacked
        n = (len(entries) + 1) // 2
        cols2 = [[ln for i in rng for ln in entry_lines(i, half, False)] for rng in (range(n), range(n, len(entries)))]
        body = [ln.rstrip() for ln in columns2(cols2[0], cols2[1], half, paint(" │ ", GREY, on))]
    else:
        body = [ln for i in range(len(entries)) for ln in entry_lines(i, room, md == "compact")]
    lines += [_frame_row(b, on, width) for b in body]
    if back:
        lines.append(_frame_row(f"{paint('0', BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {paint(back, BOLD, on)}", on, width))
    lines.append(_rule("┗", "┛", on, width=width))
    return lines


def menu_box(version: str, status: list, sections: list, keys: list, on: bool, width: int = WIDTH,
             title: str = "K3S·STRESS", compact: bool = False) -> list:
    """The main menu as one frame: header with the repo address above the version, status rows, sections of
    (key, icon, name, hint) items and a row of keys. Everything is cut to the terminal width."""
    room = width - 4

    def row(text: str = "") -> str:
        text = fit(safe_cells(text), room)
        return paint("┃", GREY, on) + " " + text + " " * max(0, room - visible_len(text)) + " " + paint("┃", GREY, on)

    head = f"{logo(on)} {paint(title, BOLD, on)}"
    ver = paint(f"v{version}", GREY, on)
    lines = [_rule("┏", "┓", on, tag=REPO_URL, width=width),
             row(head + " " * max(1, room - visible_len(head) - visible_len(ver)) + ver)]
    lines += [row(item) for item in status]
    name_w = max((len(n) for _l, items in sections for _k, _i, n, _h in items), default=4)
    for label, items in sections:
        lines.append(_rule("┣", "┫", on, width=width))
        lines.append(row(paint(label, GREY, on)))
        for key, icon, name, hint in items:
            hot = RED if name == "FULL" else BOLD
            tail = "" if compact else f"  {paint(hint, GREY, on)}"
            lines.append(row(f"{paint(key, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {icon} "
                             f"{paint(name.ljust(name_w), hot, on)}{tail}"))
    lines.append(_rule("┣", "┫", on, width=width))
    lines.append(row("   ".join(f"{paint(k, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {paint(n, BOLD, on)}"
                                for k, n in keys)))
    lines.append(_rule("┗", "┛", on, width=width))
    return lines


def show_choices(title: str, entries: list, back: Optional[str] = None) -> None:
    on = color_enabled()
    print("\n" + "\n".join(panel(title, entries, on, back)))


def prompt(label: str, default: Optional[str] = None) -> str:
    """Text for input(): `stress@k3s:~$ label [default] ▸ ` (readline-safe escapes when coloured). When the question does
    not fit one line of the terminal it is printed above, wrapped, and only the short prompt is left for input()."""
    on = color_enabled()
    suffix = f" [{default}]" if default is not None else ""
    c = term_cols()
    if c is not None and visible_len(f"stress@k3s:~$ {label}{suffix} ▸ ") > c - 1:
        for part in wrap(label, c - 1, "  ", "  "):
            print(paint(part, GREY, on))
        label = ""
        if visible_len(f"stress@k3s:~$ {suffix.strip()} ▸ ") > c - 1:
            suffix = f" [{fit(str(default), max(4, c - 24))}]"
    text = f"{prompt_text(on)}{' ' + label if label else ''}{suffix} {paint('▸', GREY, on)} "
    return re.sub(r"(\x1b\[[0-9;]*m)", "\x01\\1\x02", text) if on else text


def _marked(mark: str, code: str, text: str) -> None:
    on = color_enabled()
    c = term_cols()
    lines = wrap(text, c - 1, f"  {mark} ", "    ") if c is not None else [f"  {mark} {text}"]
    for i, line in enumerate(lines):
        print(line.replace(mark, paint(mark, code, on), 1) if i == 0 else line)


def warn(text: str) -> None:
    _marked("!", RED, text)


def info(text: str) -> None:
    _marked("›", GREEN, text)


# --- styling of ordinary output lines (terminal only, logs stay plain) ------------------------------
_RULE = re.compile(r"^([=-])\1{9,}$")
_TITLE = re.compile(r"^[A-Z][A-Z0-9 ·/()-]{3,}$")
_ROW = re.compile(r"^(\s*)([A-Za-z][^:\s]*(?: [^:\s]+){0,3}):(\s+)(\S.*)$")
_TEMP = re.compile(r"(?<![\w.])(\d{2,3})(\s?°C)")
_BAD = re.compile(r"\b(SUSPECTED|ERROR|FAILED|OVERHEAT\w*|STOPPED|TIMEOUT)\b")
_GOOD = re.compile(r"\b(OK|Ready)\b")
_SPARK = "▁▂▃▄▅▆▇█"


def temp_code(temp: int) -> str:
    return GREEN if temp < 65 else YELLOW if temp < 80 else RED


def _colorize_value(text: str, on: bool = True) -> str:
    text = _TEMP.sub(lambda m: paint(m.group(0), temp_code(int(m.group(1))), on), text)
    text = _BAD.sub(lambda m: paint(m.group(0), RED, on), text)
    return _GOOD.sub(lambda m: paint(m.group(0), GREEN, on), text)


def style_line(line: str) -> str:
    """Colours one plain output line for the terminal; without a terminal / with NO_COLOR it is returned as is."""
    if not color_enabled():
        return line
    stripped = line.strip()
    rule = _RULE.match(stripped)
    if rule:
        return paint(("━" if rule.group(1) == "=" else "─") * len(stripped), GREY, True)
    if _TITLE.match(stripped):
        return f"{logo(True)} {paint(stripped, BOLD, True)}"
    if stripped.startswith(("⚠️", "⚠")):
        return paint(line, YELLOW, True)
    if stripped.startswith(("✅", "✔")):
        return paint(line, GREEN, True)
    if stripped.startswith(("❌", "🛑", "🔥")):
        return paint(line, RED, True)
    if stripped in ("Notes:", "Node logs:") or stripped.startswith(("Stages", "Temperatures per stage", "Hottest")):
        return paint(line, BOLD + ";" + YELLOW, True)
    if stripped.startswith("- "):
        return line.replace("- ", paint("▸ ", YELLOW, True), 1).replace(stripped[2:], _colorize_value(stripped[2:]), 1)
    row = _ROW.match(line)
    if row:
        lead, label, gap, value = row.groups()
        return f"{lead}{paint(label + ':', GREY, True)}{gap}{_colorize_value(value)}"
    return _colorize_value(line)


_PLAIN_RULE = re.compile(r"^(\s*)([=\-])\2{9,}\s*$")
_KV = re.compile(r"^(\s*)([^\s:=|\[][^:|]{0,40}?):( {2,})(\S.*)$")
_BULLET = re.compile(r"^(\s*)([-•▸*] |\d+[.)] )(\S.*)$")
_PREFIX = re.compile(r"^(\s*)((?:[^\x00-\x7f]\S*|\[[\d:/]+\])\s{1,2})(?=\S)")


def adapt_line(line: str, room: int, md: str) -> list:
    """One plain output line laid out for `room` cells: rules are shortened, a line that is too long is wrapped at word
    boundaries with a hanging indent (labels stay in front: 'Label:  value' keeps the value under the value, in compact
    mode the value goes under the label). URLs / paths are never split - a word wider than the line is cut with an
    ellipsis on a line of its own."""
    rule_ = _PLAIN_RULE.match(line)
    if rule_:
        return [rule_.group(1) + rule_.group(2) * max(1, min(len(line.strip()), room - len(rule_.group(1))))]
    if visible_len(line) <= room or not line.strip():
        return [line]
    kv = _KV.match(line)
    if kv:
        lead, label, gap, value = kv.groups()
        col = len(lead) + len(label) + 1 + len(gap)
        if md == "compact" or col > room * 0.45:
            return [f"{lead}{label}:"] + wrap(value, room, lead + "  ")
        return wrap(value, room, line[:col], " " * col)
    bullet = _BULLET.match(line)
    if bullet:
        lead, mark, rest = bullet.groups()
        return wrap(rest, room, lead + mark, " " * (len(lead) + len(mark)))
    prefix = _PREFIX.match(line)
    if prefix:
        hang = " " * visible_len(prefix.group(0))
        return wrap(line[prefix.end():], room, prefix.group(0), hang if visible_len(hang) < room // 2 else prefix.group(1) + "  ")
    lead = line[:len(line) - len(line.lstrip())]
    if len(lead) > room // 3:                                    # deep hand-made indentation: not worth the space
        lead = " " * min(len(lead), 4)
    return wrap(line.strip(), room, lead, lead)


def adapt_lines(lines: list, columns: Optional[int] = None) -> list:
    """`adapt_line` for a block of lines; in a wide terminal runs of short 'Label:  value' rows are put in two columns."""
    columns = columns or cols()
    room, md = max(MIN_COLS, columns) - 1, mode(columns)
    out, run = [], []

    def flush() -> None:
        nonlocal run
        if md == "wide" and len(run) >= 4:
            half = (room - 3) // 2
            if all(visible_len(r) <= half for r in run):
                n = (len(run) + 1) // 2
                out.extend(ln.rstrip() for ln in columns2(run[:n], run[n:], half, "   "))
                run = []
                return
        if md == "compact" and run and any(visible_len(r) > room for r in run):      # the whole block: label above value
            for r in run:
                lead, label, _gap, value = _KV.match(r).groups()
                out.extend([f"{lead}{label}:"] + wrap(value, room, lead + "  "))
        else:
            for r in run:
                out.extend(adapt_line(r, room, md))
        run = []

    half = (room - 3) // 2
    for line in lines:
        kv = _KV.match(line)
        if kv and ((md == "wide" and visible_len(line) <= half) or md == "compact"):
            run.append(line)
            continue
        flush()
        out.extend(adapt_line(line, room, md))
    flush()
    return out


def emit(line: str = "") -> None:
    """print() for ordinary output lines, in the menu's style. With a terminal (or STRESS_TEST_COLUMNS) the lines are laid
    out for its width first (rules shortened, long lines wrapped, nothing is left to the terminal to wrap)."""
    parts = line.split("\n")
    columns = term_cols()
    if columns is not None:
        parts = adapt_lines(parts, columns)
        room = max(MIN_COLS, columns) - 1
        print("\n".join(fit(style_line(part), room) for part in parts))
        return
    print("\n".join(style_line(part) for part in parts))


def spark(value: Optional[float], top: float = 100.0) -> str:
    """One bar character for a value 0..top (used in tables)."""
    if value is None:
        return "·"
    return _SPARK[max(0, min(len(_SPARK) - 1, int(value / top * (len(_SPARK) - 1) + 0.5)))]


# --- live screens (redrawn in place, nothing scrolls) -----------------------------------------------
_PERCENT = re.compile(r"(\d+) %\)")
_CPU = re.compile(r"CPU: ([\d?]+%?)")
_RAM = re.compile(r"RAM: ([^|]+?)\s*\|")
_TEMPS = re.compile(r"Temp: (.+?)\s*\| Clock")
_CLOCK = re.compile(r"Clock: ([\d?]+ MHz)")
_POWER = re.compile(r"Power: ([\d.]+ W)")
_PING = re.compile(r"Ping: ([^|]+)")
_GPU = re.compile(r"GPU: (\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+thr=(0x[0-9a-fA-F]+)(?:\s+fan=(\d+)%)?")


def term_width(stream, limit: int = WIDTH) -> int:
    """Usable width of a frame on `stream`: the terminal (or STRESS_TEST_COLUMNS) minus one column, at most `limit`."""
    cols = _env_cols()
    if cols is None:
        try:
            cols = os.get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            cols = limit + 1
    return max(MIN_COLS, min(limit, cols - 1))


def box(title: str, sections: list, on: bool, width: int = WIDTH, tag: str = REPO_URL) -> list:
    """A framed block: title row, then the sections (lists of row contents) separated by lines."""
    room = width - 4

    def row(text: str = "") -> str:
        text = fit(safe_cells(text), room)
        return paint("┃", GREY, on) + " " + text + " " * max(0, room - visible_len(text)) + " " + paint("┃", GREY, on)

    lines = [_rule("┏", "┓", on, tag=tag, width=width), row(f"{logo(on)} {paint(title, BOLD, on)}")]
    for section in sections:
        lines.append(_rule("┣", "┫", on, width=width))
        lines.extend(row(item) for item in section)
    lines.append(_rule("┗", "┛", on, width=width))
    return lines


def menu_box_wide(version: str, status: list, left: list, right: list, keys: list, on: bool, width: int,
                  title: str = "K3S·STRESS") -> list:
    """The menu of a wide terminal: status rows on top, then two columns (left | right), then the keys row."""
    room = width - 4
    col = (room - 3) // 2

    def row(text: str = "") -> str:
        text = fit(safe_cells(text), room)
        return paint("┃", GREY, on) + " " + text + " " * max(0, room - visible_len(text)) + " " + paint("┃", GREY, on)

    head = f"{logo(on)} {paint(title, BOLD, on)}"
    ver = paint(f"v{version}", GREY, on)
    lines = [_rule("┏", "┓", on, tag=REPO_URL, width=width),
             row(head + " " * max(1, room - visible_len(head) - visible_len(ver)) + ver)]
    lines += [row(item) for item in status]
    lines.append(_rule("┣", "┫", on, width=width))
    sep = paint(" │ ", GREY, on)
    for i in range(max(len(left), len(right))):
        a = fit(left[i], col) if i < len(left) else ""
        b = fit(right[i], col) if i < len(right) else ""
        lines.append(row(a + " " * max(0, col - visible_len(a)) + sep + b))
    lines.append(_rule("┣", "┫", on, width=width))
    lines.append(row("   ".join(f"{paint(k, BOLD + ';' + YELLOW, on)} {paint('▸', GREY, on)} {paint(n, BOLD, on)}"
                                for k, n in keys)))
    lines.append(_rule("┗", "┛", on, width=width))
    return lines


def progress_bar(percent: float, length: int, on: bool) -> str:
    done = max(0, min(length, round(length * percent / 100)))
    return paint("█" * done, GREEN, on) + paint("░" * (length - done), GREY, on)


def phase_bar(phase: str, fraction: float, length: int, on: bool) -> str:
    """One bar for every phase of a test: "wait" (waiting for the other nodes, yellow), "test" (green bar),
    "cool" (snowflakes fill the bar while the node cools down), "done" (full)."""
    fraction = max(0.0, min(1.0, fraction))
    if phase == "cool":
        done = round(length * fraction)
        return paint("❆" * done, CYAN, on) + paint("·" * (length - done), GREY, on)
    done = round(length * fraction)
    if phase == "wait":
        return paint("▰" * done, YELLOW, on) + paint("▱" * (length - done), GREY, on)
    if phase == "done":
        return paint("█" * length, GREEN, on)
    return paint("█" * done, GREEN, on) + paint("░" * (length - done), GREY, on)


def clock(seconds: float) -> str:
    """Seconds as m:ss or h:mm:ss."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def metric(icon: str, label: str, value: str, on: bool, code: Optional[str] = None) -> str:
    return f"{icon} {paint(label, GREY, on)} " + (paint(value, code, on) if code else value)


def draw_block(stream, lines: list, previous_height: int) -> int:
    """Writes `lines` over the previous block of the same kind (cursor up, line by line); returns the new height."""
    move = f"\x1b[{previous_height}A" if previous_height else ""
    stream.write(move + "".join(f"\x1b[2K{line}\n" for line in lines))
    stream.flush()
    return len(lines)


class Canvas:
    """A block of lines redrawn in place. When the terminal is resized the old block may have been reflowed, cut or
    kept as it was - terminals differ and nothing reliable can be said about where it is now - so after a resize the
    screen is cleared and the block is drawn again from the top (like a full-screen program does)."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.widths: list = []
        self._size = None

    def _term_size(self) -> tuple:
        try:
            size = os.get_terminal_size(self.stream.fileno())
            return max(1, size.columns), max(1, size.lines)
        except (OSError, ValueError, AttributeError):
            return 10 ** 6, 10 ** 6

    def draw(self, lines: list) -> None:
        size = self._term_size()
        if self.widths and self._size is not None and size != self._size:
            move = "\x1b[2J\x1b[H"                      # resized: clear the screen, start at the top
            self.widths = []
        else:
            move = f"\x1b[{len(self.widths)}A\x1b[J" if self.widths else ""
        self._size = size
        self.stream.write(move + "".join(f"\x1b[2K{line}\n" for line in lines))
        self.stream.flush()
        self.widths = [visible_len(line) for line in lines]

    def reset(self) -> None:
        """The block stays on the screen, the next one is written below it."""
        self.widths = []

    @property
    def height(self) -> int:
        return len(self.widths)


class LiveScreen:
    """The framed block of a running single-node test: bar (time driven, redrawn every second),
    latest measurement, last events. Phases: the load (green bar) and the cooldown (snowflakes)."""

    EVENTS = 8

    def __init__(self, title: str, total: float = 0, stream=None) -> None:
        self.title = title
        self.subtitle = ""                      # one line under the title (CPU model and cores, GPU card ...)
        self.total = total                      # seconds of the load (0 = unknown)
        self.stream = stream or sys.stdout
        self.on = color_enabled()
        self.stage = "starting"
        self.measure = ""
        self.events: list = []
        self.phase = "test"
        self.prep = None                        # callable -> (fraction, step text, "k/N") while the node is being prepared
        self.cool_total = 0.0
        self._t0 = time.monotonic()
        self._cool_t0 = 0.0
        self._canvas = Canvas(self.stream)
        self._last = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._full = False
        self._ticker = None

    @staticmethod
    def wanted() -> bool:
        return sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb") \
            and not os.environ.get("STRESS_NO_LIVE")

    # --- feeding ---
    def event(self, line: str) -> None:
        with self._lock:
            for part in line.split("\n"):
                if part.strip("-= "):
                    self.events.append(part)
            del self.events[:-self.EVENTS]
            if "▶" in line:
                self.stage = line.split("]", 1)[-1].strip().lstrip("▶ ").strip()
        self.draw()

    def status(self, line: str) -> None:
        """The 'Remaining …' line of the monitor - the bar is driven by time, so nothing to take from it."""
        self.draw()

    def reading(self, line: str, cooldown: bool = False) -> None:
        with self._lock:
            self.measure = line
        self.draw()

    def set_subtitle(self, text: str) -> None:
        with self._lock:
            self.subtitle = text
        self.draw(force=True)

    def begin_prep(self, source) -> None:
        with self._lock:
            self.phase, self.prep, self.stage = "prep", source, "preparing the node"
        self.draw(force=True)

    def begin_test(self) -> None:
        with self._lock:
            self.phase, self._t0, self.stage = "test", time.monotonic(), "starting"
        self.draw(force=True)

    def begin_cooldown(self, seconds: float) -> None:
        with self._lock:
            self.phase, self.cool_total, self._cool_t0 = "cool", float(seconds), time.monotonic()
            self.stage = "cooldown - the load has ended, still measuring"
        self.draw(force=True)

    @property
    def _height(self) -> int:
        return self._canvas.height

    def start_ticker(self) -> None:
        """Redraws every second, so that the bar and the time run smoothly between the measurements."""
        def tick() -> None:
            while not self._stop.wait(1.0):
                self.draw()
        self._ticker = threading.Thread(target=tick, daemon=True)
        self._ticker.start()

    # --- drawing ---
    def _metrics(self) -> list:
        on, line = self.on, self.measure
        if not line:
            return [paint("waiting for the first reading…", GREY, on)]
        out = []
        for regex, icon, label in ((_CPU, ICON_CPU, "CPU"), (_RAM, ICON_RAM, "RAM")):
            found = regex.search(line)
            if found:
                out.append(metric(icon, label, found.group(1), on))
        temps = _TEMPS.search(line)
        if temps:
            value = _colorize_value(temps.group(1).replace("OVERHEATING!", "OVERHEATING"), on)
            out.append(metric(ICON_TEMP, "TEMP", value, on))
        for regex, icon, label in ((_CLOCK, ICON_CLOCK, "CLOCK"), (_POWER, ICON_POWER, "POWER"), (_PING, ICON_PING, "PING")):
            found = regex.search(line)
            if found:
                out.append(metric(icon, label, found.group(1).strip(), on))
        gpu = _GPU.search(line)
        if gpu:                                   # GPU test: two more rows - temperature, clock / memory, load, power, throttling
            temp, power, clock, util, mem, thr, _fan = gpu.groups()
            from .gpu import throttle_names
            bad = throttle_names(int(thr, 16), only_bad=True)
            hot = "GPU HOT" in line
            shown = lambda v, unit="": "N/A" if v == "?" else v + unit            # noqa: E731
            row1 = [metric(ICON_GPU, "GPU", paint(shown(temp), YELLOW, on) if hot else shown(temp), on),
                    metric(ICON_CLOCK, "GPU CLOCK", shown(clock), on)]
            row2 = [metric(ICON_RAM, "VRAM", shown(mem), on), metric(ICON_CPU, "GPU LOAD", shown(util), on),
                    metric(ICON_POWER, "GPU POWER", shown(power), on)]
            if bad:
                row2.append(metric(ICON_FAIL, "THROTTLE", paint(",".join(bad), YELLOW, on), on))
            base = ["   ".join(out[:2]), "   ".join(out[2:])] if len(out) > 2 else ["   ".join(out)]
            return base + ["   ".join(row1), "   ".join(row2)]
        return ["   ".join(out[:2]), "   ".join(out[2:])] if len(out) > 2 else ["   ".join(out)]

    def _bar_row(self, width: int) -> str:
        now, on = time.monotonic(), self.on
        if self.phase == "prep" and self.prep:
            fraction, text, steps = self.prep()
            bar = phase_bar("wait", fraction, max(10, min(40, width - 50)), on)
            return f"{bar} {min(100, int(fraction * 100)):>3} %  {ICON_TIME} {paint(f'prep {steps} · {text}', GREY, on)}"
        if self.phase == "cool":
            total, elapsed, label = self.cool_total, now - self._cool_t0, "cooldown"
        else:
            total, elapsed, label = self.total, now - self._t0, "test"
        fraction = 1.0 if self._full else elapsed / total if total > 0 else 0.0
        bar = phase_bar(self.phase, fraction, max(10, min(40, width - 40)), on)
        left = "0:00" if self._full else clock(total - elapsed) if total > 0 else "?"
        return f"{bar} {min(100, int(fraction * 100)):>3} %  {ICON_TIME} {paint(f'{label} · {left} left', GREY, on)}"

    def _event_rows(self) -> int:
        """How many event rows fit: the frame (13 rows without events) must stay lower than the terminal height."""
        try:
            rows = os.get_terminal_size(self.stream.fileno()).lines
        except (OSError, ValueError, AttributeError):
            return self.EVENTS
        return max(2, min(self.EVENTS, rows - 16))

    def _lines(self) -> list:
        on, width = self.on, term_width(self.stream, 100)
        status = [(ICON_COOL if self.phase == "cool" else ICON_READY if self.phase == "prep" else ICON_STAGE) + " "
                  + paint(self.stage, CYAN if self.phase == "cool" else YELLOW, on), self._bar_row(width)]
        keep = self._event_rows()
        shown = self.events[-keep:]
        events = [style_line(line) for line in shown] + [""] * (keep - len(shown))
        head = [[paint(self.subtitle, GREY, on)]] if self.subtitle else []
        return box(f"{ICON_NODE} {self.title}", [*head, status, self._metrics(), events], on, width)

    def draw(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < 0.15:
                return
            self._last = now
            self._canvas.draw(self._lines())

    def close(self) -> None:
        """Leaves the last frame on the screen; further output continues below it."""
        self._stop.set()
        with self._lock:
            if self._canvas.height:
                self._full = True
                self.draw(force=True)
            self._canvas.reset()


def gauge(fraction: float, length: int, on: bool, code: Optional[str] = None) -> str:
    """`▕████░░░░▏` - a bar with a scale; `code` = colour of the filled part."""
    fraction = max(0.0, min(1.0, fraction))
    done = round(length * fraction)
    return paint("▕", GREY, on) + paint("█" * done, code, on) + paint("░" * (length - done), GREY, on) + paint("▏", GREY, on)


def _gpu_num(text: str) -> Optional[float]:
    match = re.match(r"[\d.]+", text or "")
    return float(match.group(0)) if match else None


class GpuScreen(LiveScreen):
    """Live frame of a GPU test: the card in the title, bars for temperature (with the stop / warning marks), clock,
    VRAM and load, throttling and power, the host (CPU / RAM / CPU temperature) on one row, the last events."""

    EVENTS = 5
    HISTORY = 24

    def __init__(self, node: str, total: float = 0, stream=None, limit: int = 80, warn: int = 75, board=None) -> None:
        super().__init__(node, total, stream)
        self.board = board                          # GpuBoard of a multi-node test (None = one node)
        self.node, self.limit, self.warn = node, limit, warn
        self.card: dict = {}
        self.history: list = []
        self.gpu: Optional[tuple] = None          # (temp, power, clock, util, mem, thr) of the newest reading
        self.perf = ""

    # --- feeding ---
    def set_card(self, lines: list) -> None:
        """The static card data (`Label: value` lines of the GPU INFO) - model, driver, memory, maximum clock..."""
        with self._lock:
            for line in lines:
                key, _, value = line.partition(":")
                self.card[key.strip()] = value.strip()
        self.draw(force=True)

    def set_perf(self, text: str) -> None:
        with self._lock:
            self.perf = text
        self.draw()

    def reading(self, line: str, cooldown: bool = False) -> None:
        found = _GPU.search(line)
        with self._lock:
            self.measure = line
            if found:
                self.gpu = found.groups()
                temp = _gpu_num(self.gpu[0])
                if temp is not None:
                    self.history = (self.history + [temp])[-self.HISTORY:]
                if self.board is not None:
                    self.board.live(self.node, temp=temp, clock=_gpu_num(self.gpu[2]),
                                    card=self.card.get("GPU", ""), vram=self.gpu[4])
        self.draw()

    # --- drawing ---
    def _subtitle(self) -> str:
        c = self.card
        parts = []
        if c.get("Memory"):
            mib = _gpu_num(c["Memory"])
            parts.append(f"{mib / 1024:g} GB" if mib and mib >= 1024 else c["Memory"])
        if c.get("Driver"):
            parts.append(f"driver {c['Driver']}")
        if c.get("Compute capability") and c["Compute capability"] != "N/A":
            parts.append(f"CC {c['Compute capability']}")
        if c.get("PCIe (max)") and c["PCIe (max)"] != "N/A":
            width = c.get("PCIe width", "")
            parts.append(f"PCIe {c['PCIe (max)']}" + (f" {width}" if width and width != "N/A" else ""))
        return " · ".join(parts)

    def _gpu_rows(self, width: int) -> list:
        on = self.on
        if not self.gpu:
            return [paint("waiting for the first GPU reading…", GREY, on)]
        temp_s, power_s, clock_s, util_s, mem_s, thr_s, fan_s = self.gpu
        compact = width < 60
        room = width - 4
        shown = lambda v: "N/A" if v == "?" else v                                  # noqa: E731
        temp, clock, util = _gpu_num(temp_s), _gpu_num(clock_s), _gpu_num(util_s)
        used, _, total = mem_s.replace("MiB", "").partition("/")
        used_n = _gpu_num(used)
        total_n = _gpu_num(total) or _gpu_num(self.card.get("Memory", ""))
        max_clock = _gpu_num(self.card.get("Max SM clock", ""))

        def bar_len(hint: str) -> int:
            return max(6, min(28, room - 30 - 24))      # the same length on every row (24 cells are kept for the hint)

        def row(icon: str, label: str, value: str, fraction: Optional[float], code, hint: str = "", extra: str = "") -> str:
            hint = "" if compact else hint
            head = f"{icon} {paint(f'{label:<9}', GREY, on)} {value:<16}"
            if fraction is None:
                return head
            return f"{head} {gauge(fraction, 8 if compact else bar_len(hint), on, code)} {paint(hint, GREY, on)}{extra}"

        hot = temp is not None and temp >= self.limit
        warm = temp is not None and temp >= self.warn
        tcode = RED if hot else YELLOW if warm else GREEN
        spark_text = ""
        if not compact and self.history:
            top = max(self.limit, max(self.history))
            spark_text = "  " + paint("".join(spark(v - 20, top - 20) for v in self.history), tcode, on)
        rows = [row(ICON_TEMP, "GPU TEMP", paint(f"{temp:.0f} °C", tcode, on) + " " * max(0, 16 - len(f"{temp:.0f} °C"))
                    if temp is not None else "N/A", (temp or 0) / max(self.limit, 1), tcode,
                    f"stop {self.limit} · warn {self.warn}", spark_text)]
        rows.append(row(ICON_CLOCK, "GPU CLOCK", f"{clock:.0f} MHz" if clock is not None else "N/A",
                        (clock / max_clock) if clock is not None and max_clock else None, GREEN,
                        f"max {max_clock:.0f}" if max_clock else ""))
        pct = (used_n / total_n) if used_n is not None and total_n else None
        rows.append(row(ICON_RAM, "VRAM", f"{used_n:.0f} / {total_n:.0f} MiB" if used_n is not None and total_n
                        else shown(mem_s), pct, CYAN, f"{pct * 100:.0f} %" if pct is not None else ""))
        rows.append(row(ICON_CPU, "GPU LOAD", f"{util:.0f} %" if util is not None else "N/A",
                        (util / 100) if util is not None else None, GREEN))
        from .gpu import throttle_names
        bad = throttle_names(int(thr_s, 16), only_bad=True)
        power = ("N/A (not reported)" if power_s == "?" else power_s.replace("W", " W"))
        limit_w = self.card.get("Power limit", "N/A")
        if power_s != "?" and limit_w not in ("N/A", ""):
            power += f" / {limit_w}"
        throttle = paint(",".join(bad), YELLOW, on) if bad else paint("none", GREEN, on)
        rows.append(f"{ICON_POWER} {paint('POWER', GREY, on)} {power}   {ICON_COOL} {paint('THROTTLE', GREY, on)} {throttle}"
                    + (f"   {ICON_FAN} {paint('GPU FAN', GREY, on)} {fan_s} %" if fan_s else ""))
        if self.perf:
            rows.append(f"🚀 {paint('PERF', GREY, on)} {self.perf}")
        return rows

    def _host_row(self) -> str:
        on, line = self.on, self.measure
        out = [paint(f"{ICON_NODE} HOST", GREY, on)]
        for regex, icon, label in ((_CPU, ICON_CPU, "CPU"), (_RAM, ICON_RAM, "RAM")):
            found = regex.search(line)
            if found:
                value = found.group(1).split(" (")[-1].strip(")") if label == "RAM" and "(" in found.group(1) else found.group(1)
                out.append(f"{icon} {paint(label, GREY, on)} {value}")
        temps = _TEMPS.search(line)
        if temps:
            out.append(f"{ICON_TEMP} {paint('CPU', GREY, on)} " + _colorize_value(temps.group(1).replace("CPU: ", ""), on))
        found = _CLOCK.search(line)
        if found:
            out.append(f"{ICON_CLOCK} {found.group(1)}")
        return "   ".join(out)

    def _event_rows(self) -> int:
        try:
            rows = os.get_terminal_size(self.stream.fileno()).lines
        except (OSError, ValueError, AttributeError):
            return self.EVENTS
        return max(2, min(self.EVENTS, rows - 22))

    def _lines(self) -> list:
        on, width = self.on, term_width(self.stream, 100)
        status = [(ICON_COOL if self.phase == "cool" else ICON_READY if self.phase == "prep" else ICON_STAGE) + " "
                  + paint(self.stage, CYAN if self.phase == "cool" else YELLOW, on), self._bar_row(width)]
        keep = self._event_rows()
        shown = self.events[-keep:]
        events = [style_line(line) for line in shown] + [""] * (keep - len(shown))
        name = self.card.get("GPU")
        title = f"{ICON_GPU} {name} · {ICON_NODE} {self.node}" if name else f"{ICON_GPU} {self.node}"
        head = [[paint(self._subtitle(), GREY, on)]] if self._subtitle() else []
        host = [[self._host_row()]] if self.measure else []
        if self.board is not None:                  # several nodes: the table of all of them on top of the frame
            fraction = 1.0 if self._full else (time.monotonic() - self._t0) / self.total if self.total > 0 else 0.0
            if self.phase == "test":
                self.board.live(self.node, progress=min(1.0, max(0.0, fraction)))
            head = [self.board.lines(on, width), *head]
        return box(title, [*head, status, self._gpu_rows(width), *host, events], on, width)


class MatrixScreen:
    """Live frame of the network matrix (--net-matrix): bar of the pairs, the grid fills in as the pairs finish."""

    EVENTS = 4

    def __init__(self, names: list, net_time: int, stream=None) -> None:
        self.names = list(names)
        self.net_time = net_time
        self.stream = stream or sys.stdout
        self.on = color_enabled()
        self.cells: dict = {}                   # (client index, server index) -> (text, colour code)
        self.total = len(names) * (len(names) - 1)
        self.done = 0
        self.phase = "prep"
        self.text = "starting the helper pods"
        self.prep_fraction = 0.0
        self.events: list = []
        self._pair_t0 = 0.0
        self._running = None                    # (client index, server index) of the running pair
        self._canvas = Canvas(self.stream)
        self._last = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()

    wanted = staticmethod(LiveScreen.wanted)

    # --- feeding ---
    def event(self, line: str) -> None:
        with self._lock:
            for part in line.split("\n"):
                if part.strip():
                    self.events.append(part)
            del self.events[:-self.EVENTS]
        self.draw()

    def prep(self, ready: int, total: int, text: str) -> None:
        with self._lock:
            self.phase, self.prep_fraction, self.text = "prep", ready / max(total, 1), text
        self.draw(force=True)

    def begin_pair(self, index: int, total: int, client: str, server: str, capped: int = 0) -> None:
        with self._lock:
            self.phase, self.total, self._pair_t0 = "test", total, time.monotonic()
            self._running = (self.names.index(client), self.names.index(server))
            self.text = f"[{index}/{total}] {client} → {server}" + (f"  (capped at {capped} Mbit/s)" if capped else "")
        self.draw(force=True)

    def end_pair(self, pair) -> None:
        with self._lock:
            key = (self.names.index(pair.client), self.names.index(pair.server))
            if pair.mbps is None:
                self.cells[key] = ("x", RED)
            else:
                self.cells[key] = (f"{pair.mbps:.0f}" + ("*" if pair.capped else ""), None)
            self.done += 1
            self._running = None
        self.draw(force=True)

    def start_ticker(self) -> None:
        def tick() -> None:
            while not self._stop.wait(1.0):
                self.draw()
        threading.Thread(target=tick, daemon=True).start()

    # --- drawing ---
    def _fraction(self) -> float:
        if self.phase == "prep":
            return self.prep_fraction
        if self.phase == "done":
            return 1.0
        part = min(0.95, (time.monotonic() - self._pair_t0) / (self.net_time + 8)) if self._running else 0.0
        return (self.done + part) / max(self.total, 1)

    def _cell(self, i: int, j: int, top: float) -> str:
        on = self.on
        if i == j:
            return paint(f"{'-':>7}", GREY, on)
        if self._running == (i, j):
            return paint(f"{'…':>7}", YELLOW, on)
        if (i, j) not in self.cells:
            return paint(f"{'·':>7}", GREY, on)
        text, code = self.cells[(i, j)]
        if code is None:
            value = float(text.rstrip("*"))
            code = GREEN if value >= 0.7 * top else YELLOW if value >= 0.4 * top else RED
            if text.endswith("*"):
                code = GREY
        return paint(f"{text:>7}", code, on)

    def _lines(self) -> list:
        on, width = self.on, term_width(self.stream, 112)
        fraction = self._fraction()
        length = max(10, min(40, width - 44))
        if self.phase == "prep":
            left = f"{ICON_TIME} preparing the helper pods"
            bar = phase_bar("wait", fraction, length, on)
        else:
            remaining = (self.total - self.done) * (self.net_time + 8)
            left = f"{ICON_TIME} ~{clock(remaining)} left" if self.phase == "test" else f"{ICON_OK} finished"
            bar = phase_bar("done" if self.phase == "done" else "test", fraction, length, on)
        status = [(ICON_STAGE + " " + paint(self.text, YELLOW, on)) if self.phase == "test" else
                  (ICON_OK + " all pairs measured" if self.phase == "done" else ICON_READY + " " + paint(self.text, YELLOW, on)),
                  f"{bar} {min(100, int(fraction * 100)):>3} %  {paint(left, GREY, on)}  "
                  + paint(f"pairs {self.done}/{self.total}", GREY, on)]
        top = max((float(t.rstrip("*")) for t, c in self.cells.values() if c is None), default=1.0)
        label = min(22, max((len(n) for n in self.names), default=4))
        grid = [" " * (label + 6) + "".join(paint(f"{f'[{j}]':>7}", GREY, on) for j in range(1, len(self.names) + 1))]
        for i, name in enumerate(self.names):
            grid.append(f"{ICON_NODE} {paint(f'[{i + 1}]', GREY, on)} " + paint(fit(name, label) + " " * max(0, label - len(name)), BOLD, on)
                        + "".join(self._cell(i, j, top) for j in range(len(self.names))))
        grid.append(paint("Mbit/s · row = client, column = server · * = capped on purpose", GREY, on))
        shown = self.events[-self.EVENTS:]
        events = [style_line(line) for line in shown] + [""] * (self.EVENTS - len(shown))
        return box(f"{ICON_PING} NETWORK MATRIX · {len(self.names)} nodes · {self.net_time} s per pair",
                   [status, grid, events], on, width)

    def draw(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < 0.15:
                return
            self._last = now
            self._canvas.draw(self._lines())

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self.phase = "done"
            if self._canvas.height:
                self.draw(force=True)
            self._canvas.reset()
