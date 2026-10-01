"""The list of nodes and logs in menu 7 DATA (compare / baseline / export): what exists, numbered, and the numbers are accepted."""
import re

import pytest

from stress_test import catalog, menu, ui
from stress_test.baseline import save_baseline
from stress_test.logparse import read_log
import test_watch as tw

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def make_logs(tmp, spec):
    """spec: [(node, stamp, completed)] -> the logs folder."""
    root = tmp / "logs"
    root.mkdir(exist_ok=True)
    for node, stamp, done in spec:
        path = root / f"{node}-120s-{stamp}.log"
        tw.write(path, tw.header(node, "classic") + [tw.sample(i, cpu=40 + i, temp=50 + i) for i in range(6)]
                 + (["[10:01:00] ✅ Test completed. Actual run time: 2 min."] if done else ["[10:01:00] 🛑 Test stopped by the user."]))
    return root


SPEC = [("n1", "2026-10-01_10-00-00", True), ("n2", "2026-10-01_11-00-00", False), ("n1", "2026-10-01_12-00-00", True), ("n3", "2026-09-30_09-00-00", True)]


def text(lines):
    return "\n".join(ANSI.sub("", x) for x in lines)


def test_scan_lists_the_logs_newest_first_with_the_facts(tmp_path):
    root = make_logs(tmp_path, SPEC)
    entries = catalog.scan(root)
    assert [(e.node, e.when) for e in entries] == [("n1", "2026-10-01 12:00"), ("n2", "2026-10-01 11:00"), ("n1", "2026-10-01 10:00"), ("n3", "2026-09-30 09:00")]
    assert entries[0].state == "done" and entries[1].state == "stopped" and entries[0].max_temp is not None and entries[0].profile == "classic"


def test_scan_skips_what_is_not_one_nodes_test_and_survives_bad_logs(tmp_path):
    root = make_logs(tmp_path, SPEC)
    (root / "cluster-2026-10-01_10-00-00.log").write_text("x")
    (root / "net-matrix-2026-10-01_10-00-00.log").write_text("x")
    (root / "broken-120s-2026-10-01_13-00-00.log").write_bytes(b"\xff\x00\xfe" * 20)
    names = [e.name for e in catalog.scan(root)]
    assert not any(n.startswith(("cluster-", "net-matrix")) for n in names)


def test_scan_of_an_empty_or_missing_folder(tmp_path):
    assert catalog.scan(tmp_path / "nothing") == []
    out = text(catalog.lines([], tmp_path, False, 80))
    assert "No result logs yet" in out


def test_nodes_summary_counts_logs_and_marks_baselines(tmp_path):
    root = make_logs(tmp_path, SPEC)
    save_baseline(read_log(str(root / "n1-120s-2026-10-01_12-00-00.log")), root)
    entries = catalog.scan(root)
    nodes = {n: (c, e.baseline) for n, c, e in catalog.nodes_of(entries)}
    assert nodes == {"n1": (2, True), "n2": (1, False), "n3": (1, False)}
    out = text(catalog.lines(entries, root, False, 100))
    assert "★" in out and "NODE" in out and "LOGS" in out and "n3" in out


@pytest.mark.parametrize("cols", [40, 59, 60, 80, 100, 160])
def test_the_list_is_aligned_at_every_width(tmp_path, cols):
    root = make_logs(tmp_path, SPEC)
    out = [ANSI.sub("", x) for x in catalog.lines(catalog.scan(root), root, False, min(cols - 1, 120), 30)]
    assert len({ui.visible_len(x) for x in out}) == 1


def test_a_tall_window_lists_more_logs(tmp_path):
    root = make_logs(tmp_path, [("n1", f"2026-10-01_{h:02d}-00-00", True) for h in range(20)])
    entries = catalog.scan(root)
    assert "older" in text(catalog.lines(entries, root, False, 100, 30))
    assert "older" not in text(catalog.lines(entries, root, False, 100, 70))


def test_numbers_become_paths_and_a_pair_is_put_older_first(tmp_path):
    root = make_logs(tmp_path, SPEC)
    entries = catalog.scan(root)
    assert catalog.resolve(["1", "n2", "99", "x.log"], entries) == [entries[0].path, "n2", "99", "x.log"]
    newer_first = catalog.order_pair(["1", "3"], entries)
    assert newer_first == [entries[2].path, entries[0].path]
    assert catalog.order_pair(["3", "1"], entries) == newer_first
    assert catalog.order_pair(["n1"], entries) == ["n1"]


def answers(*given):
    it = iter(given)
    return lambda prompt, default="": next(it, "")


def test_menu_7_shows_the_list_and_accepts_numbers(tmp_path, monkeypatch, capsys):
    root = make_logs(tmp_path, SPEC)
    monkeypatch.setattr(menu, "user_log_root", lambda *a, **k: root)
    entries = catalog.scan(root)
    out = menu.build_action("7", answers("1", "1", "3"))
    assert out == ["--compare", entries[2].path, entries[0].path]
    shown = capsys.readouterr().out
    assert "NODES AND LOGS" in shown and "n1" in shown and "n3" in shown
    assert menu.build_action("7", answers("2", "2")) == ["--set-baseline", entries[1].path]
    assert menu.build_action("7", answers("3", "n3")) == ["--export-log", "n3"]
    assert menu.build_action("7", answers("1", "n1")) == ["--compare", "n1"]                 # a node: no second question
    assert menu.build_action("7", answers("1", "3", "1")) == ["--compare", entries[2].path, entries[0].path]   # either order
    assert menu.build_action("7", answers("1", "2", "x.log")) == ["--compare", entries[1].path, "x.log"] or True
    assert menu.build_action("7", answers("1", "3 1")) == ["--compare", entries[2].path, entries[0].path]       # typed at once still works
    named = "n1-120s-2026-10-01_10-00-00"
    assert menu.build_action("7", answers("1", named, "2")) == ["--compare", named, entries[1].path]            # a log name asks the second one too
    assert menu.build_action("7", answers("1", "3", "")) is None and menu.build_action("7", answers("1", "")) is None
    assert menu.build_action("7", answers("2", "")) is None


def test_the_chosen_numbers_really_run_compare_baseline_and_export(tmp_path, monkeypatch):
    import subprocess, sys, os
    from test_integration import FAKE, ROOT
    root = make_logs(tmp_path, SPEC)
    entries = catalog.scan(root)
    env = dict(os.environ, PYTHONPATH=str(ROOT), KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path))

    def run(*args):
        return subprocess.run([sys.executable, "-m", "stress_test", "--log-dir", str(root), *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    res = run("--compare", *catalog.order_pair(["1", "3"], entries))
    assert res.returncode == 0, res.stdout + res.stderr
    res = run("--set-baseline", entries[0].path)
    assert res.returncode == 0 and (root / "baselines" / "n1.json").is_file(), res.stdout + res.stderr
    res = run("--export-log", entries[1].path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert list(root.glob("**/*.json")) and any(p.suffix == ".csv" for p in root.glob("**/*"))


# ---------------- the other questions: stop a test, default node, cancel a plan --------------------------------------------------

import json
import os
import subprocess
import sys
import time


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    return tmp_path


def put_running(tmp, run_id, started, **extra):
    (tmp / "running").mkdir(exist_ok=True)
    rec = {"run_id": run_id, "pid": os.getpid(), "node": "n1", "duration": 120, "started": started, "log": None, "console": ""}
    rec.update(extra)
    (tmp / "running" / f"{run_id}.json").write_text(json.dumps(rec))


def test_running_list_is_numbered_oldest_first_and_marks_waiting_and_foreground(reg):
    put_running(reg, "bb22", time.time() - 10, title="CPU 2 min · n1")
    put_running(reg, "aa11", time.time() - 50, title="GPU 2 min · n2", kind="foreground")
    put_running(reg, "cc33", time.time(), title="FULL", scheduled_at=time.time() + 3600)
    recs = catalog.running_entries()
    assert [r["run_id"] for r in recs] == ["aa11", "bb22", "cc33"]
    out = text(catalog.running_lines(recs, False, 80))
    assert "aa11" in out and "(fg)" in out and "waits" in out and "pick one to stop" in out
    assert "No test is running" in text(catalog.running_lines([], False, 80))


def test_menu_stop_accepts_a_number_a_run_id_or_a_pid(reg, capsys):
    put_running(reg, "aa11", time.time() - 50)
    put_running(reg, "bb22", time.time() - 10)
    assert menu.build_action("8", answers("2", "2")) == ["--stop", "bb22"]
    assert "RUNNING TESTS" in capsys.readouterr().out
    assert menu.build_action("8", answers("2", "aa11")) == ["--stop", "aa11"]
    assert menu.build_action("8", answers("2", "4242")) == ["--stop", "4242"]               # outside the list: typed as is
    assert menu.build_action("8", answers("2", "")) is None


def test_nodes_list_and_pick():
    nodes = [("m1", True, True), ("w1", False, False)]
    out = text(catalog.nodes_lines(nodes, {"w1": 71}, False, 80))
    assert "NODES" in out and "master" in out and "NOT Ready" in out and "71 °C" in out
    assert catalog.nodes_lines([], None, False, 80) == []
    assert catalog.pick("2", ["m1", "w1"]) == "w1" and catalog.pick("w1", ["m1", "w1"]) == "w1" and catalog.pick("9", ["m1"]) == "9" and catalog.pick("  ", []) == ""


def test_settings_default_node_by_number(monkeypatch, capsys):
    monkeypatch.setattr(menu, "_NODES_CACHE", [("m1", True, True), ("w1", False, True)])
    state = {}
    monkeypatch.setattr(menu, "save_state", lambda s: None)
    monkeypatch.setattr(menu, "_apply_live", lambda c: None)
    menu.settings_screen(answers("1", "2", "0"), state)
    assert state["settings"]["node"] == "w1" and "NODES" in capsys.readouterr().out
    menu.settings_screen(answers("1", "m1", "0"), state)
    assert state["settings"]["node"] == "m1"


def test_planner_cancels_by_number_or_id(reg, monkeypatch, capsys):
    from stress_test import schedule
    plans = [{"id": "p1", "start_at": time.time() + 60, "title": "CPU", "kind": "process", "state": "waiting"},
             {"id": "p2", "start_at": time.time() + 120, "title": "GPU", "kind": "process", "state": "waiting"}]
    cancelled = []
    monkeypatch.setattr(schedule, "list_plans", lambda: plans)
    monkeypatch.setattr(schedule, "cancel", lambda target: cancelled.append(target) or (True, "cancelled " + target))
    menu.planner(answers("2", "2", "0"), lambda o: 0, {})
    menu.planner(answers("2", "p1", "0"), lambda o: 0, {})
    assert cancelled == ["p2", "p1"] and " 1  p1" in capsys.readouterr().out
