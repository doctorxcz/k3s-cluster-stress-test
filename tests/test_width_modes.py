"""The whole program follows the terminal width: compact (< 60 columns), normal (60..99), wide (100..200).

Every screen is rendered at several widths (STRESS_TEST_COLUMNS makes the width-dependent layout work without a
terminal) and no line may be wider than cols-1 cells, every frame has aligned borders, and the content stays there.
Without the variable (pipes, log files) the classic plain layout is kept.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stress_test import compare, menu, netmatrix as mx, selftest, series, ui
from stress_test.logparse import parse_log
from stress_test.models import NodeInfo, Sample
from stress_test.runner import EXIT_OK
from stress_test.summary import StressMetric, build_summary, run_stats

WIDTHS = [40, 59, 60, 76, 99, 100, 140, 200]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FAKE = HERE / "fake_kubectl.py"


def plain(lines):
    return [ANSI.sub("", ln) for ln in lines]


def flat(lines):
    """All the text of some lines as one string without the frame characters and runs of spaces."""
    return " ".join(re.sub(r"[┃┏┓┣┫┗┛━│─]", " ", " ".join(plain(lines))).split())


def shown(lines, cols):
    """What the terminal gets from ui.emit for these lines."""
    return plain(ui.adapt_lines(list(lines), cols))


def assert_fits(lines, cols, what=""):
    for ln in plain(lines):
        assert ui.visible_len(ln) <= cols - 1, (what, cols, ui.visible_len(ln), ln)


def assert_frames_aligned(lines):
    block = []
    for ln in plain(lines) + [""]:
        if ln[:1] in "┏┃┣┗" and ln:
            block.append(ln)
            if ln[0] == "┗":
                assert len({ui.visible_len(x) for x in block}) == 1, block
                block = []
        else:
            block = []


@pytest.fixture
def cols(monkeypatch):
    def setter(n):
        monkeypatch.setenv("STRESS_TEST_COLUMNS", str(n))
        return n
    return setter


# ---------------- helpers ----------------------------------------------------------------------------

def test_term_cols_env_override_and_plain_default(monkeypatch):
    monkeypatch.delenv("STRESS_TEST_COLUMNS", raising=False)
    assert ui.term_cols() is None and not ui.adaptive() and ui.cols() == ui.WIDTH + 1
    assert ui.avail() == ui.WIDTH and ui.panel_width() == ui.WIDTH
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "83")
    assert ui.term_cols() == 83 and ui.adaptive() and ui.avail() == 82
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "abc")
    assert ui.term_cols() is None


def test_mode_thresholds_and_limits(monkeypatch):
    assert [ui.mode(c) for c in (40, 59, 60, 99, 100, 200)] == ["compact", "compact", "normal", "normal", "wide", "wide"]
    assert (ui.COMPACT_BELOW, ui.WIDE_FROM, ui.WIDE_MAX) == (60, 100, 200)
    assert (menu.COMPACT_BELOW, menu.WIDE_FROM, menu.WIDE_MAX) == (60, 100, 200)
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "300")
    assert ui.avail() == 200 and ui.avail(130) == 130 and ui.panel_width() == 120
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "10")
    assert ui.avail() == ui.MIN_COLS


def test_wrap_hanging_indent_and_unbreakable_words():
    text = "Long sentence with words that has to be wrapped at word boundaries only"
    lines = ui.wrap(text, 30, "- ", "  ")
    assert all(ui.visible_len(x) <= 30 for x in lines) and lines[0].startswith("- ") and lines[1].startswith("  ")
    assert " ".join(x.strip("- ") for x in lines).split() == text.split()
    url = "https://github.com/doctorxcz/k3s-cluster-stress-test/blob/HEAD/HELPDESK.md"
    cut = ui.wrap("see " + url, 30)
    assert cut[0] == "see" and cut[1].endswith("…") and ui.visible_len(cut[1]) <= 30       # own line, cut, never split
    assert ui.wrap("", 20) == [""]
    assert ui.wrap("emoji 🔥 🔥 🔥 🔥 🔥 🔥", 12) and all(ui.visible_len(x) <= 12 for x in ui.wrap("emoji 🔥 🔥 🔥 🔥 🔥 🔥", 12))


def test_kv_block_classic_without_terminal(monkeypatch):
    monkeypatch.delenv("STRESS_TEST_COLUMNS", raising=False)
    assert ui.kv_block([("Node", "a"), ("Log", "no")]) == ["  Node:                a", "  Log:                 no"]


@pytest.mark.parametrize("n", WIDTHS)
def test_kv_block_modes(cols, n):
    cols(n)
    pairs = [("Node", "control-plane-node-00001 (master)"), ("Test duration", "10 min (600 s)"), ("CPU load", "100 %"),
             ("RAM load", "no"), ("Stop at", "85 °C (2 readings in a row)"), ("Cooldown after test", "60 s of measuring"),
             ("Log", "/home/user/cluster-testing/python-stress-test/logs/2026-09-30/node-600s-2026-09-30_10-00-00.log")]
    lines = ui.kv_block(pairs)
    assert_fits(lines, n)
    text = "\n".join(lines)
    assert "Test duration" in text and "85 °C" in text
    if ui.mode(n) == "compact":
        assert "  Node:" in lines and "    control-plane-node-00001 (master)" in lines      # the label on its own line
    if ui.mode(n) == "wide":
        assert any(len(re.findall(r"[A-Za-z]:\s{2}", ln)) >= 2 for ln in lines)            # two columns of short pairs
        assert any(ln.startswith("  Log:") for ln in lines)                                # the long value stays full width


def test_table_drops_columns_by_priority_and_cards_in_compact():
    columns = [("Node", 0, "<"), ("State", 0, "<"), ("max", 1, ">"), ("over", 2, ">"), ("extra", 9, ">")]
    rows = [["control-plane-node-00001 (master)", "OK", "61", "0 s", "12345678901234567890"]]
    wide = ui.table(columns, rows, 99, "wide")
    assert "extra" in wide[0] and all(ui.visible_len(x) <= 99 for x in wide)
    mid = ui.table(columns, rows, 50, "normal")
    assert "extra" not in mid[0] and "State" in mid[0] and "max" in mid[0] and all(ui.visible_len(x) <= 50 for x in mid)
    card = ui.table(columns, rows, 38, "compact")
    assert card[0].startswith("control-plane") and any("State OK" in x for x in card) and all(ui.visible_len(x) <= 38 for x in card)


def test_grid_columns():
    cells = [f"[{i}] node-{i}" for i in range(1, 9)]
    assert len(ui.grid(cells, 12)) == 8 and len(ui.grid(cells, 100)) < 8
    assert all(ui.visible_len(x) <= 100 for x in ui.grid(cells, 100))


def test_adapt_line_keeps_labels_bullets_and_icons():
    md = "normal"
    kv = "  Test duration:        about 35 min in total (+ installing iperf3) and more text goes here for wrapping"
    out = ui.adapt_line(kv, 80, md)
    assert out[0].startswith("  Test duration:") and out[1].startswith(" " * 24)
    assert ui.adapt_line(kv, 50, "compact")[0] == "  Test duration:"
    bullet = "  - a bullet that is long and has to wrap around the screen somewhere"
    assert ui.adapt_line(bullet, 30, md)[1].startswith("    ") and ui.adapt_line(bullet, 30, md)[0].startswith("  - ")
    icon = "ℹ️  For the stepped test the time is set with --step-time, --time is ignored."
    wrapped = ui.adapt_line(icon, 40, md)
    assert wrapped[0].startswith("ℹ️  ") and wrapped[1].startswith("    ")
    assert ui.adapt_line("=" * 52, 40, md) == ["=" * 40] and ui.adapt_line("-" * 10, 40, md) == ["-" * 10]
    assert ui.adapt_line("short", 40, md) == ["short"]


@pytest.mark.parametrize("n", WIDTHS)
def test_emit_never_prints_a_line_wider_than_the_terminal(cols, capsys, n):
    cols(n)
    ui.emit("=" * 52)
    ui.emit("CLUSTER SUMMARY")
    ui.emit("⚠️  Node control-plane-node-00001 runs these services (the test loads them, they may slow down or be interrupted):")
    ui.emit("    default/nginx-deployment-66b6c48dd5-abcde (Deployment nginx-deployment, 1 pod, cpu 250m, 512Mi)")
    ui.emit("  Log:                  /home/user/cluster-testing/python-stress-test/logs/2026-09-30/node-600s.log")
    ui.emit("https://github.com/doctorxcz/k3s-cluster-stress-test/blob/HEAD/HELPDESK.md and more words after the url")
    ui.emit("x" * 300)
    out = ANSI.sub("", capsys.readouterr().out).split("\n")
    assert_fits(out, n)
    assert "CLUSTER SUMMARY" in "\n".join(out) and "nginx-deployment" in "\n".join(out)


def test_emit_is_unchanged_without_terminal_or_variable(monkeypatch, capsys):
    monkeypatch.delenv("STRESS_TEST_COLUMNS", raising=False)
    long = "word " * 40
    ui.emit(long + "\n" + "=" * 52)
    assert capsys.readouterr().out == long + "\n" + "=" * 52 + "\n"


@pytest.mark.parametrize("n", WIDTHS)
def test_prompt_with_a_long_question_is_short_enough(cols, capsys, n):
    cols(n)
    prompt = ui.prompt("Extras? (s = disk health (SMART, privileged pod), w = ping another node during the test "
                       "(--net-watch); Enter = none)", "")
    printed = capsys.readouterr().out.split("\n")
    assert_fits(printed, n)
    assert ui.visible_len(ANSI.sub("", prompt).replace("\x01", "").replace("\x02", "")) <= n - 1
    assert "Extras?" in "\n".join(printed) + prompt and "--net-watch" in "\n".join(printed) + prompt


def test_short_prompt_is_unchanged(cols, capsys):
    cols(80)
    assert ui.prompt("Choice", "q") == "stress@k3s:~$ Choice [q] ▸ " and capsys.readouterr().out == ""


# ---------------- panels (submenus and questions) ------------------------------------------------------

COOLDOWN = [("1", "1 min", " (default)"), ("2", "3 min", ""), ("3", "5 min", ""), ("4", "10 min", " (max)"),
            ("5", "custom duration", "")]
NETJOBS = [("·", "dns", "CoreDNS + external name lookups"), ("·", "internet", "ping 1.1.1.1 + a bounded download"),
           ("·", "mtr", "path, loss and latency to the peer"),
           ("·", "service", "TCP through a Kubernetes Service (pod network only)"),
           ("·", "no-udp", "leave the UDP test out (when the firewall lets in only TCP)"),
           ("·", "all", "dns + internet + mtr + service")]


@pytest.mark.parametrize("n", WIDTHS)
@pytest.mark.parametrize("title,entries,back", [
    ("COOLDOWN (measuring CPU temperature and clock)", COOLDOWN, "off"),
    ("EXTRA NETWORK JOBS (comma separated, Enter = none)", NETJOBS, None),
    ("CPU LOAD", [("1", "classic", "one load for the given time"), ("2", "stepped", "gradually 25 / 50 / 75 / 100 %"),
                  ("3", "spike", "repeating jump between a low and a target load")], "Back")])
def test_panel_fits_and_keeps_all_entries(cols, n, title, entries, back):
    cols(n)
    lines = ui.panel(title, entries, False, back)
    assert_fits(lines, n)
    assert_frames_aligned(lines)
    text = flat(lines)
    for _key, label, hint in entries:
        assert label in text
        assert all(word in text for word in hint.split()[:2])
    if back:
        assert f"0 ▸ {back}" in text
    if ui.mode(n) == "wide" and len(entries) >= 5:
        assert any("│" in ln for ln in lines)                # two columns of choices
    if ui.mode(n) == "compact":
        assert not any("│" in ln for ln in lines)


def test_panel_without_terminal_is_the_classic_one(monkeypatch):
    monkeypatch.delenv("STRESS_TEST_COLUMNS", raising=False)
    lines = ui.panel("CPU LOAD", [("1", "classic", "one load")], False, "Back")
    assert {len(x) for x in lines} == {ui.WIDTH} and lines[1].startswith("┃ ◖●▲●◗ CPU LOAD")


# ---------------- menu screens -----------------------------------------------------------------------

@pytest.mark.parametrize("n", WIDTHS)
def test_help_pages_fit(cols, capsys, n):
    cols(n)
    menu.show_help(lambda *a, **k: "", size=(n, 30))
    lines = capsys.readouterr().out.split("\n")
    assert_fits(lines, n)
    assert_frames_aligned(lines)
    text = " ".join(lines)
    assert "HELP 1/" in text and "KEYS" in text and "README" in text
    if ui.mode(n) == "wide":
        assert any("│" in ln for ln in lines)


@pytest.mark.parametrize("n", WIDTHS)
def test_settings_screen_fits(cols, capsys, n, monkeypatch, tmp_path):
    monkeypatch.setenv("STRESS_TEST_MENU_STATE", str(tmp_path / "menu-state.json"))      # never touch the real state file
    cols(n)
    answers = iter(["1", "worker-3-with-a-rather-long-node-name", "0"])
    menu.settings_screen(lambda *a, **k: next(answers), {"settings": {}})
    lines = capsys.readouterr().out.split("\n")
    assert_fits(lines, n)
    assert_frames_aligned(lines)
    assert "SETTINGS" in " ".join(lines) and "default node" in " ".join(lines)


@pytest.mark.parametrize("n", WIDTHS)
def test_choose_submenu_and_extras_question(cols, capsys, n):
    cols(n)
    answers = iter(["1", ""])
    options = menu.build_action("1", lambda q, d="": next(answers))
    assert options == ["--profile", "classic"]
    assert_fits(capsys.readouterr().out.split("\n"), n)


# ---------------- test settings, summaries ----------------------------------------------------------

def S(t, temp=None, freq=None, cpu=100.0, phase="test", mem=4000, mem_pct=50.0, stage=0):
    return Sample(t=t, phase=phase, cpu_temp=temp, freq_mhz=freq, cpu_pct=cpu, mem_used_mib=mem, mem_used_pct=mem_pct,
                  stage=stage)


def _samples(peak, stage=0):
    test = [S(i * 5, peak - 10 + i, 3000 - i * 120, stage=stage) for i in range(12)]
    return test + [S(60 + i * 5, peak - 20 - i, 3000, cpu=3.0, phase="cooldown") for i in range(1, 8)]


@pytest.mark.parametrize("n", WIDTHS)
def test_test_summary_through_emit(cols, capsys, n):
    cols(n)
    lines = build_summary(_samples(85), None, [StressMetric("cpu", 1, 1.0, 2646.7)], cooldown_requested=60)
    ui.emit("\n".join(lines))
    out = ANSI.sub("", capsys.readouterr().out).split("\n")
    assert_fits(out, n)
    text = " ".join(out)
    assert "TEST SUMMARY" in text and "86 °C" in text and "Notes:" in text and "Throttling" in text
    if ui.mode(n) == "wide" and n >= 140:
        assert any(len(re.findall(r":\s{2,}\S", ln)) >= 2 for ln in out)        # short rows in two columns


def _outcome(name, code, peak=None, master=False, reason="", stage=False):
    stats = run_stats(_samples(peak), 40, 80, stage_targets=(25, 50, 75, 100) if stage else None) if peak else None
    return series.NodeOutcome(name, master, code, reason, stats, [StressMetric("cpu", 1, 1.0, 2646.7)] if peak else [],
                              f"/home/user/logs/{name}.log" if peak else None)


@pytest.mark.parametrize("n", WIDTHS)
def test_cluster_summary_for_the_screen(n):
    outcomes = [_outcome("worker-1", EXIT_OK, 84), _outcome("worker-3", EXIT_OK, 49),
                _outcome("control-plane-node-00001", EXIT_OK, 60, master=True),
                series.NodeOutcome("worker-4", reason="node is not Ready"),
                series.NodeOutcome("w9", reason="not tested (series interrupted)")]
    lines = shown(series.format_cluster_summary(outcomes, width=ui.avail(130, n)), n)
    text = "\n".join(lines)
    assert_fits(lines, n)
    assert "CLUSTER SUMMARY" in text and "Hottest: worker-1 (85 °C)." in " ".join(text.split())
    assert "not tested (series interrupted)" in " ".join(text.split()) and "SKIPPED" in text and "OK" in text      # the reason may wrap in a narrow window
    if n >= 140:
        assert "avg °C" in text and "throttling" in text and "return to idle" in text
    assert "worker-3" in text


def test_cluster_summary_classic_is_unchanged():
    lines = series.format_cluster_summary([_outcome("dell", EXIT_OK, 84)])
    assert lines[0] == "=" * 104 and lines[3].startswith("Node")


@pytest.mark.parametrize("n", WIDTHS)
def test_cluster_summary_stepped_stages_fit(n):
    lines = shown(series.format_cluster_summary([_outcome("worker-1", EXIT_OK, 84, stage=True)],
                                                width=ui.avail(130, n)), n)
    assert_fits(lines, n)
    assert "Temperatures per stage" in "\n".join(lines) and "100 % →" in " ".join(" ".join(lines).split())


def _log(temps_test, temps_cool, node="worker-1"):
    import importlib
    tc = importlib.import_module("test_compare")
    return parse_log(tc.make_log(temps_test, temps_cool, node=node), node + ".log")


@pytest.mark.parametrize("n", WIDTHS)
def test_compare_fits(n):
    sys.path.insert(0, str(HERE))
    a = _log([78, 80, 82, 83, 82, 83, 84, 83, 82, 82, 83, 84, 82], [62, 61, 60, 60, 58, 58, 57, 56, 56, 56, 55, 55])
    b = _log([66, 68, 70, 71, 72, 72, 73, 72, 72, 73, 74, 72, 72], [52, 51, 50, 49, 48, 48, 47, 47, 46, 46, 45, 45])
    lines = shown(compare.compare_runs(a, b, width=ui.avail(130, n)), n)
    assert_fits(lines, n)
    text = flat(lines)
    assert "TEST COMPARISON" in text and "CPU temp max" in text and "(better)" in text and "Total:" in text
    assert "84" in text and "74" in text
    classic = compare.compare_runs(a, b)
    assert "=" * compare.WIDTH in classic and "TEST COMPARISON" in classic             # the classic table is unchanged
    assert any(ln.startswith("CPU temp max:") and ui.visible_len(ln) >= 60 for ln in classic)


def _matrix(n_nodes):
    names = [f"control-plane-node-0{i}" for i in range(n_nodes)]
    r = mx.MatrixResult(names, 5, links={n: {"if": "eth0", "speed": 1000, "duplex": "full"} for n in names})
    for c in names:
        for s in names:
            if c != s:
                r.pairs.append(mx.Pair(c, s, 940.0, 1, 0.4, 0.0, capped=(c == names[0])))
    return r


@pytest.mark.parametrize("n", WIDTHS)
@pytest.mark.parametrize("nodes", [3, 9])
def test_net_matrix_tables_fit_and_keep_every_cell(n, nodes):
    lines = shown(mx.format_matrix(_matrix(nodes), width=ui.avail(130, n)), n)
    assert_fits(lines, n)
    text = "\n".join(lines)
    assert text.count("940") == nodes * (nodes - 1)
    assert "Throughput, Mbit/s" in text and "Ping, ms (average)" in text and f"[{nodes}]" in text
    classic = mx.format_matrix(_matrix(3))
    assert classic[classic.index("Throughput, Mbit/s") + 1].startswith("      " + "      [1]")


# ---------------- self-test screens ------------------------------------------------------------------

def _nodes():
    return [NodeInfo(name=n, ready=True, is_control_plane=m, allocatable_mem_mib=8000, capacity_cpu="8", os_image="x",
                     kernel="1", architecture="amd64", runtime="r")
            for n, m in (("worker-1", False), ("control-plane-node-00001", True))]


@pytest.mark.parametrize("n", WIDTHS)
def test_selftest_boxes_fit(cols, n, tmp_path):
    cols(n)
    phases = selftest.plan_phases("standard", tmp_path, 1, True)
    for lines in (selftest.warning_box(False), selftest.plan_box(phases, "standard", _nodes(), False)):
        assert_fits(lines, n)
        assert_frames_aligned(lines)
    text = flat(selftest.warning_box(False))
    assert "READ THIS BEFORE YOU START" in text and "FULL POWER" in text and "circuit breaker" in text
    plan = flat(selftest.plan_box(phases, "standard", _nodes(), False))
    assert "Network matrix" in plan and "control-plane-node-00001 (master)" in plan and "level: standard" in plan


@pytest.mark.parametrize("n", WIDTHS)
def test_selftest_verdict_fits(cols, n):
    cols(n)
    reports = {"worker-1": selftest.NodeReport("worker-1", temp_max=70, cpu_ops=2500.0, ram_max_pct=41.0,
                                                       disk={"seq-read": (480.0, 1000)}, net_avg=930.0),
               "control-plane-node-00001": selftest.NodeReport("control-plane-node-00001", True, temp_max=82,
                                                               problems=["reached 82 °C (limit 80 °C)"],
                                                               warnings=["slow disk WDC WD10EZEX: random read 1.0 MB/s"])}
    reports["control-plane-node-00001"].warnings.append("a long warning text about something that is wrong " * 2)
    lines = ["❌ control-plane-node-00001: reached 82 °C (limit 80 °C)",
             "⚠️ control-plane-node-00001: " + reports["control-plane-node-00001"].warnings[-1],
             "VERDICT: ❌ 1 node(s) with problems, 0 with warnings, 1 OK", "====="]
    box = selftest.framed_verdict(reports, lines, False)
    assert_fits(box, n)
    assert_frames_aligned(box)
    text = flat(box)
    assert "FULL SELF-TEST - RESULT" in text and "worker-1" in text and "FAIL" in text and "1 node(s) with problems" in text


# ---------------- whole program through the real command line --------------------------------------------

def run_tool(tmp_path, *args, columns=None, timeout=90):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT),
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), FAKE_RUN="3", NO_COLOR="1")
    env.pop("STRESS_TEST_COLUMNS", None)
    env.pop("COLUMNS", None)
    if columns:
        env["STRESS_TEST_COLUMNS"] = str(columns)
    return subprocess.run([sys.executable, "-m", "stress_test", *args], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=timeout)


@pytest.mark.parametrize("n", [40, 59, 60, 99, 100, 140, 200])
@pytest.mark.parametrize("args", [["--list-nodes"], ["--status"], ["--help"], ["--node", "fake-node", "--time", "60",
                                  "--non-interactive", "--dry-run"]])
def test_cli_output_fits(tmp_path, n, args):
    res = run_tool(tmp_path, *args, columns=n)
    assert res.returncode == 0, res.stdout + res.stderr
    assert_fits(res.stdout.split("\n"), n)
    if args[0] == "--list-nodes":
        assert "fake-node" in res.stdout and "worker" in res.stdout


@pytest.mark.parametrize("n", [40, 59, 76, 100, 160])
def test_real_test_run_output_fits_and_log_is_unchanged(tmp_path, n):
    res = run_tool(tmp_path, "--node", "fake-node", "--non-interactive", "--interval", "0.5", "--time", "5", "--cooldown", "0",
                   "--log", "--log-dir", str(tmp_path / "logs"), columns=n)
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert_fits(res.stdout.split("\n"), n)
    assert "TEST SETTINGS" in res.stdout and "TEST SUMMARY" in res.stdout and "Test completed" in res.stdout
    log = next((tmp_path / "logs").rglob("fake-node-5s-*.log")).read_text(encoding="utf-8")
    assert "=" * 52 in log and not ANSI.search(log) and "\x1b" not in log
    assert re.search(r"^CPU temp \(test\):\s{10}min", log, re.M)               # the log keeps the classic fixed layout


def test_plain_run_without_the_variable_keeps_the_classic_layout(tmp_path):
    res = run_tool(tmp_path, "--node", "fake-node", "--non-interactive", "--interval", "0.5", "--time", "5", "--cooldown", "0")
    assert res.returncode == EXIT_OK, res.stdout + res.stderr
    assert "  Node:                fake-node" in res.stdout and "=" * 52 in res.stdout


# ---------------- details ------------------------------------------------------------------------------

def test_cut_word_keeps_the_end_of_a_path_and_the_start_of_a_url():
    path = "/home/user/cluster-testing/python-stress-test/logs/2026-09-30/node-600s.log"
    cut = ui.cut_word(path, 40)
    assert cut.endswith("/node-600s.log") and "…" in cut and ui.visible_len(cut) <= 40 and cut.startswith("/home")
    url = "https://github.com/doctorxcz/k3s-cluster-stress-test/blob/HEAD/HELPDESK.md"
    assert ui.cut_word(url, 30).startswith("https://github.com") and ui.cut_word(url, 30).endswith("…")
    assert ui.cut_word("short", 30) == "short"


def test_compact_key_value_block_is_stacked_as_a_whole():
    lines = ["Samples:                  12 during the test", "Log:                      /home/user/a/very/long/path/to/the/log/file-2026.log",
             "Notes:", "  - a note"]
    out = ui.adapt_lines(lines, 40)
    assert out[:2] == ["Samples:", "  12 during the test"] and out[2] == "Log:" and out[3].endswith("file-2026.log")
    assert ui.adapt_lines(["Samples:   12 during the test"], 40) == ["Samples:   12 during the test"]    # a block that fits stays
    assert ui.adapt_lines(lines, 80)[0] == lines[0]                   # normal mode keeps the aligned columns


def test_wide_key_value_rows_go_into_two_columns_long_ones_stay_single():
    lines = [f"Row {i}:                   value {i}" for i in range(6)] + ["Long:                     " + "word " * 30]
    out = ui.adapt_lines(lines, 140)
    assert len(out) < len(lines) + 4 and any("Row 0:" in ln and "Row 3:" in ln for ln in out)
    assert ui.adapt_lines(lines, 99)[0] == lines[0]


def test_panel_compact_keeps_a_short_hint_beside_the_label():
    lines = plain(ui.panel("COOLDOWN", COOLDOWN, False, "off", columns=50))
    assert any("1 ▸ 1 min  (default)" in ln for ln in lines) and any("4 ▸ 10 min  (max)" in ln for ln in lines)


def test_width_override_is_read_at_print_time(monkeypatch, capsys):
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "40")
    ui.emit("word " * 20)
    first = capsys.readouterr().out.split("\n")
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "120")
    ui.emit("word " * 20)
    second = capsys.readouterr().out.split("\n")
    assert len(first) > 2 and len(second) == 2            # one line + the final newline
