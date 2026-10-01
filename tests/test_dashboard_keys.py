"""Dashboard: sort (o), filter (/), show (f), view (v), graphs (g), pod log (l), screen (e), help (?), Tab in every width."""
import io
import re

import pytest

from stress_test import dashboard as d, ui, watch
import test_dashboard as td
import test_watch as tw

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def mixed():
    v = td.cluster()
    a, b, c, m = v.nodes
    a.temp_c = None
    a.temps["CPU"], a.cpu_pct, a.mem_used_mib = 45, 90.0, 4000
    b.temps["CPU"], b.cpu_pct, b.mem_used_mib = 82, 10.0, 12000
    c.temps["CPU"], c.cpu_pct, c.mem_used_mib = 60, 50.0, 20000
    m.temps["CPU"], m.cpu_pct, m.mem_used_mib = 70, 30.0, 8000
    c.ready = False
    for n in v.nodes:
        for i in range(50):
            n.h_cpu.append(20 + i % 60)
            n.h_temp.append(40 + i % 30)
            n.h_mem.append(30 + i % 10)
            n.h_net.append(1000 * i)
            n.h_gpu.append(50 + i % 10)
    return v


def names(nodes):
    return [n.name for n in nodes]


def plain(lines):
    return [ANSI.sub("", x) for x in lines]


def dash(tmp_path=None, monkeypatch=None, view=None):
    class Kube:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kw):
            self.calls.append(args)
            return "line one\n\x1b[31mred\x1b[0m line\nlast line"
    dsh = d.Dashboard(Kube(), None, io.StringIO(), lambda t: None)
    dsh.collector.view = view or mixed()
    return dsh


# ---------------- sort / filter / show ---------------------------------------------------------------------------------------

def test_sorts_put_the_biggest_first_and_unknown_last():
    v = mixed()
    v.nodes[1].temps["CPU"] = None
    v.nodes[1].temps.pop("CPU")
    v.nodes[1].temps.pop("NVMe")
    assert names(d.shown_nodes(v, d.UiState(sort=1)))[-1] == "worker-2"                        # temp, unknown last
    assert names(d.shown_nodes(mixed(), d.UiState(sort=2)))[0] == "worker-1"                     # cpu 90
    assert names(d.shown_nodes(mixed(), d.UiState(sort=3)))[0] == "worker-3"                        # ram 20000
    assert names(d.shown_nodes(mixed(), d.UiState(sort=0))) == names(mixed().nodes)                       # default = as listed


def test_filter_and_presets():
    v = mixed()
    assert names(d.shown_nodes(v, d.UiState(filter="WORKER"))) == ["worker-1", "worker-2", "worker-3"]      # not case sensitive
    assert d.shown_nodes(v, d.UiState(filter="zzz")) == []
    problems = names(d.shown_nodes(v, d.UiState(preset=1)))
    assert "worker-3" in problems and "worker-2" in problems                                     # NotReady, 82 °C, pods not running
    assert all(not n.master for n in d.shown_nodes(v, d.UiState(preset=2)))
    assert names(d.shown_nodes(v, d.UiState(preset=3))) == ["worker-1"]                                 # the one with a GPU
    assert names(d.shown_nodes(v, d.UiState(preset=2, filter="worker", sort=1))) == ["worker-2", "worker-3", "worker-1"]


def test_header_shows_what_is_changed_and_the_counts():
    out = "\n".join(plain(d.render(mixed(), d.UiState(sort=1, preset=2, filter="worker", view=1), 130, 30, False)))
    assert "sort: temp" in out and "show: workers" in out and "view: temps" in out and "filter: “worker”" in out and "3/4 nodes" in out
    assert "sort:" not in "\n".join(plain(d.render(mixed(), d.UiState(), 130, 30, False)))
    assert "no node matches" in "\n".join(plain(d.render(mixed(), d.UiState(filter="zzz"), 130, 30, False)))


# ---------------- keys ----------------------------------------------------------------------------------------------------------

def press(dsh, *keys):
    for k in keys:
        dsh.handle(k)


def test_keys_o_f_v_c_cycle_and_the_selected_node_stays_selected():
    dsh = dash()
    press(dsh, "down", "down")                                           # worker-3
    press(dsh, "o")
    shown = d.shown_nodes(dsh.collector.view, dsh.state)
    assert shown[dsh.state.selected].name == "worker-3" and dsh.state.sort == 1
    for _ in range(6):
        press(dsh, "o")
    assert dsh.state.sort == 1 and dsh.state.view == 0                   # six sorts, a full circle
    press(dsh, "v", "V")
    assert dsh.state.view == 2
    press(dsh, "f", "F")
    assert dsh.state.preset == 2
    press(dsh, "c")
    assert (dsh.state.sort, dsh.state.preset, dsh.state.view, dsh.state.filter) == (0, 0, 0, "")


def test_typing_a_filter_every_key_is_text_until_enter_or_esc():
    dsh = dash()
    press(dsh, "/")
    assert dsh.state.typing
    press(dsh, "w", "o", "r", "q", "o")                                  # q and o are text now, not commands
    assert dsh.state.filter == "worqo" and dsh.state.typing
    press(dsh, "\x7f", "\x7f")
    assert dsh.state.filter == "wor"
    press(dsh, "enter")
    assert not dsh.state.typing and dsh.state.filter == "wor" and len(d.shown_nodes(dsh.collector.view, dsh.state)) == 3
    press(dsh, "/", "x", "esc")
    assert dsh.state.filter == "" and not dsh.state.typing
    press(dsh, "/")
    for ch in "a" * 50:
        press(dsh, ch)
    assert len(dsh.state.filter) == 30                                   # a limit


def test_q_leaves_only_from_the_main_screen_and_closes_pages_first():
    dsh = dash()
    assert dsh.handle("g") and dsh.state.overlay == "graph"
    assert dsh.handle("q") and dsh.state.overlay == ""
    assert dsh.handle("l") and dsh.state.overlay == "pods"
    assert dsh.handle("enter") and dsh.state.pod_log
    assert dsh.handle("q") and dsh.state.overlay == "pods" and not dsh.state.pod_log
    assert dsh.handle("Q") and dsh.state.overlay == ""
    assert dsh.handle("?") and dsh.state.overlay == "help" and dsh.handle("x") and dsh.state.overlay == ""
    assert dsh.handle("q") is False


def test_g_and_l_need_a_selected_node():
    dsh = dash()
    dsh.state.filter = "zzz"
    press(dsh, "g", "l")
    assert dsh.state.overlay == ""


# ---------------- views ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("view", range(5))
@pytest.mark.parametrize("cols", [40, 60, 90, 120, 160, 220, 300])
def test_every_view_is_aligned_at_every_width(view, cols):
    out = plain(d.render(mixed(), d.UiState(view=view), cols, 45, False))
    assert len({ui.visible_len(x) for x in out}) == 1 and ui.visible_len(out[0]) <= max(cols, 30)


def test_views_show_their_own_columns():
    heads = {name: "\n".join(plain(d.render(mixed(), d.UiState(view=i), 200, 30, False))) for i, name in enumerate(d.VIEWS)}
    assert "CORES max" in heads["temps"] and "NVMe" in heads["temps"] and "THROT" in heads["temps"]
    assert "LINK" in heads["net"] and "LOST" in heads["net"]
    assert "DISKS" in heads["disks"] and "IO read/write" in heads["disks"] and "SWAP" in heads["disks"]
    assert "ALLOC" in heads["gpu"] and "FAN" in heads["gpu"] and "POWER" in heads["gpu"]
    assert "CORES max" not in heads["overview"]


# ---------------- pods and the log -----------------------------------------------------------------------------------------------

def test_pod_names_are_checked_before_they_reach_kubectl():
    assert d.pod_ref({"ns": "default", "name": "app-1.x"}) == ("default", "app-1.x")
    for bad in ({"ns": "default", "name": "-n"}, {"ns": "a b", "name": "x"}, {"ns": "default", "name": "UP"}, {"ns": "", "name": "x"},
                {"ns": "default", "name": "x;rm"}, {"ns": "default", "name": "../x"}, {}):
        assert d.pod_ref(bad) is None


def test_the_log_is_fetched_cleaned_and_rate_limited():
    dsh = dash()
    press(dsh, "l", "enter")
    nodes = d.shown_nodes(dsh.collector.view, dsh.state)
    dsh.draw()
    assert dsh.kube.calls and dsh.kube.calls[0][:3] == ("logs", "-n", "default") and dsh.kube.calls[0][3] == "app"
    assert dsh.state.log_lines == ["line one", "[31mred [0m line", "last line"]            # the escape code of the pod is made harmless
    assert not any("\x1b" in x for x in dsh.state.log_lines)
    dsh.draw()
    dsh.draw()
    assert len(dsh.kube.calls) == 1                                       # at most every 2 s
    press(dsh, "down")
    dsh.draw()
    assert len(dsh.kube.calls) == 2 and dsh.kube.calls[1][3] == "crash"   # another pod: read at once


def test_a_bad_pod_name_is_never_given_to_kubectl():
    dsh = dash()
    dsh.collector.view.nodes[0].pods[0]["name"] = "--all-namespaces"
    press(dsh, "l", "enter")
    dsh.draw()
    assert dsh.kube.calls == [] and "not a valid" in dsh.state.log_note


@pytest.mark.parametrize("cols", [30, 60, 100, 200])
def test_pods_and_log_pages_are_aligned(cols):
    st = d.UiState(overlay="pods")
    out = plain(d.render(mixed(), st, cols, 30, False))
    assert len({ui.visible_len(x) for x in out}) == 1
    assert cols < 100 or "CrashLoopBackOff" in "\n".join(out)
    st = d.UiState(overlay="pods", pod_log=True, pod_i=1, log_lines=["a" * 500, "\x1b[2Jcleared", "ok"])
    out = plain(d.render(mixed(), st, cols, 30, False))
    assert len({ui.visible_len(x) for x in out}) == 1 and not any("\x1b" in x for x in out)


# ---------------- Tab in every width, help ---------------------------------------------------------------------------------------

def test_tab_changes_the_content_in_every_tier():
    v = mixed()
    t1 = ["\n".join(plain(d.render(v, d.UiState(tab=i, detail_page=True), 80, 40, False))) for i in range(3)]
    assert "Tab: pods, events" in t1[0] and "· pods (Tab)" in t1[1] and "cluster events (Tab)" in t1[2]
    t2 = ["\n".join(plain(d.render(v, d.UiState(tab=i), 130, 40, False))) for i in range(3)]
    assert len(set(t2)) == 3
    t3 = ["\n".join(plain(d.render(v, d.UiState(tab=i), 200, 60, False))) for i in range(3)]
    assert "cluster events (warnings)" in t3[2] and t3[0] != t3[2]


@pytest.mark.parametrize("cols", [30, 60, 100, 200])
def test_help_page(cols):
    out = plain(d.render(mixed(), d.UiState(overlay="help"), cols, 40, False))
    assert len({ui.visible_len(x) for x in out}) == 1 and "keys" in out[1]
    if cols >= 100:
        text = "\n".join(out)
        for key in ("sort", "filter", "graphs", "SCREEN", "scr/", "pods of the selected node"):
            assert key in text


def test_keys_line_lists_the_new_keys():
    assert "o sort" in "\n".join(plain(d.render(mixed(), d.UiState(), 130, 30, False)))


# ---------------- the screen (key e) ------------------------------------------------------------------------------------------------

def test_e_saves_the_screen_exactly_as_drawn(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    monkeypatch.setenv("NO_COLOR", "")
    dsh = dash()
    dsh.draw()
    frame = list(dsh._frame)
    press(dsh, "e")
    files = sorted((tmp_path / "scr").iterdir())
    assert [f.suffix for f in files] == [".txt"] and files[0].name.startswith("dashboard-") and "x" in files[0].name        # a text file only
    txt = files[0].read_text(encoding="utf-8").splitlines()
    assert txt == [ANSI.sub("", x) for x in frame]
    assert "saved" in dsh.status and "scr/" not in dsh.status or "scr" in dsh.status
    assert (files[0].stat().st_mode & 0o077) == 0                         # private files
    dsh.draw()
    assert "screen saved" in "\n".join(plain(dsh._frame))                  # the message is on the screen for a while


def test_e_works_on_every_page_and_twice_in_a_second(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    dsh = dash()
    for page in ("g", "q", "l", "q", "?"):
        press(dsh, page)
        dsh.draw()
        if page in ("g", "l"):
            press(dsh, "e")
    press(dsh, "q") if dsh.state.overlay else None
    dsh.draw()
    press(dsh, "e", "e")
    assert len(list((tmp_path / "scr").iterdir())) >= 4                    # nothing was overwritten


def test_an_unwritable_screen_folder_is_reported_not_raised(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(blocker / "scr"))
    dsh = dash()
    dsh.draw()
    press(dsh, "e")
    assert "could not be saved" in dsh.status


def test_status_message_goes_away(monkeypatch):
    dsh = dash()
    dsh._say("hello", 0.0)
    dsh.draw()
    assert dsh.status == ""


def test_w_saves_its_screen_too(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "scr"))
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    tw._three(tmp_path)
    scr = watch.Screen(stream=io.StringIO(), keys=lambda t: None)
    scr.w.refresh()
    scr.draw()
    scr.handle("e")
    files = list((tmp_path / "scr").iterdir())
    assert [f.suffix for f in files] == [".txt"] and files[0].name.startswith("status-") and "saved" in scr.state.message
    scr.handle("E")
    assert len(list((tmp_path / "scr").iterdir())) == 2


def test_save_screen_function(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_SCREEN_DIR", str(tmp_path / "s"))
    paths = ui.save_screen(["\x1b[32mgreen\x1b[0m", "plain"], "../evil name", 60, 40)
    assert [p.suffix for p in paths] == [".txt"] and paths[0].parent == tmp_path / "s"
    assert paths[0].read_text() == "green\nplain\n" and "\x1b" not in paths[0].read_text() and "evil_name" in paths[0].name and "60x40" in paths[0].name
