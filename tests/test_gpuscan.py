"""GPU scan: classification of the nodes, the table, the node choice and the scan itself against the fake kubectl."""
import json
import os
import sys

import pytest

from stress_test import gpuscan, menu
from stress_test.gpuscan import (ERROR, NONE, NOT_READY, OK, UNKNOWN, NodeScan, candidates, choose_nodes, classify,
                                 options_for, parse_cards, table_lines)

NV = ("NVIDIA", "NVIDIA Corporation GP107GL [Quadro P620]")
INTEL = ("Intel", "Intel Corporation HD Graphics 4600")


def scan(name="n", gpus=0, cards=(), ok=True, ready=True, master=False):
    return classify(NodeScan(name, master, ready, gpus, list(cards), ok))


def test_parse_cards_vendors_and_done_marker():
    text = ("GPU-CARD Intel Corporation HD Graphics 4600 [8086:0412] (rev 06)\n"
            "GPU-CARD NVIDIA Corporation GP107GL [Quadro P620] [10de:1cb6] (rev a1)\nSCAN-DONE\n")
    cards, done = parse_cards(text)
    assert done and cards[0][0] == "Intel" and cards[1][0] == "NVIDIA" and "[10de" not in cards[1][1]
    assert parse_cards("SCAN-NOLSPCI\n") == ([], False)


@pytest.mark.parametrize("kw, status", [
    (dict(gpus=1, cards=[NV]), OK),
    (dict(gpus=1, cards=[], ok=False), OK),                    # the API says the GPU is usable, even if the scan failed
    (dict(gpus=0, cards=[NV]), ERROR),                         # card there, Kubernetes cannot use it
    (dict(gpus=0, cards=[INTEL]), NONE),                       # integrated only
    (dict(gpus=0, cards=[]), NONE),
    (dict(gpus=0, ok=False), UNKNOWN),                         # cannot tell
    (dict(gpus=1, cards=[NV], ready=False), NOT_READY),
])
def test_classification(kw, status):
    assert scan(**kw).status == status


def test_table_marks_every_case_and_counts():
    scans = [scan("a", 1, [NV]), scan("b", 0, [NV]), scan("c", 0, [INTEL]), scan("d", 0, ok=False)]
    text = "\n".join(table_lines(scans, False, 100))
    assert "✅ OK" in text and "ERROR" in text and "❌ none" in text and "? ERROR" in text
    assert "1 of 4 nodes have a usable dedicated NVIDIA GPU" in text
    assert len({len(line) for line in text.splitlines()}) >= 1


def test_choose_nodes_zero_one_many():
    said = []
    assert choose_nodes([scan("c", 0, [INTEL])], lambda *a: "", said.append) is None and "No node" in said[0]
    assert choose_nodes([scan("a", 1, [NV]), scan("c")], lambda *a: "", said.append) == ["a"]     # one = automatic
    many = [scan("a", 1, [NV]), scan("b", 1, [NV]), scan("c", 1, [NV])]
    assert choose_nodes(many, lambda *a: "a") == ["a", "b", "c"]
    assert choose_nodes(many, lambda *a: "3,1") == ["c", "a"]
    assert choose_nodes(many, lambda *a: "b") == ["b"]
    answers = iter(["9", "x", "2"])
    assert choose_nodes(many, lambda *a: next(answers)) == ["b"]
    assert choose_nodes(many, lambda *a: "0") is None


def test_options_for_one_or_many():
    assert options_for(["a"]) == ["--node", "a"] and options_for(["a", "b"]) == ["--nodes", "a,b"]


def test_default_node_setting_is_not_added_next_to_nodes():
    out = menu.apply_settings(["--nodes", "a,b", "--profile", "gpu"], {"node": "x", "max_temp": 0})
    assert "--node" not in out


def test_scan_nodes_against_the_fake_cluster(tmp_path, monkeypatch):
    here = os.path.dirname(__file__)
    monkeypatch.setenv("KUBECTL", os.path.join(here, "fake_kubectl.py"))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path))
    monkeypatch.setenv("FAKE_NODES", json.dumps([{"name": "gpu1"}, {"name": "plain"}, {"name": "broken"},
                                                  {"name": "noplugin"}, {"name": "m1", "master": True},
                                                  {"name": "down", "ready": False}]))
    monkeypatch.setenv("FAKE_GPU_BY_NODE", json.dumps({"gpu1": "1", "noplugin": "plugin-missing", "broken": "scan-fail"}))
    from stress_test.kube import Kubectl
    scans = {s.name: s for s in gpuscan.scan_nodes(Kubectl(os.path.join(here, "fake_kubectl.py")), lambda _t: None, timeout=30)}
    assert scans["gpu1"].status == OK and scans["plain"].status == NONE and scans["noplugin"].status == ERROR
    assert scans["broken"].status == UNKNOWN and scans["down"].status == NOT_READY and scans["m1"].status == NONE
    # the scan pods are read-only: no privileged, no hostPath
    for path in tmp_path.glob("manifest-hw-info-*.json"):
        text = path.read_text()
        assert "hostPath" not in text and '"privileged"' not in text
