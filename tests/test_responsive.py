"""Width modes of the menu (compact / normal / wide), emoji-safe frame rows and the resize-proof Canvas."""
import io
import os

import pytest

from stress_test import menu, ui
from stress_test.parallel import LiveInfo, frame_row


def _lines(cols, nodes=True):
    menu._NODES_CACHE[:] = [("dell-9020-sff-i7", False, True), ("hp-g2-celeron", False, False),
                            ("hp-prodesk-400-g6-master", True, True)] if nodes else []
    return menu.menu_lines("cluster: 3 nodes", ["🔁 last: ./stress.sh --self-test"], False, cols=cols,
                           temps={"dell-9020-sff-i7": 77})


def test_menu_mode_thresholds():
    assert [menu.menu_mode(c) for c in (40, 59, 60, 99, 100, 200)] == \
        ["compact", "compact", "normal", "normal", "wide", "wide"]


@pytest.mark.parametrize("cols", [40, 50, 59, 60, 76, 99, 100, 120, 140, 200])
def test_every_menu_row_has_the_same_width(cols):
    lines = _lines(cols)
    widths = {ui.visible_len(line) for line in lines}
    assert len(widths) == 1, (cols, widths)
    assert widths.pop() <= cols - 1 or cols < 41
    assert all("️" not in line for line in lines)            # no width-unreliable emoji in a frame


def test_compact_menu_has_no_hints_and_short_keys():
    text = "\n".join(_lines(50))
    assert "classic" not in text and "R ▸ rep" in text and "Q ▸ quit" in text


def test_normal_menu_is_the_known_layout():
    text = "\n".join(_lines(80))
    assert "TESTS" in text and "CLUSTER" in text and "classic · stepped · spike" in text and "│" not in text


def test_wide_menu_has_two_columns_nodes_and_temperatures():
    text = "\n".join(_lines(140))
    assert "│" in text and "NODES" in text and "77 °C" in text and "NOT Ready" in text
    assert "hp-prodesk-400-g6-master" in text and "master" in text
    assert "whole cluster" in text and "TIPS" in text and "quit (0 works too)" in text


def test_wide_menu_wraps_long_hints_instead_of_cutting_them():
    text = "\n".join(_lines(140))
    assert "…" not in text.split("NODES")[0].replace("… and", "")


def test_safe_cells_replaces_unreliable_emoji():
    assert ui.safe_cells("⚠️ a 🌡️ b 🖥️ c ⏱️ d") == "❗ a 🔥 b 💻 c ⏳ d"
    assert ui.safe_cells("x️y") == "xy"


def test_box_rows_do_not_contain_variation_selectors():
    lines = ui.box("t", [["⚠️ warning", "🌡️ CPU sensor found"]], False, 60)
    assert all("️" not in line for line in lines) and len({ui.visible_len(x) for x in lines}) == 1


@pytest.mark.parametrize("width", [45, 59, 69, 70, 96, 97, 107, 108, 112])
def test_frame_row_fits_its_frame_at_every_width(width):
    info = LiveInfo(cpu=100.0, temp=61, freq=3400, ram_pct=25.0, has_sample=True, stage=(2, 4, 50))
    row = frame_row("hp-elitedesk-705-g4-35w-a10", info, None, ("test", 0.5, "1:00"), "", False, width=width)
    assert ui.visible_len(row) <= width - 4, (width, ui.visible_len(row), row)
    assert "hp-elitedesk" in row or width < 70


class _TTY(io.StringIO):
    size = (100, 30)

    def isatty(self):
        return True

    def fileno(self):
        return 99


def _canvas(monkeypatch, stream):
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size(stream.size))
    return ui.Canvas(stream)


def test_canvas_redraws_in_place_when_the_size_is_unchanged(monkeypatch):
    out = _TTY()
    canvas = _canvas(monkeypatch, out)
    canvas.draw(["a", "b", "c"])
    canvas.draw(["a", "b", "d"])
    text = out.getvalue()
    assert "\x1b[3A\x1b[J" in text and "\x1b[2J" not in text
    assert canvas.height == 3


def test_canvas_clears_the_screen_after_a_resize(monkeypatch):
    out = _TTY()
    canvas = _canvas(monkeypatch, out)
    canvas.draw(["x" * 90, "y"])
    out.size = (50, 20)                                   # the window got narrower
    canvas.draw(["z" * 40])
    tail = out.getvalue().split("y\n", 1)[1]
    assert tail.startswith("\x1b[2J\x1b[H") and "\x1b[A" not in tail
    assert canvas.height == 1


def test_canvas_without_a_terminal_never_moves_the_cursor_wrongly():
    out = io.StringIO()
    canvas = ui.Canvas(out)
    canvas.draw(["a"])
    canvas.draw(["b"])
    assert out.getvalue().count("\x1b[1A") == 1 and "\x1b[2J" not in out.getvalue()


def test_canvas_reset_leaves_the_block_and_starts_a_new_one(monkeypatch):
    out = _TTY()
    canvas = _canvas(monkeypatch, out)
    canvas.draw(["a", "b"])
    canvas.reset()
    canvas.draw(["c"])
    assert out.getvalue().count("\x1b[2A") == 0 and canvas.height == 1
