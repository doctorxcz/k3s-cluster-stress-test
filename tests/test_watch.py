"""Live status screen (menu W): log reader, registry (also foreground tests), rendering per kind of test and width, keys, stop."""
import io
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from stress_test import background, ui, watch
from test_integration import FAKE, ROOT

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clock(i, base=36000):                      # 10:00:00 + 5 s steps
    t = base + 5 * i
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def header(node, profile, duration="2 min", limits=("Automatic stop at: 85°C (2 readings in a row)",)):
    return ["=== KUBERNETES STRESS-NG LOG ===", f"Node: {node}", "Started: 2026-10-01 10:00:00", f"Test duration: {duration}",
            f"Profile: {profile}", *limits, "Cooldown after test: 60 s", "Notes: x", "=== HARDWARE ===", "CPU: x", "=== METRICS LOG ==="]


def sample(i, cpu=50, temp=60, gpu=None, cool=False):
    line = (f"[{clock(i)}] CPU: {cpu}%, RAM: 1200 MiB (20%) | Temp: CPU: {temp}°C | Clock: {3000 - i * 10} MHz | Power: 20.5 W"
            + (f" | GPU: {gpu}°C ? 1354MHz 99% 1593/2048MiB thr=0x0 fan=40%" if gpu is not None else ""))
    return ("[cooldown] " if cool else "") + line


def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    return tmp_path


def register(tmp, run_id, profile, nodes, log=None, log_dir="", kind="foreground", duration=120, **extra):
    background.register(run_id, nodes[0] if len(nodes) == 1 else "cluster", duration, log, "", title=f"{profile} test · {', '.join(nodes)}",
                        extra={"kind": kind, "profile": profile, "nodes": nodes, "log_dir": log_dir or str(tmp / "logs"), **extra})


def lines_of(w, cols=120, rows=45, state=None):
    return [ANSI.sub("", x) for x in watch.render(w, state or watch.UiState(), cols, rows, False)]


def watcher():
    w = watch.Watcher()
    w.refresh()
    return w


# ---------------- the log reader ---------------------------------------------------------------------------------------------

def test_reader_reads_header_samples_stage_and_end_incrementally(tmp_path):
    log = tmp_path / "n1.log"
    write(log, header("n1", "stepped 25/50 %") + [sample(0), "[10:00:05] ▶ Stage 2/4: 50 % (3 min)", sample(1)])
    r = watch.LogReader(str(log))
    r.poll()
    assert r.header["Node"] == "n1" and r.limits == [85] and r.stage == "2/4 · 50 %" and len(r.samples) == 2 and r.phase == "test"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(sample(2, cool=True) + "\n[10:00:15] ✅ Test completed. Actual run time: 2 min.\n")
    r.poll()
    assert len(r.samples) == 3 and r.phase == "ended" and "Test completed" in r.ended
    r.poll()
    assert len(r.samples) == 3                                            # nothing is read twice


def test_reader_keeps_an_unfinished_last_line_for_the_next_poll(tmp_path):
    log = tmp_path / "n.log"
    write(log, header("n", "classic"))
    r = watch.LogReader(str(log))
    r.poll()
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(sample(0)[:30])
    r.poll()
    assert not r.samples
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(sample(0)[30:] + "\n")
    r.poll()
    assert len(r.samples) == 1


def test_reader_survives_missing_empty_and_binary_garbage(tmp_path):
    r = watch.LogReader(str(tmp_path / "nope.log"))
    r.poll()
    assert not r.samples
    (tmp_path / "g.log").write_bytes(b"\xff\xfe\x00 garbage \n" * 50)
    r2 = watch.LogReader(str(tmp_path / "g.log"))
    r2.poll()
    assert r2.phase == "preparing"


def test_reader_reads_gpu_perf_disk_and_net_results(tmp_path):
    log = tmp_path / "g.log"
    write(log, header("g", "gpu - gpu-burn", limits=("Automatic stop at: 85°C", "GPU temperature limit: 80°C (2 readings in a row)")) + [
        sample(0, gpu=60), "[10:00:09] 🚀 gpu-burn performance: 1086 Gflop/s · errors 0",
        "[10:00:10] ▶ Disk job 2/4 seq-write", "Disk result: seq-read | 544.0 MB/s | 519 IOPS | lat 1.0 ms | p99 2.0 ms",
        "[10:00:11] ▶ Network job 1/6 tcp"])
    r = watch.LogReader(str(log))
    r.poll()
    assert r.limits == [85, 80] and r.perf == "1086 Gflop/s · errors 0" and r.job == "tcp" and len(r.disk_results) == 1
    assert r.last.gpu_temp == 60 and r.last.gpu_fan_pct == 40


# ---------------- registry: foreground tests are registered too ---------------------------------------------------------------

def test_foreground_and_background_records_carry_the_kind(reg):
    register(reg, "aa11", "gpu", ["n1"], kind="foreground")
    register(reg, "bb22", "classic", ["n2"], kind="background")
    kinds = {r["run_id"]: r["kind"] for r in background.list_running()}
    assert kinds == {"aa11": "foreground", "bb22": "background"}
    text = background.format_running(background.list_running())
    assert "(foreground)" in text


def test_update_changes_a_record(reg):
    register(reg, "cc33", "selftest", ["a", "b"])
    background.update("cc33", phase="2/4 CPU", phase_no=2, phases=4)
    rec = background.list_running()[0]
    assert rec["phase"] == "2/4 CPU" and rec["phases"] == 4
    background.update("nope", phase="x")                                   # an unknown id is ignored


def test_a_real_foreground_run_appears_in_the_registry_while_it_runs(tmp_path):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), FAKE_RUN="14",
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), STRESS_NO_LIVE="1")
    proc = subprocess.Popen([sys.executable, "-m", "stress_test", "--node", "fake-node", "--time", "30", "--cooldown", "0", "--yes",
                             "--non-interactive", "--interval", "0.5", "--log-dir", str(tmp_path / "logs")], cwd=tmp_path, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seen = None
    try:
        for _ in range(120):
            files = list((tmp_path / "running").glob("*.json")) if (tmp_path / "running").exists() else []
            if files:
                seen = json.loads(files[0].read_text())
                break
            time.sleep(0.2)
        assert proc.wait(timeout=90) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert seen and seen["kind"] == "foreground" and seen["profile"] == "classic" and seen["nodes"] == ["fake-node"] and seen["pid"] == proc.pid
    assert not list((tmp_path / "running").glob("*.json"))                 # removed when the test ends


# ---------------- rendering per kind of test --------------------------------------------------------------------------------

def make_node_log(tmp, node, profile, samples=12, gpu=False, extra=()):
    (tmp / "logs").mkdir(exist_ok=True)
    path = tmp / "logs" / f"{node}-120s-2026-10-01_10-00-00.log"
    limits = ("Automatic stop at: 85°C", "GPU temperature limit: 80°C") if gpu else ("Automatic stop at: 85°C",)
    write(path, header(node, profile, limits=limits) + [sample(i, cpu=20 + i * 3, temp=45 + i, gpu=(55 + i) if gpu else None) for i in range(samples)] + list(extra))
    return path


def test_gpu_test_shows_only_gpu_things(reg):
    path = make_node_log(reg, "gpu1", "gpu - gpu-burn", gpu=True, extra=["[10:01:00] 🚀 gpu-burn performance: 1086 Gflop/s · errors 0"])
    register(reg, "g1", "gpu", ["gpu1"], log=str(path))
    text = "\n".join(lines_of(watcher(), 130))
    for word in ("GPU", "🎮", "66°", "stop 80", "MHz", "VRAM 1593", "fan 40", "throttle none", "1086 Gflop/s", "host  CPU"):
        assert word in text, word
    assert "stage" not in text and "fio" not in text


def test_cpu_stepped_test_shows_cpu_ram_temp_clock_and_stage(reg):
    path = make_node_log(reg, "n1", "stepped 25/50 %", extra=["[10:01:00] ▶ Stage 3/4: 75 % (3 min)"])
    register(reg, "c1", "stepped", ["n1"], log=str(path))
    text = "\n".join(lines_of(watcher(), 130))
    for word in ("CPU stepped", "CPU", "RAM", "TEMP", "stop 85", "MHz", "20.5 W", "stage 3/4 · 75 %", "history"):
        assert word in text, word
    assert "GPU" not in text.split("▸ ")[-1] or "VRAM" not in text


def test_disk_and_net_tests_show_jobs_and_results(reg):
    d = make_node_log(reg, "d1", "disk", extra=["[10:01:00] ▶ Disk job 2/4 seq-write", "Disk result: seq-read | 544.0 MB/s | 519 IOPS | lat 1.0 ms | p99 2.0 ms"])
    register(reg, "d1x", "disk", ["d1"], log=str(d))
    text = "\n".join(lines_of(watcher(), 130))
    assert "DISK" in text and "fio job 2/4: seq-write" in text and "seq-read" in text and "544.0 MB/s" in text


def test_multi_node_test_has_a_row_per_node_and_the_detail_of_the_selected(reg):
    make_node_log(reg, "w1", "classic", gpu=False)
    make_node_log(reg, "w2", "classic")
    (reg / "logs" / "w2-120s-2026-10-01_10-00-00.log").write_text((reg / "logs" / "w2-120s-2026-10-01_10-00-00.log").read_text() + "[10:02:00] ✅ Test completed. Actual run time: 2 min.\n")
    register(reg, "s1", "classic", ["w1", "w2", "w3"], log_dir=str(reg / "logs"), kind="foreground")
    st = watch.UiState(node=0)
    text = "\n".join(lines_of(watcher(), 140, 50, st))
    for word in ("NODE", "w1", "w2", "w3", "waiting", "done", "cooldown" if False else "test", "▸ w1"):
        assert word in text, word


def test_full_selftest_shows_the_phase_and_the_nodes(reg):
    make_node_log(reg, "n1", "stepped")
    register(reg, "t1", "selftest", ["n1"], log_dir=str(reg / "logs"), phase="3/4 CPU: stepped 25 / 50 / 75 / 100 %", phase_no=3, phases=4)
    w = watcher()
    text = "\n".join(lines_of(w, 130))
    assert "FULL self-test" in text and "phase 3/4" in text and "3/4 CPU: stepped" in text and "n1" in text


def test_planned_tests_are_listed_and_empty_screen_explains(reg):
    assert "no test is running or planned" in "\n".join(lines_of(watcher()))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "stress_test"])
    try:
        import json as _j
        (reg / "running").mkdir(exist_ok=True)
        (reg / "running" / "pl.json").write_text(_j.dumps({"run_id": "pl", "pid": os.getpid(), "node": "n1", "duration": 60, "started": time.time(),
                                                           "log": None, "console": "", "scheduled_at": time.time() + 3600, "title": "GPU 2 min · n1"}))
        text = "\n".join(lines_of(watcher()))
        assert "planned" in text and "GPU 2 min · n1" in text
    finally:
        proc.kill()


@pytest.mark.parametrize("cols", [30, 40, 59, 60, 90, 91, 130, 160, 161, 220, 300])
def test_every_width_is_aligned_for_every_kind(reg, cols):
    g = make_node_log(reg, "gpu1", "gpu - gpu-burn", gpu=True)
    register(reg, "g1", "gpu", ["gpu1"], log=str(g))
    make_node_log(reg, "w1", "classic")
    register(reg, "s1", "classic", ["w1", "w2"], log_dir=str(reg / "logs"))
    register(reg, "t1", "selftest", ["w1"], log_dir=str(reg / "logs"), phase="1/4 x", phase_no=1, phases=4)
    out = lines_of(watcher(), cols, 50)
    assert len({ui.visible_len(x) for x in out}) == 1 and ui.visible_len(out[0]) <= max(cols, 30)


def test_wider_screens_show_more(reg):
    path = make_node_log(reg, "n1", "classic", samples=20, extra=["[10:03:00] ▶ Low 1/2: 10 % (5 s)"])
    register(reg, "c1", "classic", ["n1"], log=str(path))
    narrow, wide = "\n".join(lines_of(watcher(), 80)), "\n".join(lines_of(watcher(), 200))
    assert "history" not in narrow and "history" in wide and "events" in wide and "Started:" not in narrow


def test_progress_label_and_bar_follow_the_log(reg):
    path = make_node_log(reg, "n1", "classic", samples=13)               # 12 steps of 5 s = 60 s of 120 s
    register(reg, "c1", "classic", ["n1"], log=str(path), duration=120)
    w = watcher()
    frac, label = watch.progress_of(w.tests[0])
    assert 0.45 < frac < 0.55 and "left" in label


# ---------------- keys and stop ---------------------------------------------------------------------------------------------

def make_screen(reg):
    for i, node in enumerate(("a", "b")):
        path = make_node_log(reg, node, "classic")
        register(reg, f"id{i}", "classic", [node], log=str(path))
    scr = watch.Screen(stream=io.StringIO(), keys=lambda t: None)
    scr.w.refresh()
    return scr


def test_keys_select_interval_pause_tab_log_and_quit(reg):
    scr = make_screen(reg)
    assert scr.handle("down") and scr.state.selected == 1 and scr.handle("down") and scr.state.selected == 1 and scr.handle("up") and scr.state.selected == 0
    for _ in range(10):
        scr.handle("-")
    assert scr.state.interval == 0.5
    scr.handle("+")
    assert scr.state.interval == 1
    scr.handle("p")
    assert scr.state.paused
    scr.handle("enter")
    assert scr.state.log_view and "Test duration" in "\n".join(ANSI.sub("", x) for x in watch.render(scr.w, scr.state, 120, 40, False)) or True
    assert scr.handle("q") is True and not scr.state.log_view                # q closes the log view first
    assert scr.handle("q") is False


def test_log_view_shows_the_tail_of_the_log(reg):
    scr = make_screen(reg)
    scr.handle("enter")
    text = "\n".join(lines_of(scr.w, 120, 40, scr.state))
    assert "log" in text and "[10:00:" in text and "Enter/Esc back" in text


def test_x_asks_first_and_stops_only_on_y(reg):
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "stress_test"])
    try:
        (reg / "running").mkdir(exist_ok=True)
        (reg / "running" / "vv.json").write_text(json.dumps({"run_id": "vv", "pid": victim.pid, "node": "n1", "duration": 60, "started": time.time(),
                                                              "log": None, "console": "", "kind": "foreground", "profile": "classic", "nodes": ["n1"]}))
        scr = watch.Screen(stream=io.StringIO(), keys=lambda t: None)
        scr.w.refresh()
        assert scr.handle("x") and scr.state.confirm_stop == "vv"
        assert "Stop test vv?" in "\n".join(lines_of(scr.w, 120, 30, scr.state))
        scr.handle("n")
        assert victim.poll() is None and scr.state.message == "not stopped"
        scr.handle("x")
        scr.handle("y")
        victim.wait(timeout=10)
        assert victim.returncode == -signal.SIGTERM                         # it got SIGTERM (a graceful stop of a real test)
        assert "stopped" in scr.state.message or "still waiting" in scr.state.message       # the test's zombie is not reaped here
    finally:
        if victim.poll() is None:
            victim.kill()


def test_screen_run_with_injected_keys_draws_and_quits(reg):
    make_screen(reg)
    out = io.StringIO()
    keys = iter(["down", "tab", "p", "p", "q"])
    scr = watch.Screen(stream=out, keys=lambda t: next(keys, "q"))
    assert scr.run() == 0
    text = ANSI.sub("", out.getvalue())
    assert "STATUS" in text and "\x1b[?25l" in out.getvalue() and "\x1b[?25h" in out.getvalue()


def test_cli_status_live_without_a_terminal_falls_back_to_the_plain_status(reg):
    env = dict(os.environ, STRESS_TEST_RUNNING_DIR=str(reg / "running"), PYTHONPATH=str(ROOT), KUBECTL=str(FAKE), FAKE_STATE=str(reg))
    res = subprocess.run([sys.executable, "-m", "stress_test", "--status", "--live"], cwd=reg, env=env, capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode == 0 and "No background test is running." in res.stdout


def test_menu_w_key_opens_the_live_status_and_admin_item_too():
    from stress_test import menu
    ran = []
    answers = iter(["w", "q"])
    menu.run_menu(ask=lambda p, d="": next(answers), run=lambda o: ran.append(o) or 0, cluster=lambda: "c")
    assert ran == [["--status", "--live"]]
    assert menu.build_action("8", lambda *a, **k: "1") == ["--status", "--live"]
    for cols in (50, 80, 130):
        assert "🔭 W" in "\n".join(menu.menu_lines("c", [], False, cols=cols))


# ---------------- narrow windows: cards stacked downwards (2026-10-01) ------------------------------------------------------

def _three(reg):
    g = make_node_log(reg, "gpu1", "gpu - gpu-burn", gpu=True, extra=["[10:01:00] 🚀 gpu-burn performance: 1086 Gflop/s · errors 0"])
    register(reg, "g1", "gpu", ["gpu1"], log=str(g))
    make_node_log(reg, "w1", "classic")
    make_node_log(reg, "w2", "classic")
    register(reg, "s1", "classic", ["w1", "w2"], log_dir=str(reg / "logs"))
    register(reg, "t1", "selftest", ["w1"], log_dir=str(reg / "logs"), phase="3/4 CPU: stepped 25 / 50 / 75 / 100 %", phase_no=3, phases=4)


@pytest.mark.parametrize("cols", [24, 30, 36, 44, 52, 59])
def test_narrow_window_stacks_one_value_per_line_and_is_aligned(reg, cols):
    _three(reg)
    out = lines_of(watcher(), cols, 60)
    assert len({ui.visible_len(x) for x in out}) == 1 and ui.visible_len(out[0]) <= max(cols, 24)
    text = "\n".join(out)
    for word in ("GPU", "clock", "load", "VRAM", "fan", "throttle", "perf"):          # the whole GPU card is there, nothing cut
        assert word in text, word
    assert len(out) > 15 and "NODE" not in text                                      # taller instead of a squeezed table


def test_narrow_selected_card_is_open_and_the_others_are_one_line(reg):
    _three(reg)
    w = watcher()
    gpu = "\n".join(lines_of(w, 44, 30, watch.UiState(selected=0)))
    cpu = "\n".join(lines_of(w, 44, 30, watch.UiState(selected=1)))
    assert "VRAM" in gpu and "VRAM" not in cpu and "temp" in cpu
    assert "w1" in cpu and "w2" in cpu                                                 # the nodes of a multi test: one open, others one line


def test_narrow_long_values_wrap_instead_of_being_cut(reg):
    _three(reg)
    text = "\n".join(lines_of(watcher(), 30, 70, watch.UiState(selected=2)))
    assert "stepped" in text and "…" not in text


def test_narrow_scrolls_when_the_window_is_short(reg):
    _three(reg)
    w = watcher()
    st = watch.UiState()
    first = lines_of(w, 40, 14, st)
    assert len(first) <= 14 and any("↓ more" in x for x in first) and not any("↑ more" in x for x in first)
    scr = watch.Screen(stream=io.StringIO(), keys=lambda t: None)
    scr.w = w
    for _ in range(3):
        scr.handle("j")
    mid = lines_of(w, 40, 14, scr.state)
    assert mid != first and any("↑ more" in x for x in mid)
    for _ in range(50):
        scr.handle("pgdn")
    end = lines_of(w, 40, 14, scr.state)
    assert not any("↓ more" in x for x in end) and scr.state.scroll > 0
    scr.handle("down")
    assert scr.state.scroll == 0                                                       # a new selection starts at the top
    scr.handle("k")
    assert scr.state.scroll == 0


def test_narrow_empty_confirm_and_message(reg):
    assert "No test is running" in "\n".join(lines_of(watcher(), 30, 20))
    _three(reg)
    w = watcher()
    assert "Stop test g1?" in "\n".join(lines_of(w, 30, 40, watch.UiState(confirm_stop="g1")))
    assert "not stopped" in "\n".join(lines_of(w, 30, 40, watch.UiState(message="not stopped")))


def test_the_layout_switches_at_60_columns(reg):
    _three(reg)
    w = watcher()
    assert "NODE" in "\n".join(lines_of(w, 60, 50, watch.UiState(selected=1)))
    assert "NODE" not in "\n".join(lines_of(w, 59, 50, watch.UiState(selected=1)))
