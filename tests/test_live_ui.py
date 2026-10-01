"""ui.py: width-aware helpers, box(), phase_bar(), clock(), LiveScreen / MatrixScreen (plain rendering, no tty)."""
import io
import re

import pytest

from stress_test import ui
from stress_test.netmatrix import Pair

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_visible_len_counts_emoji_as_two_and_ignores_ansi():
    assert ui.visible_len("abc") == 3
    assert ui.visible_len("💻 x") == 4
    assert ui.visible_len("❄️") == 2                      # char + U+FE0F
    assert ui.visible_len("\x1b[38;5;79mabc\x1b[0m") == 3
    assert ui.visible_len(ui.paint("🔥 hot", ui.RED, True)) == 6
    assert ui.visible_len("") == 0


def test_fit_keeps_short_text_and_cuts_with_ellipsis():
    assert ui.fit("abc", 5) == "abc"
    cut = ui.fit("abcdefghij", 6)
    assert cut.endswith("…") and ui.visible_len(cut) <= 6
    emoji = ui.fit("💻💻💻💻💻💻", 7)
    assert ui.visible_len(emoji) <= 7 and emoji.endswith("…")


def test_fit_keeps_colour_codes_balanced():
    text = ui.paint("x" * 30, ui.GREEN, True)
    cut = ui.fit(text, 10)
    assert ui.visible_len(cut) <= 10 and cut.endswith("\x1b[0m")


def test_clock_formats():
    assert ui.clock(0) == "0:00" and ui.clock(65) == "1:05" and ui.clock(3600) == "1:00:00"
    assert ui.clock(-5) == "0:00" and ui.clock(3725.4) == "1:02:05"


@pytest.mark.parametrize("phase", ["wait", "test", "done"])
def test_phase_bar_length_and_clamp(phase):
    for frac in (-1, 0, 0.5, 1, 7):
        bar = ui.phase_bar(phase, frac, 20, False)
        assert ui.visible_len(bar) == 20, (phase, frac)


def test_phase_bar_cool_uses_snowflakes():
    bar = ui.phase_bar("cool", 0.5, 20, False)
    assert bar.count("❆") == 10 and ui.visible_len(bar) == 20            # one-cell snowflakes: the width is the same everywhere
    assert ui.phase_bar("cool", 1.0, 20, False).count("❆") == 20
    assert "❆" not in ui.phase_bar("cool", 0.0, 20, False)


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("width", [40, 76, 100])
def test_box_rows_all_have_equal_visible_width(on, width):
    sections = [["plain", "💻 node-1 🔥 52 °C", ui.paint("coloured 🧠 RAM 50 %", ui.GREEN, on)],
                [ui.phase_bar("cool", 0.4, 30, on) + " ⏳ 0:12", "x" * 200, ""]]
    lines = ui.box("💻 title ✅", sections, on, width)
    widths = {ui.visible_len(line) for line in lines}
    assert widths == {width}, widths
    assert ANSI.sub("", lines[0]).startswith("┏") and ANSI.sub("", lines[-1]).startswith("┗")


def test_box_tag_in_top_border_only_when_room():
    lines = ui.box("t", [["a"]], False, 76)
    assert ui.REPO_URL in lines[0]
    assert ui.REPO_URL not in ui.box("t", [["a"]], False, 30)[0]
    assert ui.visible_len(ui.box("t", [["a"]], False, 30)[0]) == 30


def test_header_has_repo_tag_and_equal_widths():
    lines = ui.header("K3S·STRESS", "1.2.3", "cluster", False)
    assert ui.REPO_URL in lines[0]
    assert {ui.visible_len(x) for x in lines} == {ui.WIDTH}


def _render(screen):
    return [ANSI.sub("", ln) for ln in screen._lines()]


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")


def test_livescreen_plain_frame_consistent(plain):
    s = ui.LiveScreen("fake-node", total=60, stream=io.StringIO())
    s.event("[12:00:00] ▶ Load 100 %")
    s.reading("CPU: 99% | RAM: 1200 MiB (20 %) | Temp: CPU: 61°C | Clock: 3000 MHz")
    lines = _render(s)
    assert len({ui.visible_len(x) for x in lines}) == 1
    text = "\n".join(lines)
    assert "fake-node" in text and "CPU 99%" in text and "TEMP" in text and "test · " in text


def test_livescreen_phases(plain):
    s = ui.LiveScreen("n", total=10, stream=io.StringIO())
    s.begin_prep(lambda: (0.5, "installing", "2/4"))
    assert "prep 2/4 · installing" in "\n".join(_render(s))
    s.begin_test()
    assert "test ·" in "\n".join(_render(s))
    s.begin_cooldown(30)
    text = "\n".join(_render(s))
    assert "cooldown" in text and "❄️" in text or "··" in text
    s.close()
    assert s._height == 0


def test_livescreen_draw_moves_cursor_up_on_second_draw(plain):
    out = io.StringIO()
    s = ui.LiveScreen("n", total=10, stream=out)
    s.draw(force=True)
    h = s._height
    s.draw(force=True)
    assert f"\x1b[{h}A" in out.getvalue()


def test_livescreen_keeps_only_last_events(plain):
    s = ui.LiveScreen("n", stream=io.StringIO())
    for i in range(20):
        s.event(f"line {i}")
    assert len(s.events) == ui.LiveScreen.EVENTS and s.events[-1] == "line 19"


def test_livescreen_wanted_respects_env(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("STRESS_NO_LIVE", raising=False)
    assert ui.LiveScreen.wanted() is True
    monkeypatch.setenv("STRESS_NO_LIVE", "1")
    assert ui.LiveScreen.wanted() is False


def test_livescreen_not_wanted_without_tty():
    assert ui.LiveScreen.wanted() is False          # pytest captures stdout


def test_matrixscreen_grid_and_widths(plain):
    names = ["node-a", "node-b", "a-very-long-node-name-that-needs-cutting"]
    m = ui.MatrixScreen(names, 5, stream=io.StringIO())
    lines = _render(m)
    assert len({ui.visible_len(x) for x in lines}) == 1
    m.begin_pair(1, 6, "node-a", "node-b")
    m.end_pair(Pair("node-a", "node-b", mbps=940.0))
    m.end_pair(Pair("node-b", "node-a", error="x"))
    m.end_pair(Pair("node-a", names[2], mbps=100.0, capped=100))
    lines = _render(m)
    text = "\n".join(lines)
    assert "940" in text and "x" in text and "100*" in text and "pairs 3/6" in text
    assert len({ui.visible_len(x) for x in lines}) == 1
    m.close()
    assert m.phase == "done"


def test_livescreen_shows_gpu_rows(plain):
    s = ui.LiveScreen("gpu-node", total=60, stream=io.StringIO())
    s.begin_test()
    s.reading("[12:00:00] CPU: 5%, RAM: 6397 MiB (27%) | Temp: CPU: 46°C | Clock: 3614 MHz | "
              "GPU: 67°C ? 1354MHz 100% 1593/2048MiB thr=0x20")
    lines = _render(s)
    text = "\n".join(lines)
    assert len({ui.visible_len(x) for x in lines}) == 1
    assert "GPU 67°C" in text and "GPU CLOCK 1354MHz" in text and "VRAM 1593/2048MiB" in text
    assert "GPU LOAD 100%" in text and "GPU POWER N/A" in text and "THROTTLE sw_thermal" in text


# ---------------- GPU screen, CPU subtitle ------------------------------------------------------------------

CARD = ["GPU: Quadro P620", "Driver: 580.178.04", "Memory: 2048 MiB", "Power limit: N/A", "Max SM clock: 1480 MHz",
        "PCIe (max): gen 3", "PCIe width: 16x", "Compute capability: 6.1"]


def _gpu_screen(temps=(40, 55, 67), thr="0x0", cols=100, monkeypatch=None):
    monkeypatch.setenv("STRESS_TEST_COLUMNS", str(cols))
    g = ui.GpuScreen("n1", total=120, stream=io.StringIO(), limit=80, warn=75)
    g.begin_test()
    g.set_card(CARD)
    for t in temps:
        g.reading(f"[12:00:00] CPU: 5%, RAM: 6397 MiB (27%) | Temp: CPU: 46°C | Clock: 3614 MHz | "
                  f"GPU: {t}°C ? 1354MHz 100% 1593/2048MiB thr={thr}")
    return g


@pytest.mark.parametrize("cols", [40, 59, 60, 100, 160])
def test_gpu_screen_every_width_is_aligned(plain, monkeypatch, cols):
    lines = _render(_gpu_screen(cols=cols, monkeypatch=monkeypatch))
    assert len({ui.visible_len(x) for x in lines}) == 1
    assert max(ui.visible_len(x) for x in lines) <= cols


def test_gpu_screen_shows_card_bars_and_host(plain, monkeypatch):
    text = "\n".join(_render(_gpu_screen(monkeypatch=monkeypatch)))
    assert "Quadro P620" in text and "n1" in text and "2 GB · driver 580.178.04" in text
    assert "GPU TEMP" in text and "67 °C" in text and "stop 80 · warn 75" in text and "▃" in text or "▅" in text
    assert "1354 MHz" in text and "max 1480" in text and "1593 / 2048 MiB" in text and "78 %" in text
    assert "GPU LOAD" in text and "N/A (not reported)" in text and "THROTTLE none" in text
    assert "HOST" in text and "CPU 5%" in text and "RAM 27%" in text


def test_gpu_screen_throttle_hot_and_perf(plain, monkeypatch):
    g = _gpu_screen(temps=(78,), thr="0x20", monkeypatch=monkeypatch)
    g.set_perf("1086 Gflop/s · errors 0")
    text = "\n".join(_render(g))
    assert "sw_thermal" in text and "PERF 1086 Gflop/s" in text and "78 °C" in text


def test_gpu_screen_waits_for_the_first_reading(plain, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "100")
    g = ui.GpuScreen("n1", total=60, stream=io.StringIO())
    assert "waiting for the first GPU reading" in "\n".join(_render(g))


def test_livescreen_subtitle_row(plain):
    s = ui.LiveScreen("n", total=10, stream=io.StringIO())
    s.set_subtitle("⚡ Intel Core i7-4790S · 4c/8t @ 3.2 GHz")
    lines = _render(s)
    assert len({ui.visible_len(x) for x in lines}) == 1 and "4c/8t" in "\n".join(lines)


def test_no_raw_escape_codes_without_color(plain):
    s = ui.LiveScreen("n", total=10, stream=io.StringIO())
    s.reading("CPU: 99% | RAM: 1200 MiB (20 %) | Temp: CPU: 61°C | Clock: 3000 MHz")
    assert "\x1b" not in "\n".join(s._lines())
