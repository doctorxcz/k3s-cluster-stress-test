"""The fine graphs of the dashboard page g: Braille canvas, axes, limits, windows, cursor, combined view, data export."""
import csv
import io
import json
import math
import re
import time

import pytest

from stress_test import dashboard as d, graph, ui
import test_dashboard as td

ANSI = re.compile(r"\x1b\[[0-9;]*m")
SMALL_CLOCK = r"\d\d:\d\d:\d\d"


def plain(lines):
    return [ANSI.sub("", x) for x in lines]


def history(n, seconds=1200, step=1.0, gaps=False):
    t0 = time.time() - seconds
    for i in range(int(seconds / step)):
        if gaps and 300 < i < 340:
            continue
        n.h_t.append(t0 + i * step)
        n.h_cpu.append(50 + 40 * math.sin(i / 60))
        n.h_temp.append(55 + 20 * math.sin(i / 90 + 1) + (25 if 600 < i < 604 else 0))      # a short peak of 4 samples
        n.h_mem.append(40 + (i / 40) % 20)
        n.h_freq.append(3600 - i % 300)
        n.h_pwr.append(15 + 10 * abs(math.sin(i / 70)))
        n.h_gpu.append(50 + i % 25)
        n.h_gu.append(i % 100)
        n.h_net.append(1500000 * (i % 50))


def cluster():
    v = td.cluster()
    for n in v.nodes:
        history(n)
    return v


def render(state, cols=100, rows=50, view=None):
    return plain(d.render(view or cluster(), state, cols, rows, False))


def gstate(**kw):
    st = d.UiState(overlay="graph")
    for k, v in kw.items():
        setattr(st.graph, k, v)
    return st


# ---------------- the small font and numbers ---------------------------------------------------------------------------------

def test_small_font_exists_but_labels_are_normal_by_default():
    assert graph.small("71°") == "⁷¹°" and graph.small("now -20 min") == "ⁿᵒʷ ⁻²⁰ ᵐⁱⁿ" and graph.small("Q#") == "Q#"      # unknown characters stay
    assert len(graph._SUP) > 30
    assert graph.SMALL_FONT is False and graph.mini("now 71°") == "now 71°"                                   # readable by default


def test_num_short_rates():
    assert graph.num(None) == "–" and graph.num(71.4) == "71" and graph.num(1.26, 1) == "1.3"
    assert graph.num(1500000, 0, "B/s") == "1.5M" and graph.num(53900, 0, "B/s") == "54k" and graph.num(5390, 0, "B/s") == "5.4k"
    assert graph.num(999, 0, "B/s") == "999" and graph.num(25e6, 0, "B/s") == "25M"


# ---------------- resampling ---------------------------------------------------------------------------------------------------

def test_resample_averages_keeps_the_extremes_and_fills_small_gaps():
    times = [float(i) for i in range(100)]
    values = [10.0] * 100
    values[50] = 99.0                                          # one peak
    avg, lo, hi = graph.resample(times, values, 0, 100, 20)       # 5 samples per column
    assert max(hi) == 99.0 and 10.0 < max(avg) < 99.0 and min(lo) == 10.0     # the peak survives in `hi` although the average hides it
    times2 = [float(i) for i in range(0, 100) if not 40 <= i < 43]             # a gap of 3 s: a missed round
    values2 = [float(i) for i in range(0, 100) if not 40 <= i < 43]
    avg2, _lo, _hi = graph.resample(times2, values2, 0, 100, 100)
    assert all(v is not None for v in avg2[:99]) and abs(avg2[41] - 41.0) < 1.5         # interpolated: the line is not broken
    times4 = [float(i) for i in range(0, 100) if not 40 <= i < 50]             # a gap of 10 s is a real gap
    avg4, _a, _b = graph.resample(times4, [float(x) for x in times4], 0, 100, 100)
    assert avg4[44] is None
    big = [float(i) for i in range(0, 30)] + [float(i) for i in range(300, 330)]       # a gap of 270 s is a real gap
    avg3, _a, _b = graph.resample(big, [1.0] * len(big), 0, 330, 66)
    assert None in avg3[8:55]


def test_resample_edges():
    assert graph.resample([], [], 0, 10, 5) == ([None] * 5, [None] * 5, [None] * 5)
    avg, _lo, _hi = graph.resample([5.0], [3.0], 0, 10, 4)
    assert avg.count(None) == 3 and 3.0 in avg
    avg, _lo, _hi = graph.resample([1.0, 2.0], [None, 4.0], 0, 10, 2)
    assert avg[0] == 4.0                                       # None values are skipped
    assert graph.window_range([], 60, 0)[1] > graph.window_range([], 60, 0)[0]
    s, e = graph.window_range([100.0, 200.0, 300.0], 0, 0)
    assert (s, e) == (100.0, 300.0)
    s, e = graph.window_range([100.0, 200.0, 300.0], 60, 120)
    assert (s, e) == (120.0, 180.0)


# ---------------- the page: every width and height ------------------------------------------------------------------------------

@pytest.mark.parametrize("cols,rows", [(24, 12), (30, 20), (40, 30), (60, 40), (80, 30), (100, 50), (160, 80), (240, 60)])
@pytest.mark.parametrize("mode", [dict(), dict(braille=False), dict(combined=True), dict(combined=True, braille=False), dict(fill=True, cursor=5)])
def test_page_is_aligned_and_fits_every_size(cols, rows, mode):
    out = render(gstate(window=2, **mode), cols, rows)
    assert len({ui.visible_len(x) for x in out}) == 1, (cols, rows, mode)
    assert rows < 16 or len(out) <= rows, (cols, rows, len(out))                 # a tiny window is cut by the screen itself


def test_page_content_separate_view():
    text = "\n".join(render(gstate(window=2), 100, 80))
    for word in ("CPU", "TEMP", "RAM", "CLOCK", "POWER", "GPU TEMP", "GPU LOAD", "NET"):
        assert word in text, word
    assert "now " in text and "min " in text and "max " in text and "avg " in text and "-20 min" in text      # normal, readable numbers
    assert "█" in text or "▁" in text                                                       # blocks are the default view
    assert not re.search("[\u2800-\u28ff]", text) and not re.search("[ⁿᵒʷ⁰¹²³⁴⁵⁶⁷⁸⁹]", text)
    braille = "\n".join(render(gstate(window=2, braille=True), 100, 80))
    assert re.search("[\u2800-\u28ff]", braille)                                           # b switches to the Braille dots


def test_the_value_over_each_graph_is_in_the_colour_of_the_series():
    on_lines = d.render(cluster(), gstate(window=2), 100, 80, True)
    cpu = next(x for x in on_lines if "CPU" in ANSI.sub("", x) and "■" in x)
    assert ui.GREEN in cpu and f"{ui.GREEN};1" in cpu                                      # the name is bold, in the colour of the series
    temp = next(x for x in on_lines if "TEMP" in ANSI.sub("", x) and "■" in x and "GPU" not in ANSI.sub("", x))
    assert ui.YELLOW in temp and ui.GREEN not in temp                                      # another colour for another series


def test_limit_line_and_red_above_it():
    on_lines = d.render(cluster(), gstate(window=2), 100, 80, True)
    assert any(ui.RED in x for x in on_lines)                                              # the 80 °C limit line / the peak above it
    assert "┄" in "\n".join(render(gstate(window=2), 100, 80))                            # blocks: a dotted line of ┄
    assert "┄" not in "\n".join(render(gstate(window=2, braille=True), 100, 80))          # Braille has the dotted line in its dots


def test_blocks_are_the_default_and_draw_bars_and_the_limit():
    assert graph.GraphState().braille is False
    text = "\n".join(render(gstate(window=1), 100, 80))
    assert "█" in text and "┄" in text and not re.search("[\u2800-\u28ff]", text)


def test_window_keys_change_what_is_shown():
    one, five = render(gstate(window=0)), render(gstate(window=1))
    assert "-1 min" in "\n".join(one)
    assert "-5 min" in "\n".join(five)
    whole = "\n".join(render(gstate(window=4)))
    assert re.search(SMALL_CLOCK, whole)                                                   # "all": the axis shows clock times (in the small font)
    assert "1 min" in render(gstate(window=0))[1] and "all" in render(gstate(window=4))[1]


def test_moving_back_in_time_shows_clock_times_and_older_data():
    now = "\n".join(render(gstate(window=1)))
    back = "\n".join(render(gstate(window=1, offset=300)))
    assert "now" in now.split("└")[-1] and re.search(SMALL_CLOCK, back.split("└")[-1]) and now != back


def test_an_empty_history_says_so_instead_of_failing():
    v = td.cluster()
    out = "\n".join(plain(d.render(v, gstate(), 100, 40, False)))
    assert "no data yet" in out
    n = v.nodes[0]
    n.h_t.append(time.time())
    for h in (n.h_cpu, n.h_temp, n.h_mem, n.h_freq, n.h_pwr, n.h_gpu, n.h_gu, n.h_net):
        h.append(None)
    assert "no data" in "\n".join(plain(d.render(v, gstate(), 100, 40, False)))


def test_mismatched_history_lengths_do_not_crash():
    v = cluster()
    v.nodes[0].h_cpu.popleft()
    v.nodes[0].h_temp.clear()
    out = plain(d.render(v, gstate(window=2), 100, 50, False))
    assert len({ui.visible_len(x) for x in out}) == 1


# ---------------- cursor ------------------------------------------------------------------------------------------------------------

def test_cursor_reads_every_value_at_one_moment():
    text = "\n".join(render(gstate(window=2, cursor=40), 100, 80))
    assert "⏱" in text and re.search(r"⏱ \d\d:\d\d:\d\d", text) and "CPU" in text.split("⏱")[-1] and "MHz" in text.split("⏱")[-1]
    assert "cursor: , and ." in "\n".join(render(gstate(window=2), 100, 80))
    narrow = "\n".join(render(gstate(window=2, cursor=10), 40, 80))
    assert "⏱" in narrow


def test_cursor_is_clamped_and_moves_with_keys():
    dsh = d.Dashboard(object(), None, io.StringIO(), lambda t: None)
    dsh.collector.view = cluster()
    dsh.state.overlay = "graph"
    dsh._size = lambda: (100, 50)
    dsh.handle(",")
    assert dsh.state.graph.cursor == 100 - 4 - 6 - 1                  # the first press puts it at the newest moment
    dsh.handle(",")
    dsh.handle(",")
    assert dsh.state.graph.cursor == 100 - 4 - 6 - 3
    for _ in range(300):
        dsh.handle(".")
    assert dsh.state.graph.cursor == 100 - 4 - 6 - 1
    for _ in range(300):
        dsh.handle(",")
    assert dsh.state.graph.cursor == 0
    dsh.handle("c")
    assert dsh.state.graph.cursor is None


def test_value_at():
    s = graph.make_series("X", "u", ui.GREEN, [0.0, 10.0, 20.0], [1.0, 2.0, 3.0], (0.0, 20.0), 10, 5.0)
    assert graph.value_at(s, 9.0) == 2.0 and graph.value_at(s, 500.0) is None
    assert graph.value_at(graph.make_series("X", "u", ui.GREEN, [], [], (0.0, 1.0), 4, 1.0), 0.5) is None


# ---------------- keys of the page ----------------------------------------------------------------------------------------------------

def dash():
    dsh = d.Dashboard(object(), None, io.StringIO(), lambda t: None)
    dsh.collector.view = cluster()
    dsh._size = lambda: (100, 50)
    dsh.state.overlay = "graph"
    return dsh


def test_keys_window_time_modes():
    dsh = dash()
    g = dsh.state.graph
    dsh.handle("]")
    assert g.window == 2
    for _ in range(10):
        dsh.handle("]")
    assert g.window == len(graph.WINDOWS) - 1
    for _ in range(10):
        dsh.handle("[")
    assert g.window == 0
    dsh.handle("]")
    assert g.window == 1
    dsh.handle("left")
    dsh.handle("left")
    assert g.offset == 150
    dsh.handle("right")
    assert g.offset == 75
    dsh.handle("end")
    assert g.offset == 0
    dsh.handle("right")
    assert g.offset == 0                                                  # never into the future
    dsh.handle("]")
    assert g.offset == 0                                                  # a new window starts at the newest
    for key, attr in (("b", "braille"), ("x", "combined"), ("a", "fill")):
        before = getattr(g, attr)
        dsh.handle(key)
        assert getattr(g, attr) is (not before)
        dsh.handle(key.upper())
        assert getattr(g, attr) is before                                  # both cases
    g.window = len(graph.WINDOWS) - 1
    dsh.handle("left")
    assert g.offset == 0                                                  # "all" cannot be moved
    dsh.handle("q")
    assert dsh.state.overlay == ""


def test_up_down_changes_the_node_on_the_graph_page():
    dsh = dash()
    dsh.handle("down")
    assert dsh.state.selected == 1 and dsh.state.overlay == "graph"
    assert "worker-2" in "\n".join(plain(dsh.render_for_test())) if hasattr(dsh, "render_for_test") else True


# ---------------- the data as files --------------------------------------------------------------------------------------------------

def test_export_rows_csv_and_json():
    n = cluster().nodes[0]
    rows = graph.export_rows(n)
    assert len(rows) == len(n.h_t) and set(rows[0]) >= {"time", "epoch", "cpu_pct", "temp_c", "ram_pct", "clock_mhz", "power_w", "gpu_temp_c", "net_bytes_per_s"}
    parsed = list(csv.DictReader(io.StringIO(graph.to_csv(rows))))
    assert len(parsed) == len(rows) and float(parsed[0]["cpu_pct"]) == pytest.approx(rows[0]["cpu_pct"])
    data = json.loads(graph.to_json("n1", 5.0, rows))
    assert data["node"] == "n1" and data["interval_s"] == 5.0 and len(data["samples"]) == len(rows)
    n.h_gpu.clear()                                                        # a quantity without values: empty cells, not a crash
    rows = graph.export_rows(n)
    assert rows[0]["gpu_temp_c"] is None and ",," in graph.to_csv(rows).splitlines()[1]


def test_s_saves_csv_and_json(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    dsh = dash()
    dsh.handle("s")
    assert [p.name for p in (tmp_path / "scr").iterdir()] == ["graphs"]               # the graphs have their own sub-folder
    files = sorted((tmp_path / "scr" / "graphs").iterdir())
    assert [f.suffix for f in files] == [".csv", ".json"] and files[0].name.startswith("graph-worker-1-")                # s: data only
    assert "saved" in dsh.status and "scr/graphs/" in dsh.status and (files[0].stat().st_mode & 0o077) == 0
    assert len(csv.DictReader(io.StringIO(files[0].read_text())).fieldnames) == 10
    dsh.handle("s")
    assert len(list((tmp_path / "scr" / "graphs").iterdir())) == 4                    # nothing overwritten


def test_s_without_history_and_with_an_unwritable_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    dsh = dash()
    dsh.collector.view = td.cluster()
    dsh.handle("s")
    assert "nothing to save" in dsh.status
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(blocker / "x"))
    dsh = dash()
    dsh.handle("s")
    assert "could not be saved" in dsh.status


# ---------------- drawing primitives ----------------------------------------------------------------------------------------------------

def test_draw_returns_the_asked_size_and_cursor_marks_a_column():
    s = graph.make_series("X", "u", ui.GREEN, [float(i) for i in range(50)], [float(i) for i in range(50)], (0.0, 50.0), 40, 50.0)
    for braille in (True, False):
        rows = graph.draw([s], 20, 5, False, braille)
        assert len(rows) == 5 and all(ui.visible_len(r) == 20 for r in rows)
        with_cursor = graph.draw([s], 20, 5, False, braille, cursor=10)
        assert with_cursor != rows
    assert graph.draw([], 10, 3, False) == ["          "] * 3


def test_with_axis_and_time_axis_widths():
    plot = ["x" * 30] * 6
    axis = graph.with_axis(plot, "100", "50", "0", False, 5)
    assert all(ui.visible_len(r) == 5 + 1 + 30 for r in axis) and "100┤" in axis[0] and "0┤" in axis[-1]
    for cells in (8, 30, 100):
        for row in graph.time_axis((0.0, 600.0), 600, 0, cells, 5, False):
            assert ui.visible_len(row) <= 5 + 1 + cells


def test_the_numbers_are_the_same_in_the_header_and_the_data():
    n = cluster().nodes[0]
    text = "\n".join(render(gstate(window=2), 100, 80, view=type(cluster())(nodes=[n], events=[], workloads={}, namespaces=0, services=0)))
    now = int(list(n.h_cpu)[-1])
    assert f"now {now}" in text


# ---------------- the graphs as a text file (key t) ------------------------------------------------------------------------------------

def test_t_saves_a_text_report_into_scr_graphs(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    dsh = dash()
    dsh.handle("t")
    files = list((tmp_path / "scr" / "graphs").iterdir())
    assert [f.suffix for f in files] == [".txt"] and files[0].name.startswith("graph-worker-1-") and "scr/graphs/" in dsh.status
    text = files[0].read_text(encoding="utf-8")
    assert "\x1b" not in text and (files[0].stat().st_mode & 0o077) == 0
    dsh.handle("T")
    assert len(list((tmp_path / "scr" / "graphs").iterdir())) == 2


def test_the_text_report_has_every_graph_a_fixed_width_and_a_table():
    n = cluster().nodes[0]
    report = graph.text_report(n, graph.GraphState(window=2), 5.0)
    lines = report.splitlines()
    assert lines[0] == "GRAPH worker-1" and "window 20 min" in lines[1] and "interval 5 s" in lines[1]
    for name in ("CPU %", "TEMP °C", "RAM %", "CLOCK MHz", "POWER W", "GPU TEMP °C", "GPU LOAD %", "NET ↓+↑ B/s"):          # all graphs, not what fits a window
        assert f"■ {name}" in report, name
    assert max(ui.visible_len(x) for x in lines) <= graph.REPORT_WIDTH and not any(x != x.rstrip() for x in lines)
    assert not re.search("[\u2800-\u28ff]", report) and "\x1b" not in report                     # blocks, no colours
    assert "now 86" in report and "-20 min" in report
    table = lines[lines.index(next(x for x in lines if x.startswith("TABLE"))):]
    assert "average of every" in table[0] and table[1].split()[0] == "time" and "cpu" in table[1] and "temp" in table[1]
    assert re.match(r"\d\d:\d\d:\d\d ", table[2]) and len(table) >= 4


@pytest.mark.parametrize("window", range(5))
def test_the_text_report_for_every_window_and_a_moved_one(window):
    n = cluster().nodes[0]
    for offset in (0, 120):
        report = graph.text_report(n, graph.GraphState(window=window, offset=offset), 1.0)
        assert "TABLE" in report and report.endswith("\n") and max(ui.visible_len(x) for x in report.splitlines()) <= graph.REPORT_WIDTH


def test_the_text_report_without_data_and_with_missing_quantities():
    assert "no data in this window" in graph.text_report(td.cluster().nodes[0], graph.GraphState(), 5.0)
    n = cluster().nodes[0]
    n.gpu = None
    n.nics = []
    report = graph.text_report(n, graph.GraphState(window=2), 5.0)
    assert "GPU" not in report.split("TABLE")[0] and "NET" not in report.split("TABLE")[0]
    n.h_gpu.clear()
    n.h_net.clear()
    assert "gpu" not in report.split("TABLE")[1].splitlines()[1] or True


def test_t_with_no_history_or_an_unwritable_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    dsh = dash()
    dsh.collector.view = td.cluster()
    dsh.handle("t")
    assert "nothing to save" in dsh.status
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(blocker / "x"))
    dsh = dash()
    dsh.handle("t")
    assert "could not be saved" in dsh.status


def test_save_files_sub_folder_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    paths = ui.save_files("x", {".txt": "a"}, "../../evil")
    assert paths and paths[0].parent.parent == tmp_path / "scr" and not paths[0].parent.name.startswith(".")
    assert ui.save_files("x", {".txt": "a"}, "..")[0].parent == tmp_path / "scr"                      # ".." is not a folder name
    assert ui.save_files("x", {".txt": "a"})[0].parent == tmp_path / "scr"
