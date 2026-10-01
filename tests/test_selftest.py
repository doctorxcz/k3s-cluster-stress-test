"""selftest.py: plan, cooling decisions, judging, report and run() with an injected runner / waiter."""
import argparse
import json
import re
from pathlib import Path

import pytest

from stress_test import cli, selftest
from stress_test import ui
from stress_test.models import DEFAULT_MAX_TEMP, MASTER_MAX_TEMP, WARN_TEMP, NodeInfo
from stress_test.selftest import (COOL_FLOOR, COOL_MARGIN, COOL_MAX_WAIT, PLATEAU_DROP, PLATEAU_WINDOW, NodeReport,
                                  cool_targets, cooling_decision, estimate, judge, plan_phases, report_lines)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def node(name, master=False, ready=True):
    return NodeInfo(name=name, ready=ready, is_control_plane=master, allocatable_mem_mib=8000,
                    capacity_cpu="8", os_image="x", kernel="1", architecture="amd64", runtime="r")


# ---------------- plan -----------------------------------------------------------------------------------

def test_plan_phases_order_and_argv(tmp_path):
    phases = plan_phases("quick", tmp_path, 3, False)
    assert [p.key for p in phases] == ["net", "disk", "cpu", "ram"]
    net, disk, cpu, ram = phases
    assert net.argv[0] == "--net-matrix" and "--nodes" not in net.argv
    assert disk.argv[:2] == ["--workers", "--parallel"] and "--smart" in disk.argv and "disk" in disk.argv
    assert cpu.argv[cpu.argv.index("--steps") + 1] == "25,50,75,100"
    assert cpu.argv[cpu.argv.index("--step-time") + 1] == "60"
    assert ram.argv[0] == "--workers" and "--ram-pct" in ram.argv
    for p in phases:
        assert "--non-interactive" in p.argv and p.argv[p.argv.index("--log-dir") + 1] == str(tmp_path)


def test_plan_phases_with_master_and_nodes(tmp_path):
    phases = plan_phases("standard", tmp_path, 2, True, "w1,w2,m1")
    assert phases[0].argv[phases[0].argv.index("--nodes") + 1] == "w1,w2,m1"
    assert phases[1].argv[0] == "--cluster" and phases[2].argv[0] == "--cluster"
    assert phases[3].argv[0] == "--workers"                      # RAM: workers only
    assert plan_phases("standard", tmp_path, 2, True)[2].seconds > plan_phases("standard", tmp_path, 2, False)[2].seconds


def test_levels_grow_and_estimate(tmp_path):
    totals = [estimate(plan_phases(lv, tmp_path, 3, False))[0] for lv in ("quick", "standard", "thorough")]
    assert totals == sorted(totals) and len(set(totals)) == 3
    work, cool = estimate(plan_phases("quick", tmp_path, 3, False))
    assert cool == COOL_MAX_WAIT * 3 and work == sum(p.seconds for p in plan_phases("quick", tmp_path, 3, False))


def test_human():
    assert selftest.human(45) == "45 s" and selftest.human(3600) == "60 min"


# ---------------- cooling ---------------------------------------------------------------------------------

def test_cool_targets_margin_floor_and_missing_sensor():
    t = cool_targets({"a": 50, "b": 30, "c": None})
    assert t == {"a": 50 + COOL_MARGIN, "b": COOL_FLOOR}


def test_cooling_decision_cooled():
    targets = {"a": 58, "b": 45}
    assert cooling_decision([], targets, {"a": 58, "b": 40}, 10) == "cooled"      # at the target counts
    assert cooling_decision([], targets, {}, 10) == "cooled"                        # nothing readable
    assert cooling_decision([], targets, {"a": None}, 10) == "cooled"


def test_cooling_decision_keep_waiting_when_still_falling():
    targets = {"a": 58}
    history = [(0, 20), (60, 15)]
    assert cooling_decision(history, targets, {"a": 70}, 130) == ""                 # 12 above, was 20 over 130 s ago
    assert cooling_decision([], targets, {"a": 70}, 30) == ""                       # too early for a plateau verdict


def test_cooling_decision_plateau():
    targets = {"a": 58}
    history = [(0, 10.0), (60, 9.8)]
    assert cooling_decision(history, targets, {"a": 58 + 10}, PLATEAU_WINDOW + 5) == "plateau"
    better = [(0, 10.0 + PLATEAU_DROP + 0.5)]
    assert cooling_decision(better, targets, {"a": 68}, PLATEAU_WINDOW + 5) == ""


def test_cooling_decision_timeout_beats_plateau():
    assert cooling_decision([(0, 10)], {"a": 58}, {"a": 68}, COOL_MAX_WAIT) == "timeout"


def test_cool_wait_returns_reason_with_injected_probes(monkeypatch):
    seq = iter([{"a": 80}, {"a": 70}, {"a": 45}])

    class FakeProbes:
        def __init__(self, kube, nodes): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def temps(self): return next(seq)
    monkeypatch.setattr(selftest, "Probes", FakeProbes)
    lines, slept = [], []
    why = selftest.cool_wait(None, [node("a")], {"a": 40}, lines.append, sleep=slept.append)
    assert why == "cooled" and len(slept) == 2
    assert selftest.cool_wait(None, [node("a")], {"a": None}, lines.append, sleep=slept.append) == "no sensor"


# ---------------- judge and report ---------------------------------------------------------------------

def reports(**over):
    r = {"w1": NodeReport("w1"), "m1": NodeReport("m1", master=True)}
    for k, v in over.items():
        name, attr = k.split("__")
        setattr(r[name], attr, v)
    return r


def test_judge_ok():
    r = reports(w1__temp_max=60, w1__cpu_ops=1000.0, w1__net_avg=900.0)
    judge(r, {}, {"cpu": 0})
    assert r["w1"].verdict == "OK"


def test_judge_hot_warn_and_fail_thresholds():
    r = reports(w1__temp_max=WARN_TEMP, w1__cpu_ops=1.0, m1__temp_max=MASTER_MAX_TEMP - 2)
    judge(r, {}, {"cpu": 0})
    assert r["w1"].verdict == "WARN" and "hot" in r["w1"].warnings[0]
    assert r["m1"].verdict == "FAIL" and "reached" in r["m1"].problems[0]
    r2 = reports(w1__temp_max=DEFAULT_MAX_TEMP - 2, w1__cpu_ops=1.0)
    judge(r2, {}, {})
    assert r2["w1"].verdict == "FAIL"


def test_judge_throttling_disk_net_and_missing_cpu():
    r = reports(w1__throttling=True, w1__throttle_text="clock -30 %", w1__net_avg=100.0,
                w1__disk_health=["  ⚠️ reallocated sectors: 4"])
    judge(r, {}, {"cpu": 0})
    text = " | ".join(r["w1"].warnings)
    assert "throttling" in text and "slow network" in text and "disk health: reallocated sectors: 4" in text
    assert r["w1"].problems == ["no CPU test result"]                 # worker without the cpu result
    assert r["m1"].problems == []                                      # master does not need it


def test_judge_uses_matrix_skips_and_findings():
    r = reports(w1__cpu_ops=1.0)
    matrix = {"skipped": {"w1": "no answer"}, "findings": ["w1 link is 100 Mbit/s", "w1 was not tested"]}
    judge(r, matrix, {"cpu": 0})
    assert "network test skipped: no answer" in r["w1"].problems[0]
    assert r["w1"].warnings == ["w1 link is 100 Mbit/s"]


def _meta():
    return {"started": "S", "finished": "F", "level": "quick", "seconds": 3600}


def test_report_lines_and_framed_verdict(tmp_path):
    r = reports(w1__temp_max=60, w1__idle=35, w1__cpu_ops=1234.0, w1__ram_max_pct=71.0,
                w1__disk={"seq-read": (500.0, 100.0), "seq-write": (400.0, 90.0)}, w1__net_avg=930.0,
                m1__temp_max=90, m1__cpu_ops=1.0)
    matrix = {"nodes": ["w1", "m1"], "pairs": [{"client": "w1", "server": "m1", "mbps": 900.0, "capped": False},
                                               {"client": "m1", "server": "w1", "mbps": None, "capped": False}]}
    judge(r, matrix, {"cpu": 0})
    phases = plan_phases("quick", tmp_path, 1, True)
    lines = report_lines(_meta(), r, matrix, {"net": 0, "disk": 0, "cpu": 1}, phases)
    text = "\n".join(lines)
    assert "FULL SELF-TEST REPORT" in text and "35→60" in text and "500/400" in text and "(exit code 1)" in text
    assert "NETWORK MATRIX" in text and "900" in text and "x" in text
    assert "FINDINGS" in text and lines[-2].startswith("VERDICT: ❌ 1 node(s) with problems")
    for on in (False, True):
        frame = selftest.framed_verdict(r, lines, on)
        assert len({ui.visible_len(x) for x in frame}) == 1
        assert "FULL SELF-TEST - RESULT" in ANSI.sub("", "\n".join(frame))


def test_report_healthy_verdict(tmp_path):
    r = reports(w1__cpu_ops=1.0, m1__cpu_ops=1.0)
    lines = report_lines(_meta(), r, {}, {}, plan_phases("quick", tmp_path, 1, True))
    assert lines[-2] == "VERDICT: ✅ the whole cluster is healthy"


def test_collect_reads_matrix_and_averages(tmp_path):
    (tmp_path / "net-matrix-1.json").write_text(json.dumps({"pairs": [
        {"client": "w1", "server": "m1", "mbps": 800, "capped": False},
        {"client": "m1", "server": "w1", "mbps": 1000, "capped": False},
        {"client": "w1", "server": "m1", "mbps": 100, "capped": True}]}))
    reps, matrix = selftest.collect(tmp_path, [node("w1"), node("m1", True)])
    assert reps["w1"].net_avg == 900 and reps["m1"].master and len(matrix["pairs"]) == 3


def test_warning_and_plan_boxes_are_aligned(tmp_path):
    phases = plan_phases("standard", tmp_path, 2, True)
    for box in (selftest.warning_box(False), selftest.plan_box(phases, "standard", [node("w1"), node("m1", True)], False)):
        assert {ui.visible_len(x) for x in box} == {100}


# ---------------- run() ---------------------------------------------------------------------------------

class StubKube:
    def __init__(self, nodes, busy=()):
        self.nodes, self.busy = {n.name: n for n in nodes}, list(busy)

    def list_node_names(self): return list(self.nodes)
    def get_node(self, name): return self.nodes[name]
    def list_tool_pods(self): return self.busy


class StubProbes:
    def __init__(self, kube, nodes): self.nodes = nodes
    def __enter__(self): return self
    def __exit__(self, *e): pass
    def temps(self): return {n.name: 40 for n in self.nodes}


@pytest.fixture
def stub_probes(monkeypatch):
    monkeypatch.setattr(selftest, "Probes", StubProbes)
    monkeypatch.setattr(cli, "ask_yes_no", lambda *a, **k: False)      # "test the master too?" (uses cli.ask, not the injected one)


def make_args(tmp_path, **kw):
    base = dict(log_dir=str(tmp_path / "logs"), yes=False, non_interactive=True, include_master=False,
                self_test_level="quick", self_test_ack=False)
    base.update(kw)
    return argparse.Namespace(**base)


def kube_fixture():
    return StubKube([node("w1"), node("w2"), node("m1", master=True)])


def test_run_non_interactive_without_ack_refuses(tmp_path, stub_probes, capsys):
    called = []
    code = selftest.run(make_args(tmp_path, yes=True), kube_fixture(), ask=lambda *a: "y",
                        runner=lambda argv: called.append(argv) or 0, waiter=lambda idle: "cooled")
    out = capsys.readouterr().out
    assert code == 0 and called == [] and "--self-test-ack" in out and "Cancelled" in out


def test_run_cancel_at_first_confirmation(tmp_path, stub_probes, capsys):
    called = []
    code = selftest.run(make_args(tmp_path, non_interactive=False), kube_fixture(), ask=lambda *a: "n",
                        runner=lambda argv: called.append(argv) or 0, waiter=lambda idle: "cooled")
    assert code == 0 and called == [] and "Cancelled, nothing was started." in capsys.readouterr().out


def test_run_wrong_second_word_cancels(tmp_path, stub_probes):
    answers = iter(["y", "nope"])
    called = []
    code = selftest.run(make_args(tmp_path, non_interactive=False), kube_fixture(), ask=lambda *a: next(answers),
                        runner=lambda argv: called.append(argv) or 0, waiter=lambda idle: "cooled")
    assert code == 0 and called == []


def test_run_refuses_when_a_test_is_running(tmp_path, stub_probes, capsys):
    class Pod:
        name, phase = "stress-test-abc", "Running"
    kube = StubKube([node("w1")], busy=[Pod()])
    code = selftest.run(make_args(tmp_path, yes=True, self_test_ack=True), kube, runner=lambda a: 0, waiter=lambda i: "x")
    assert code == cli.EXIT_ERROR and "running right now" in capsys.readouterr().out


def test_run_needs_a_ready_worker(tmp_path, stub_probes, capsys):
    kube = StubKube([node("m1", master=True), node("w1", ready=False)])
    assert selftest.run(make_args(tmp_path, yes=True, self_test_ack=True), kube, runner=lambda a: 0,
                        waiter=lambda i: "x") == cli.EXIT_ERROR
    assert "at least one Ready worker" in capsys.readouterr().out


def test_run_full_flow_with_ack(tmp_path, stub_probes, capsys):
    argvs, waits = [], []
    code = selftest.run(make_args(tmp_path, yes=True, self_test_ack=True), kube_fixture(),
                        runner=lambda argv: argvs.append(argv) or 0,
                        waiter=lambda idle: waits.append(idle) or "cooled")
    out = capsys.readouterr().out
    assert len(argvs) == 4 and len(waits) == 3 and waits[0] == {"w1": 40, "w2": 40}     # master not included
    assert all("--cluster" not in a for a in argvs)
    assert "PHASE 4/4" in out and "FULL SELF-TEST - RESULT" in ANSI.sub("", out)
    assert code == cli.EXIT_ERROR                       # no logs were produced -> "no CPU test result" for the workers
    (folder,) = (tmp_path / "logs").rglob("selftest-*")
    data = json.loads(next(folder.glob("full-selftest-*.json")).read_text())
    assert data["kind"] == "full-selftest" and data["codes"] == {"net": 0, "disk": 0, "cpu": 0, "ram": 0}
    assert set(data["nodes"]) == {"w1", "w2"}
    assert "VERDICT" in next(folder.glob("full-selftest-*.log")).read_text()


def test_run_with_master_uses_cluster_scope_and_nodes_arg(tmp_path, stub_probes):
    argvs = []
    selftest.run(make_args(tmp_path, yes=True, self_test_ack=True, include_master=True), kube_fixture(),
                 runner=lambda argv: argvs.append(argv) or 0, waiter=lambda idle: "cooled")
    assert argvs[1][0] == "--cluster" and argvs[3][0] == "--workers"


def test_run_interrupted_phase_stops_and_reports(tmp_path, stub_probes):
    argvs = []

    def runner(argv):
        argvs.append(argv)
        return cli.EXIT_INTERRUPTED
    selftest.run(make_args(tmp_path, yes=True, self_test_ack=True), kube_fixture(), runner=runner,
                 waiter=lambda idle: "cooled")
    assert len(argvs) == 1 and list((tmp_path / "logs").rglob("selftest-*/full-selftest-*.json"))


def test_choose_level_paths():
    assert selftest.choose_level(argparse.Namespace(self_test_level="thorough", non_interactive=True), None) == "thorough"
    assert selftest.choose_level(argparse.Namespace(self_test_level=None, non_interactive=True), None) == "standard"


def test_cli_flags_parse():
    a = cli.build_parser().parse_args(["--self-test", "--self-test-level", "quick", "--self-test-ack"])
    assert a.self_test and a.self_test_level == "quick" and a.self_test_ack
    assert cli.build_parser().parse_args([]).self_test is False


# ---------------- GPU phase (only nodes with an NVIDIA GPU) --------------------------------------------------

def test_plan_adds_one_gpu_phase_per_gpu_node_after_cpu(tmp_path):
    phases = plan_phases("quick", tmp_path, 3, False, gpu_nodes=["g1"])
    assert [p.key for p in phases] == ["net", "disk", "cpu", "gpu-g1", "ram"]
    gpu = phases[3]
    assert gpu.argv[:4] == ["--profile", "gpu", "--node", "g1"] and gpu.argv[gpu.argv.index("--time") + 1] == "60"
    assert [p.key for p in plan_phases("quick", tmp_path, 3, False)] == ["net", "disk", "cpu", "ram"]


def test_judge_gpu_findings_and_failed_gpu_phase():
    ok = NodeReport("g1", gpu={"findings": [], "max_temp": 70})
    hot = NodeReport("g2", gpu={"findings": ["GPU reached 77 °C"], "max_temp": 77})
    judge({"g1": ok, "g2": hot}, {}, {"gpu-g1": 0, "gpu-g2": 3})
    assert ok.verdict == "OK"
    assert hot.verdict == "FAIL" and any("GPU reached 77" in w for w in hot.warnings)


def test_report_has_gpu_section():
    rep = NodeReport("g1", gpu={"findings": [], "max_temp": 70, "clock_min": 1200, "clock_max": 1400,
                                 "avg_power": None, "throttle_pct": 0.0})
    text = "\n".join(report_lines({"started": "x", "finished": "y", "level": "quick", "seconds": 1},
                                  {"g1": rep}, {}, {}, []))
    assert "GPU (gpu-burn" in text and "power N/A" in text and "1200-1400 MHz" in text


def test_plan_subset_uses_nodes_scope_and_skips_matrix_for_one_node(tmp_path):
    one = plan_phases("quick", tmp_path, 1, False, "g1", gpu_nodes=["g1"], subset=True, ram_nodes="g1")
    assert [p.key for p in one] == ["disk", "cpu", "gpu-g1", "ram"]            # no network matrix with a single node
    assert one[0].argv[:3] == ["--nodes", "g1", "--parallel"] and "--workers" not in one[0].argv
    assert one[3].argv[:3] == ["--nodes", "g1", "--parallel"]
    two = plan_phases("quick", tmp_path, 2, False, "a,b", subset=True, ram_nodes="a,b")
    assert two[0].key == "net" and two[0].argv[two[0].argv.index("--nodes") + 1] == "a,b"
