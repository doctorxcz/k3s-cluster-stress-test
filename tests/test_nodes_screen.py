"""NODES screen (menu 8 ▸ list nodes): node bar, one node at a time, columns by width, scrolling by height, keys."""
import io
import re

import pytest

from stress_test import dashboard as d, nodes_screen as ns
import test_dashboard_keys as tk

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def shot(view, cols, rows, state=None):
    return [ANSI.sub("", x) for x in ns.render_nodes(view, state or ns.NodesState(), cols, rows, False)]


def screen(view=None):
    scr = ns.NodesScreen(tk.dash(view=view).kube, None, io.StringIO(), lambda t: None)
    scr.collector.view = view or tk.mixed()
    return scr


@pytest.mark.parametrize("cols", [20, 40, 59, 80, 119, 120, 150, 200, 229, 230, 300])
@pytest.mark.parametrize("rows", [8, 15, 30, 60, 100])
def test_every_size_fits_the_window(cols, rows):
    out = shot(tk.mixed(), cols, rows)
    assert out and all(len(line) <= max(30, cols) for line in out)


def test_selected_node_is_in_the_bar_and_filled_with_its_info():
    v = tk.mixed()
    st = ns.NodesState(name=v.nodes[1].name)
    text = "\n".join(shot(v, 200, 60, st))
    assert f"▶" in text and v.nodes[1].name in text and "hardware" in text and "Kubernetes" in text and "history" in text


def test_columns_by_width():
    v = tk.mixed()
    assert not any(" │ " in x for x in shot(v, 100, 80)[6:])
    assert any(" │ " in x for x in shot(v, 200, 80))
    wide = [x for x in shot(v, 260, 80) if x.count(" │ ") >= 2]
    assert wide


def test_low_window_scrolls_and_reports_it():
    v = tk.mixed()
    st = ns.NodesState()
    first = shot(v, 100, 20, st)
    assert any("rows 1-" in x for x in first)
    st.scroll = 10_000
    last = shot(v, 100, 20, st)
    assert st.scroll > 0 and first != last


def test_tall_window_has_no_scroll_note():
    assert not any("↕" in x for x in shot(tk.mixed(), 260, 200))


def test_empty_cluster_and_not_ready_node():
    assert any("waiting" in x for x in shot(d.ClusterView(), 80, 20))
    v = tk.mixed()
    bad = [n for n in v.nodes if not n.ready][0]
    text = "\n".join(shot(v, 120, 80, ns.NodesState(name=bad.name)))
    assert "NotReady" in text


def test_narrow_bar_shows_a_window_around_the_selected():
    v = tk.mixed()
    bar = ns.node_bar(v, 3, 30, False)
    assert len(bar) == 1 and v.nodes[3].name in bar[0]


def test_keys_switch_nodes_scroll_and_leave():
    scr = screen()
    names = [n.name for n in scr.collector.view.nodes]
    s = scr.nodes_state
    s.name = names[0]
    for key, want in (("tab", 1), ("down", 2), ("right", 3), ("tab", 0), ("btab", 3), ("up", 2), ("left", 1), ("end", 3), ("home", 0)):
        assert scr.handle(key)
        assert s.name == names[want]
    scr.handle("pgdn")
    assert s.scroll == 8
    scr.handle("tab")
    assert s.scroll == 0
    scr.handle("p")
    assert s.paused
    assert scr.handle("q") is False and scr.handle("esc") is False


def test_selection_follows_the_name_when_the_list_changes():
    v = tk.mixed()
    st = ns.NodesState(name=v.nodes[2].name)
    v.nodes.insert(0, v.nodes.pop(2))
    assert ns.node_index(v, st) == 0


def test_shift_tab_is_not_escape():
    import os
    r, w = os.pipe()
    os.write(w, b"\x1b[Z")
    assert d.read_key(r, 0.1) == "btab"


def test_draw_writes_a_frame():
    scr = screen()
    scr.draw()
    assert scr._frame and any("NODES" in ANSI.sub("", x) for x in scr._frame)
