"""Network matrix (--net-matrix): parsing, findings, table and a run against the fake kubectl."""
import json

import pytest

from stress_test import netmatrix as mx
from stress_test.models import NodeInfo
from test_integration import run_tool

NODES3 = json.dumps([{"name": "n1", "ip": "10.0.0.1"}, {"name": "n2", "ip": "10.0.0.2"},
                     {"name": "m1", "ip": "10.0.0.3", "master": True}])
PING = ("MX-PING 10 packets transmitted, 10 received, 0% packet loss, time 1800ms "
        "rtt min/avg/max/mdev = 0.2/0.4/0.8/0.1 ms ")
IPERF = "MX-IPERF " + json.dumps({"end": {"sum_sent": {"retransmits": 2}, "sum_received": {"bits_per_second": 930e6}}})


def test_parse_pair_output_ok_and_failed():
    p = mx.parse_pair_output("a", "b", PING + "\n" + IPERF)
    assert (p.mbps, p.retrans, p.ping_avg_ms, p.ping_loss_pct, p.error) == (930.0, 2, 0.4, 0.0, "")
    bad = mx.parse_pair_output("a", "b", "MX-PING 10 packets transmitted, 0 received, 100% packet loss\nMX-IPERF iperf3: error")
    assert bad.mbps is None and "iperf3 failed" in bad.error and bad.ping_loss_pct == 100.0
    assert mx.parse_pair_output("a", "b", "").error == "no result"


def test_scripts_and_pod():
    assert "ping -c 10 -i 0.2 -q 10.0.0.2" in mx.ping_script("10.0.0.2")
    s = mx.iperf_script("10.0.0.1", 32102, 5, 300)
    assert "iperf3 -c 10.0.0.1 -p 32102 -t 5 -R -J -b 300M" in s          # -R: the data flows towards this node
    pod = mx.matrix_pod("n1", "net-mx-0-abc", 900, "abc")
    assert pod["spec"]["hostNetwork"] and pod["spec"]["nodeName"] == "n1" and pod["metadata"]["labels"]["role"] == "netmx"


def _result(mbps):
    """3 nodes; mbps maps 'a>b' to the throughput (default 940)."""
    r = mx.MatrixResult(["a", "b", "c"], 5, links={n: {"if": "eth0", "speed": 1000, "duplex": "full"} for n in "abc"})
    for c in "abc":
        for s in "abc":
            if c != s:
                r.pairs.append(mx.Pair(c, s, mbps.get(f"{c}>{s}", 940.0), 1, 0.4, 0.0))
    return r


def test_findings_clean_matrix():
    assert mx.findings(_result({})) == []
    assert "Nothing suspicious" in "\n".join(mx.format_matrix(_result({})))


def test_findings_slow_pair_asymmetry_and_weak_node():
    notes = mx.findings(_result({"a>b": 300.0, "b>a": 930.0, "c>a": 300.0, "a>c": 300.0}))
    text = " ".join(notes)
    assert "a -> b is slow" in text and "directions differ" in text and "a is the weak spot" in text


def test_findings_link_loss_and_skipped():
    r = _result({})
    r.links["b"] = {"if": "eth0", "speed": 100, "duplex": "half"}
    r.pairs[0].ping_loss_pct = 3.0
    r.skipped["z"] = "helper pod did not start"
    text = " ".join(mx.findings(r))
    assert "only 100 Mb/s" in text and "half duplex" in text and "lost 3 %" in text and "z was not tested" in text


def test_format_matrix_shape():
    lines = mx.format_matrix(_result({"a>b": 500.0}))
    text = "\n".join(lines)
    assert "[1] a" in text and "Throughput, Mbit/s" in text and "Ping, ms" in text
    row = next(line for line in lines if line.startswith("  [1] ") and "500" in line)
    assert row.split()[1] == "-" and "500" in row


def test_matrix_dict_is_json():
    data = mx.matrix_to_dict(_result({}))
    json.dumps(data)
    assert data["kind"] == "net-matrix" and len(data["pairs"]) == 6


# ---------------- against the fake kubectl ---------------------------------------------------------------

def _run(tmp_path, *extra, env=None):
    env_all = {"FAKE_NODES": NODES3, **(env or {})}
    return run_tool(tmp_path, "--net-matrix", "--net-time", "5", *extra, env_extra=env_all)


def test_matrix_refuses_without_confirmation(tmp_path):
    res = _run(tmp_path)
    assert res.returncode != 0 and "Cancelled" in res.stdout and not list((tmp_path / "logs").glob("net-matrix-*"))


def test_matrix_runs_and_saves(tmp_path):
    res = _run(tmp_path, "--yes")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "NETWORK MATRIX" in res.stdout and "Nothing suspicious" in res.stdout and "[6/6]" in res.stdout
    files = {p.suffix for p in (tmp_path / "logs").glob("net-matrix-*")}
    assert files == {".log", ".json"}
    data = json.loads(next((tmp_path / "logs").glob("net-matrix-*.json")).read_text())
    assert len(data["pairs"]) == 6 and all(p["mbps"] == 940.0 or p["client"] == "m1" or p["server"] == "m1" for p in data["pairs"])


def test_matrix_finds_a_slow_pair_and_a_bad_link(tmp_path):
    four = json.dumps([{"name": "n1", "ip": "10.0.0.1"}, {"name": "n2", "ip": "10.0.0.2"},
                       {"name": "n3", "ip": "10.0.0.4"}, {"name": "m1", "ip": "10.0.0.3", "master": True}])
    res = _run(tmp_path, "--yes", env={"FAKE_NODES": four, "FAKE_MX_MBPS": json.dumps({"n1>n2": 200.0}),
                                       "FAKE_MX_SPEED": json.dumps({"n2": 100})})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "n1 -> n2 is slow" in res.stdout and "n2: the network card negotiated only 100 Mb/s" in res.stdout


def test_matrix_failed_pair_and_skipped_node(tmp_path):
    res = _run(tmp_path, "--yes", env={"FAKE_MX_FAIL": "n1>n2", "FAKE_MX_NOT_READY": "m1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "m1: helper pod did not start" in res.stdout and "n1 -> n2: iperf3 failed" in res.stdout
    assert "[2/2]" in res.stdout                              # only 2 nodes remain -> 2 pairs


def test_matrix_needs_two_nodes(tmp_path):
    res = run_tool(tmp_path, "--net-matrix", "--yes", env_extra={})
    assert res.returncode != 0 and "at least two Ready nodes" in res.stdout


def test_matrix_refuses_while_a_test_runs(tmp_path):
    other = json.dumps([{"name": "stress-test-abc123", "node": "n1", "phase": "Running", "run": "abc123"}])
    res = _run(tmp_path, "--yes", env={"FAKE_OTHER_PODS": other})
    assert res.returncode != 0 and "running right now" in res.stdout


def test_capped_pairs_are_not_reported_as_slow():
    r = _result({})
    for p in r.pairs:
        if "c" in (p.client, p.server):
            p.mbps, p.capped = 300.0, 300
    assert mx.findings(r) == []
    text = "\n".join(mx.format_matrix(r))
    assert "300*" in text and "capped on purpose" in text
