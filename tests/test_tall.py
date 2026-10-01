"""The height is the second axis of the layout (2026-10-01): a tall window opens more instead of scrolling - W, dashboard, menu, live frames."""
import io
import re

import pytest

from stress_test import dashboard as d, menu, ui, watch
import test_dashboard as td
import test_watch as tw

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(lines):
    return [ANSI.sub("", x) for x in lines]


# ---------------- height_mode / term_rows -----------------------------------------------------------------------------------

@pytest.mark.parametrize("rows,mode", [(None, "normal"), (10, "short"), (23, "short"), (24, "normal"), (39, "normal"), (40, "tall"), (300, "tall")])
def test_height_mode(rows, mode):
    assert ui.height_mode(rows) == mode


def test_term_rows_follows_the_override(monkeypatch):
    monkeypatch.setenv("STRESS_TEST_LINES", "123")
    assert ui.term_rows() == 123
    monkeypatch.setenv("STRESS_TEST_LINES", "x")
    assert ui.term_rows(io.StringIO()) is None


# ---------------- dashboard ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cols", [40, 60, 90, 120, 160, 200, 260])
def test_dashboard_tall_is_aligned_and_never_higher_than_the_window(cols):
    out = plain(d.render(td.cluster(), d.UiState(), cols, 100, False))
    assert len(out) <= 100 and len({ui.visible_len(x) for x in out}) == 1


def test_dashboard_tall_narrow_opens_node_cluster_problems_tests_and_the_other_nodes():
    short = "\n".join(plain(d.render(td.cluster(), d.UiState(), 60, 30, False)))
    tall = "\n".join(plain(d.render(td.cluster(), d.UiState(), 60, 160, False)))
    for word in ("pods ·", "capacity", "problems", "tests", "worker-2", "cluster events"):
        assert word in tall, word
    assert "capacity" not in short and "cluster events" not in short
    assert len(tall.splitlines()) > len(short.splitlines()) + 20


def test_dashboard_tall_grows_with_the_height_and_stops_when_there_is_nothing_more():
    heights = [len(d.render(td.cluster(), d.UiState(), 60, h, False)) for h in (40, 60, 80, 100, 400)]
    assert heights == sorted(heights) and heights[-1] == len(d.render(td.cluster(), d.UiState(), 60, 500, False))   # no empty stretching


def test_dashboard_short_and_normal_windows_are_unchanged():
    for rows in (20, 30, 39):
        out = plain(d.render(td.cluster(), d.UiState(), 60, rows, False))
        assert len(out) <= rows and "cluster events" not in "\n".join(out)


def test_dashboard_tall_wide_adds_the_other_nodes_below_the_panels():
    one = "\n".join(plain(d.render(td.cluster(), d.UiState(), 200, 60, False)))
    two = "\n".join(plain(d.render(td.cluster(), d.UiState(), 200, 200, False)))
    assert len(two.splitlines()) >= len(one.splitlines())


# ---------------- W ------------------------------------------------------------------------------------------------------------

def test_watch_tall_narrow_opens_every_card_and_every_node(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    tw._three(tmp_path)
    w = tw.watcher()
    st = watch.UiState(selected=1)
    normal = "\n".join(tw.lines_of(w, 44, 30, st))
    tall = "\n".join(tw.lines_of(w, 44, 90, watch.UiState(selected=1)))
    assert "VRAM" not in normal and "VRAM" in tall and "▸ w2" in tall and "history" in tall
    assert len(tall.splitlines()) > len(normal.splitlines())
    out = tw.lines_of(w, 44, 90)
    assert len({ui.visible_len(x) for x in out}) == 1 and len(out) <= 90
    assert not any(a.strip("┃ ") == b.strip("┃ ") and "──" in a for a, b in zip(out, out[1:]))        # no double rules


def test_watch_tall_wide_opens_the_other_tests_and_more_events(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    tw._three(tmp_path)
    w = tw.watcher()
    normal = "\n".join(tw.lines_of(w, 130, 30, watch.UiState(selected=0)))
    tall = "\n".join(tw.lines_of(w, 130, 90, watch.UiState(selected=0)))
    assert "▸ classic test" not in normal and "▸ classic test" in tall
    assert len(tall.splitlines()) <= 90


# ---------------- menu ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cols", [40, 59, 60, 80, 99])
def test_menu_tall_adds_tips_and_nodes_and_stays_aligned(cols, monkeypatch):
    monkeypatch.setattr(menu, "_NODES_CACHE", [("n1", True, True), ("n2", False, False)])
    short = plain(menu.menu_lines("c", [], False, cols=cols, rows=30))
    tall = plain(menu.menu_lines("c", [], False, cols=cols, rows=70))
    assert "TIPS" not in "\n".join(short) and "TIPS" in "\n".join(tall) and "NODES" in "\n".join(tall) and "NOT Ready" in "\n".join(tall)
    assert len({ui.visible_len(x) for x in tall}) == 1 and len(tall) > len(short)


def test_menu_wide_is_unchanged_by_the_height():
    assert menu.menu_lines("c", [], False, cols=130, rows=30) == menu.menu_lines("c", [], False, cols=130, rows=90)


# ---------------- live frames of a running test ------------------------------------------------------------------------------

def test_live_frame_shows_more_events_in_a_tall_window(monkeypatch):
    out = io.StringIO()
    monkeypatch.setenv("STRESS_TEST_LINES", "60")
    screen = ui.LiveScreen("n1", 60, stream=out)
    for i in range(40):
        screen.event(f"[10:00:{i:02d}] event number {i}")
    tall = len(screen._lines())
    monkeypatch.setenv("STRESS_TEST_LINES", "30")
    assert len(screen._lines()) < tall and tall <= 60
    monkeypatch.delenv("STRESS_TEST_LINES")
    assert len(screen._lines()) <= 13 + screen.EVENTS + 3                    # no terminal: the plain size, as before


def test_matrix_screen_shows_more_events_in_a_tall_window(monkeypatch):
    out = io.StringIO()
    screen = ui.MatrixScreen(["a", "b", "c"], 5, stream=out)
    for i in range(30):
        screen.event(f"[10:00:{i:02d}] pair {i}")
    monkeypatch.setenv("STRESS_TEST_LINES", "30")
    short = len(screen._lines())
    monkeypatch.setenv("STRESS_TEST_LINES", "70")
    assert len(screen._lines()) > short
    monkeypatch.delenv("STRESS_TEST_LINES")
    assert len(screen._lines()) == short                                      # no terminal: the plain size


def test_dashboard_narrow_tall_wraps_long_rows_instead_of_cutting_them():
    text = "\n".join(plain(d.render(td.cluster(), d.UiState(), 44, 120, False)))
    assert "hyper-threading" in text and "…" not in text.split("▸ pods")[0].split("NODE")[-1].split("┣")[-1]


# ---------------- the parallel table ----------------------------------------------------------------------------------------

def _plans(tmp, n=3):
    from types import SimpleNamespace
    plans = {}
    for i in range(n):
        name = f"node{i}"
        path = tmp / f"{name}.log"
        tw.write(path, tw.header(name, "classic") + [tw.sample(k, cpu=30 + k * 4, temp=50 + k) for k in range(10)]
                 + ["[10:01:00] ▶ Stage 2/4: 50 % (3 min)", "[10:01:05] ⚠️ something happened on " + name])
        plans[name] = (name, SimpleNamespace(max_temp=85), str(path), "")
    return plans


def test_parallel_tail_detail_reads_history_and_events(tmp_path):
    from stress_test import parallel
    cpu, temp, events = parallel.tail_detail(str(_plans(tmp_path, 1)["node0"][2]))
    assert len(cpu) == 10 and len(temp) == 10 and len(events) == 2 and "something happened" in events[-1]
    assert parallel.tail_detail(str(tmp_path / "missing.log")) == ([], [], [])


@pytest.mark.parametrize("cols", [40, 60, 112, 200])
def test_parallel_tall_blocks_fit_the_budget_and_the_width(tmp_path, cols):
    from stress_test import parallel
    plans = _plans(tmp_path, 4)
    for budget in (0, 5, 12, 100):
        blocks = parallel.tall_sections(plans, cols, False, budget)
        assert sum(len(b) + 1 for b in blocks) <= budget
        frame = parallel.build_frame("T", ["s"], ["row"], cols, False, blocks)
        assert len({ui.visible_len(x) for x in frame}) == 1
    assert len(parallel.tall_sections(plans, cols, False, 100)) == 4 and "node3" in "\n".join(b[0] for b in parallel.tall_sections(plans, cols, False, 100))


def test_parallel_table_without_extra_is_as_before():
    from stress_test import parallel
    assert parallel.build_frame("T", ["s"], ["r1", "r2"], 60, False) == parallel.build_frame("T", ["s"], ["r1", "r2"], 60, False, [])


# ---------------- keys work in both cases (2026-10-01) -----------------------------------------------------------------------

@pytest.mark.parametrize("key", ["w", "W", "d", "D", "t", "T", "r", "R", "s", "S", "q", "Q", "?"])
def test_menu_keys_work_in_both_cases(key, monkeypatch):
    ran = []
    monkeypatch.setattr(menu, "load_state", lambda: {})                       # nothing to repeat, whatever the real state file says
    monkeypatch.setattr(menu, "settings_screen", lambda ask, state: ran.append("settings"))
    monkeypatch.setattr(menu, "start_time_dialog", lambda ask: ran.append("time"))
    monkeypatch.setattr(menu, "show_help", lambda ask: ran.append("help"))
    answers = iter([key, "q"])
    menu.run_menu(ask=lambda p, d="": next(answers, "q"), run=lambda o: ran.append(tuple(o)) or 0, cluster=lambda: "c")
    expected = {"w": [("--status", "--live")], "d": [("--dashboard",)], "t": ["time"], "s": ["settings"], "?": ["help"], "q": [], "r": []}[key.lower()]
    assert ran == expected


@pytest.mark.parametrize("pair", [("j", "J"), ("k", "K"), ("p", "P"), ("x", "X"), ("q", "Q")])
def test_watch_keys_work_in_both_cases(pair, tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    tw._three(tmp_path)
    results = []
    for key in pair:
        scr = watch.Screen(stream=io.StringIO(), keys=lambda t: None)
        scr.w.refresh()
        scr.state.scroll = 3
        alive = scr.handle(key)
        results.append((alive, scr.state.scroll, scr.state.paused, scr.state.confirm_stop))
    assert results[0] == results[1]


@pytest.mark.parametrize("pair", [("p", "P"), ("r", "R"), ("q", "Q")])
def test_dashboard_keys_work_in_both_cases(pair):
    states = []
    for key in pair:
        dash = d.Dashboard.__new__(d.Dashboard)
        dash.state = d.UiState()
        dash._wake = type("E", (), {"set": lambda self: None, "clear": lambda self: None})()
        dash.status = ""
        dash.collector = type("C", (), {"refresh": lambda self: None})()
        try:
            alive = dash.handle(key)
        except Exception as exc:                                              # noqa: BLE001
            alive = type(exc).__name__
        states.append((alive, dash.state.paused))
    assert states[0] == states[1]
