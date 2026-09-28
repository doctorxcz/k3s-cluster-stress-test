"""Node selection, plan and series summary (without a cluster, with dummies)."""
import argparse

import pytest

from stress_test import series
from stress_test.cli import _choose_scope, _keep_master
from stress_test.models import NodeInfo, NodeWorkload, ProbeData, Sample, StressConfig
from stress_test.runner import (EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_OVERHEAT,
                                EXIT_PREMATURE)
from stress_test.summary import StressMetric, run_stats


def node(name, master=False, ready=True):
    return NodeInfo(name=name, ready=ready, is_control_plane=master, allocatable_mem_mib=8000,
                    capacity_cpu="8", os_image="x", kernel="1", architecture="amd64", runtime="r")


class FakeKube:
    def __init__(self, nodes):
        self.nodes = {n.name: n for n in nodes}

    def list_node_names(self):
        return list(self.nodes)

    def get_node(self, name):
        return self.nodes[name]


CLUSTER = [node("master1", master=True), node("w1"), node("w2"), node("w3", ready=False)]


# ---------------- node selection -------------------------------------------------------------------

def test_select_workers_excludes_master_and_skips_not_ready():
    ready, skipped = series.select_nodes(FakeKube(CLUSTER), "workers")
    assert [n.name for n in ready] == ["w1", "w2"]
    assert skipped == [("w3", "node is not Ready")]


def test_select_cluster_puts_master_last():
    ready, _ = series.select_nodes(FakeKube(CLUSTER), "cluster")
    assert [n.name for n in ready] == ["w1", "w2", "master1"]


def test_select_workers_with_include_master():
    ready, _ = series.select_nodes(FakeKube(CLUSTER), "workers", include_master=True)
    assert [n.name for n in ready] == ["w1", "w2", "master1"]


def test_select_explicit_nodes_keeps_master_last_and_rejects_unknown():
    ready, _ = series.select_nodes(FakeKube(CLUSTER), "nodes", ["master1", "w2"])
    assert [n.name for n in ready] == ["w2", "master1"]
    ready, _ = series.select_nodes(FakeKube(CLUSTER), "nodes", ["w1", "w1"])
    assert [n.name for n in ready] == ["w1"]                              # duplicates removed
    with pytest.raises(ValueError, match="Unknown nodes: nobody"):
        series.select_nodes(FakeKube(CLUSTER), "nodes", ["nobody"])


# ---------------- plan and master limits ----------------------------------------------------------

def _template(**kw):
    base = dict(node="*", duration=300, cooldown=60, log=True)
    base.update(kw)
    return StressConfig(**base)


def _stepped_template():
    return _template(profile="stepped", duration=720)


def test_plan_config_caps_master_but_not_workers():
    cfg, msgs = series.plan_config(_stepped_template(), node("master1", master=True))
    assert cfg.node == "master1" and cfg.steps == (25, 50, 70) and cfg.duration == 540 and msgs
    cfg, msgs = series.plan_config(_stepped_template(), node("w1"))
    assert cfg.steps == (25, 50, 75, 100) and cfg.duration == 720 and msgs == []
    cfg, msgs = series.plan_config(_template(), node("master1", master=True))       # classic
    assert cfg.cpu_load == 70 and any("limited" in m for m in msgs)


def test_plan_config_does_not_change_the_template():
    template = _stepped_template()
    series.plan_config(template, node("master1", master=True))
    assert template.steps == (25, 50, 75, 100) and template.duration == 720


def test_plan_lines_estimate_workloads_and_master_warning():
    work = {"w1": NodeWorkload((("minecraft", "mc-1"), ("minecraft", "mc-2")), 4),
            "w2": NodeWorkload((), 3)}
    lines = "\n".join(series.plan_lines(_stepped_template(),
                                        [node("w1"), node("w2"), node("master1", master=True)], work))
    assert "Test 3 nodes one after another" in lines and "stepped 25/50/75/100 % for 3 min" in lines
    assert "1. w1: about 14 min 30 s" in lines                              # 90 + 720 + 60
    assert "3. master1 (MASTER): about 11 min 30 s" in lines                # 90 + 540 + 60
    assert "stages limited to at most 70 %: 25/50/70" in lines
    assert "services: minecraft (2): mc-1, mc-2" in lines
    assert "only system pods (3× kube-system)" in lines
    assert "Estimate in total: about 40 min 30 s" in lines


def test_plan_lines_single_node_and_classic_profile():
    lines = "\n".join(series.plan_lines(_template(), [node("w1")]))
    assert "Test of 1 node." in lines and "Profile: classic, CPU load 100 % for 5 min" in lines


# ---------------- choice of scope and master in the stepped test -----------------------------------------

def ns(**kw):
    base = dict(workers=False, cluster=False, nodes=None, node=None, non_interactive=True,
                yes=False, include_master=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_choose_scope_from_flags():
    assert _choose_scope(ns(workers=True)) == "workers"
    assert _choose_scope(ns(cluster=True)) == "cluster"
    assert _choose_scope(ns(nodes="a,b")) == "nodes"
    assert _choose_scope(ns(node="x")) == "single"
    assert _choose_scope(ns()) == "single"                                 # non-interactive without a node


def test_choose_scope_rejects_conflicts():
    with pytest.raises(ValueError, match="only one"):
        _choose_scope(ns(workers=True, cluster=True))
    with pytest.raises(ValueError, match="cannot be combined"):
        _choose_scope(ns(workers=True, node="x"))


def test_keep_master_rules(capsys):
    stepped, classic = _stepped_template(), _template()
    assert _keep_master(ns(), classic) is True                             # classic: the overview confirms
    assert _keep_master(ns(include_master=True), stepped) is True
    assert _keep_master(ns(yes=True), stepped) is True
    assert _keep_master(ns(), stepped) is False                            # non-interactive without confirmation
    assert "Master left out of the stepped test" in capsys.readouterr().out


def test_keep_master_asks_in_interactive_mode(monkeypatch):
    asked = []
    monkeypatch.setattr("stress_test.cli.ask_yes_no", lambda q, *a, **k: asked.append(q) or True)
    assert _keep_master(ns(non_interactive=False), _stepped_template()) is True
    assert len(asked) == 1 and "25/50/70" in asked[0] and "70 %" in asked[0]


# ---------------- series summary ---------------------------------------------------------------------------

def _samples(peak, stage=0):
    s = [Sample(t=i * 5, phase="test", cpu_temp=peak - 10 + i, freq_mhz=3000, cpu_pct=100.0,
                stage=stage) for i in range(11)]
    return s + [Sample(t=60 + i * 5, phase="cooldown", cpu_temp=peak - 20 - i, freq_mhz=3000,
                       cpu_pct=3.0) for i in range(1, 8)]


def _outcome(name, code, peak=None, master=False, reason=""):
    stats = run_stats(_samples(peak), 40, 80) if peak else None
    return series.NodeOutcome(name, master, code, reason, stats,
                              [StressMetric("cpu", 1, 1.0, 2646.7)] if peak else [],
                              f"/logs/{name}.log" if peak else None)


def test_aggregate_code_order():
    ok, hot = _outcome("a", EXIT_OK, 60), _outcome("b", EXIT_OVERHEAT, 90)
    assert series.aggregate_code([ok, ok]) == EXIT_OK
    assert series.aggregate_code([ok, hot]) == EXIT_OVERHEAT
    assert series.aggregate_code([ok, _outcome("c", EXIT_PREMATURE, 60)]) == EXIT_ERROR
    assert series.aggregate_code([hot, _outcome("d", EXIT_ERROR)]) == EXIT_OVERHEAT
    assert series.aggregate_code([hot, _outcome("e", EXIT_INTERRUPTED, 50)]) == EXIT_INTERRUPTED
    assert series.aggregate_code([series.NodeOutcome("skip", reason="x")]) == EXIT_OK
    assert series.aggregate_code([]) == EXIT_OK


def test_node_outcome_status_names():
    assert _outcome("a", EXIT_OK).status == "OK" and _outcome("a", EXIT_OVERHEAT).status == "OVERHEATED"
    assert _outcome("a", EXIT_PREMATURE).status == "PREMATURE"
    assert _outcome("a", EXIT_ERROR).status == "ERROR" and _outcome("a", EXIT_INTERRUPTED).status == "INTERRUPTED"
    assert series.NodeOutcome("a").status == "SKIPPED" and _outcome("a", 42).status == "CODE 42"


def test_cluster_summary_table():
    outcomes = [_outcome("dell", EXIT_OK, 84), _outcome("g2", EXIT_OK, 49),
                _outcome("g6", EXIT_OK, 60, master=True),
                series.NodeOutcome("g4", reason="node is not Ready"),
                series.NodeOutcome("w9", reason="not tested (series interrupted)")]
    text = "\n".join(series.format_cluster_summary(outcomes))
    assert "CLUSTER SUMMARY" in text and "throttling" in text and "return to idle" in text
    dell = next(l for l in text.splitlines() if l.startswith("dell"))
    assert "OK" in dell and "84" in dell and "2646.7" in dell
    assert "g6 (master)" in text
    assert "g4" in text and "SKIPPED" in text and "node is not Ready" in text
    assert "not tested (series interrupted)" in text
    assert "Hottest: dell (84 °C)." in text
    assert "dell: /logs/dell.log" in text and "g4:" not in text.split("Node logs:")[1]


def test_cluster_summary_lists_temperatures_per_stage_for_stepped():
    samples = []
    for stage, temp in ((1, 47), (2, 58), (3, 70), (4, 84)):
        samples += [Sample(t=(stage * 10 + i) * 5, phase="test", cpu_temp=temp, freq_mhz=3000,
                           cpu_pct=stage * 25.0, stage=stage) for i in range(3)]
    stats = run_stats(samples, 40, 80, stage_targets=(25, 50, 75, 100))
    o = series.NodeOutcome("dell", False, EXIT_OK, stats=stats, log_path="/l/d.log")
    text = "\n".join(series.format_cluster_summary([o]))
    assert "Temperatures per stage (max):" in text
    assert "25 % → 47 °C | 50 % → 58 °C | 75 % → 70 °C | 100 % → 84 °C" in text
