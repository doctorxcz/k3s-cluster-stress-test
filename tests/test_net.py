"""Network test (iperf3 + ping): parsing, command, config, findings, run against the fake kubectl, baseline."""
import json

import pytest

from stress_test import baseline, cli, series
from stress_test.logparse import parse_log
from stress_test.manifests import build_stress_command, net_server_pod, stress_pod
from stress_test.models import PROFILE_NET, PodNames, StressConfig
from stress_test.net import (NetResult, build_net_command, describe, findings, job_names, link_errors,
                             net_duration, net_port, parse_iperf, parse_payload, parse_ping, parse_result_line)
from stress_test.parallel import child_args
from test_integration import run_tool

PING = ("50 packets transmitted, 50 received, 0% packet loss, time 9812ms\n"
        "rtt min/avg/max/mdev = 0.207/0.964/1.582/0.541 ms")
TCP = {"end": {"sum_sent": {"retransmits": 3}, "sum_received": {"bits_per_second": 941.2e6}}}
UDP = {"end": {"sum": {"bits_per_second": 100e6, "jitter_ms": 0.0421, "lost_percent": 0.5}}}
NODES = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "peer-node", "ip": "10.0.0.2"}])


def _cfg(**kw):
    base = dict(node="n", profile=PROFILE_NET, net_time=10, duration=net_duration(10))
    base.update(kw)
    return StressConfig(**base)


# ---------------- parsing --------------------------------------------------------------------------------

def test_parse_ping_real_output():
    r = parse_ping(PING)
    assert r.values == {"loss_pct": 0.0, "min_ms": 0.207, "avg_ms": 0.964, "max_ms": 1.582, "jitter_ms": 0.541}
    total = parse_ping("50 packets transmitted, 0 received, 100% packet loss, time 10000ms")
    assert total.values == {"loss_pct": 100.0} and "no reply" in describe(total)
    assert parse_ping("garbage") is None


def test_parse_iperf_tcp_and_udp():
    assert parse_iperf("tcp-up", json.dumps(TCP)).values == {"mbps": 941.2, "retrans": 3}
    assert parse_iperf("udp", json.dumps(UDP)).values == {"mbps": 100.0, "jitter_ms": 0.042, "loss_pct": 0.5}
    assert parse_iperf("tcp-up", "not json") is None and parse_iperf("tcp-up", '{"error": "x"}') is None


def test_parse_payload_dispatch_and_kv():
    link = parse_payload("link", "if=eno1 speed=1000 duplex=full rx_errors=0 rx_dropped=4 tx_errors=0 tx_dropped=0")
    assert link.values["if"] == "eno1" and link.values["speed"] == 1000 and link.values["rx_dropped"] == 4
    assert parse_payload("mtu", "mtu=1450").values == {"mtu": 1450}
    assert parse_payload("ping", PING).name == "ping"


def test_log_line_roundtrip():
    r = NetResult("tcp-up", {"mbps": 941.2, "retrans": 3})
    back = parse_result_line(r.log_line())
    assert back.name == "tcp-up" and back.values == {"mbps": 941.2, "retrans": 3}
    assert parse_result_line("[10:00:00] CPU: 1%") is None


def test_link_errors_delta():
    a = NetResult("link", {"rx_errors": 1, "rx_dropped": 5, "tx_errors": 0, "tx_dropped": 0})
    b = NetResult("link-end", {"rx_errors": 4, "rx_dropped": 5, "tx_errors": 0, "tx_dropped": 2})
    e = link_errors(a, b)
    assert e.values == {"rx_errors": 3, "rx_dropped": 0, "tx_errors": 0, "tx_dropped": 2} and "rx_errors +3" in describe(e)


# ---------------- findings -------------------------------------------------------------------------------

def _res(**named):
    return [NetResult(n, v) for n, v in named.items()]


def test_findings_flags_slow_link_loss_and_asymmetry():
    notes = findings(_res(link={"speed": 100, "duplex": "half"}, ping={"loss_pct": 2.0, "jitter_ms": 3.0},
                          **{"tcp-up": {"mbps": 90.0}, "tcp-down": {"mbps": 30.0}}))
    text = " ".join(notes)
    assert "only 100 Mb/s" in text and "half duplex" in text and "lost 2 %" in text
    assert "jitter" in text and "directions differ" in text


def test_findings_clean_run_is_empty():
    assert findings(_res(link={"speed": 1000, "duplex": "full"}, ping={"loss_pct": 0.0, "jitter_ms": 0.1},
                         **{"tcp-up": {"mbps": 940.0, "retrans": 2}, "tcp-down": {"mbps": 930.0}})) == []


# ---------------- command, pods --------------------------------------------------------------------------

def test_job_names_by_mode():
    assert job_names("host")[0] == "link" and job_names("host")[-1] == "link-end" and len(job_names("host")) == 8
    assert job_names("pod") == ("ping", "mtu", "tcp-up", "tcp-down", "tcp-x4", "udp")


def test_build_net_command_host_and_rate_cap():
    cmd = build_net_command("10.0.0.2", 5210, "host", 10)
    assert "NET-JOB $i/8" in cmd and "iperf3 -c 10.0.0.2 -p 5210 -t 10 -J" in cmd and "-R" in cmd and "-P 4" in cmd
    assert "-u -b 100M" in cmd and "linkinfo" in cmd and "NET-DONE" in cmd
    pod = build_net_command("10.42.1.5", 5210, "pod", 10, 300)
    assert "NET-JOB $i/6" in pod and "linkinfo" not in pod and " -b 300M" in pod and "-u -b 100M" in pod


def test_stress_command_dispatch_and_pods():
    cfg = _cfg(net_mode="pod")
    assert build_stress_command(cfg, None, "1.2.3.4", 5300) == build_net_command("1.2.3.4", 5300, "pod", 10, 0)
    client = stress_pod("n", PodNames.new(), 900, "echo", package="iperf3 iputils-ping", host_network=True)
    assert client["spec"]["hostNetwork"] is True and client["spec"]["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert "iperf3 iputils-ping" in client["spec"]["containers"][0]["command"][-1]
    names = PodNames.new()
    server = net_server_pod("peer", names, 900, 5210, host_network=True)
    assert server["metadata"]["name"] == names.hw and server["spec"]["nodeName"] == "peer"
    assert "iperf3 -s --forceflush -p 5210" in server["spec"]["containers"][0]["command"][-1] and server["spec"]["hostNetwork"]
    assert "hostNetwork" not in net_server_pod("peer", names, 900, 5210, host_network=False)["spec"]


def test_net_port_is_stable_and_safe():
    assert net_port("a1b2c3") == net_port("a1b2c3") and 32000 <= net_port("ffffff") < 32700
    assert net_port("not-hex") == 32000
    assert net_port("not-hex", avoid={32000, 32001}) == 32002              # skips ports used by NodePort Services
    assert 30000 <= net_port("abcdef") <= 32767                            # always inside the NodePort range


# ---------------- config, menu, subprocess ---------------------------------------------------------------

def test_config_validation_and_master_cap():
    _cfg().validate()
    for kw in ({"net_time": 2}, {"net_mode": "wifi"}, {"net_rate": -1}, {"duration": 999}, {"ram_pct": 5}):
        with pytest.raises(ValueError):
            _cfg(**kw).validate()
    cfg = _cfg()
    assert any("network test limited" in m for m in cfg.apply_master_limits()) and cfg.net_rate == 300
    cfg.validate()


def _args(*extra):
    return cli.build_parser().parse_args(["--node", "n", "--profile", "net", "--max-temp", "85", "--cooldown", "60",
                                          "--no-background", "--notes", "", "--non-interactive", *extra])


def test_build_config_net():
    cfg = cli.build_config(_args("--net-time", "20", "--net-peer", "peer-node", "--net-mode", "pod",
                                 "--net-rate", "500"), "n", master_mode=False)
    assert (cfg.net, cfg.net_time, cfg.net_peer, cfg.net_mode, cfg.net_rate) == (True, 20, "peer-node", "pod", 500)
    assert cfg.duration == net_duration(20) and cfg.log
    cfg.validate()
    default = cli.build_config(_args(), "n", master_mode=False)
    assert default.net_time == 10 and default.net_mode == "host" and default.net_peer == ""


def test_ask_profile_net(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": "5")
    assert cli.ask_profile() == PROFILE_NET


def test_child_args_net_roundtrip():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    a = child_args(_cfg(net_peer="peer", net_mode="pod", net_rate=200), opts, "/l/w.log", 1)
    child = cli.build_config(cli.build_parser().parse_args(a), "w", master_mode=False)
    assert (child.net, child.net_peer, child.net_mode, child.net_rate, child.duration) == (True, "peer", "pod", 200, net_duration(10))


# ---------------- log + baseline -------------------------------------------------------------------------

def _log(mbps, ping_ms, name):
    text = ("=== KUBERNETES STRESS-NG LOG ===\nNode: n1\nStarted: 2026-09-29 10:00:00\n"
            "Profile: network - iperf3 + ping, host network, 10 s per test\nNetwork peer: p (10.0.0.2), mode host\n"
            "[10:00:00] CPU: 5%, RAM: 100 MiB (1%) | Temp: CPU: 50°C, GPU: 50°C | Clock: 3000 MHz\n"
            + NetResult("ping", {"loss_pct": 0.0, "avg_ms": ping_ms, "min_ms": 0.1, "max_ms": 1.0, "jitter_ms": 0.1}).log_line() + "\n"
            + NetResult("tcp-up", {"mbps": mbps, "retrans": 1}).log_line() + "\n")
    return parse_log(text, name)


def test_parse_log_reads_net_profile_and_results():
    run = _log(940.0, 0.4, "a.log")
    assert run.profile == "net" and run.net_peer.startswith("p (10.0.0.2)")
    assert [r.name for r in run.net_results] == ["ping", "tcp-up"]


def test_baseline_flags_slower_network(tmp_path):
    baseline.save_baseline(_log(940.0, 0.4, "a.log"), tmp_path)
    assert baseline.check_run(_log(900.0, 0.45, "b.log"), tmp_path)["status"] == "ok"
    res = baseline.check_run(_log(500.0, 2.0, "c.log"), tmp_path)
    assert res["status"] == "regression"
    text = " ".join(res["warnings"])
    assert "tcp-up throughput lower" in text and "ping latency higher" in text


# ---------------- against the fake kubectl ---------------------------------------------------------------

def _run(tmp_path, *extra, env=None):
    env_all = {"FAKE_NODES": NODES, "FAKE_RUN": "5", **(env or {})}
    return run_tool(tmp_path, "--profile", "net", "--net-time", "5", *extra, env_extra=env_all)


def test_run_network_test_against_fake(tmp_path):
    res = _run(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Network peer: peer-node (10.0.0.2:" in res.stdout and "NETWORK TEST (iperf3 + ping)" in res.stdout
    assert "940.0 Mbit/s" in res.stdout and "Nothing suspicious" in res.stdout and "Test completed" in res.stdout
    log = next((tmp_path / "logs").glob("fake-node-*.log")).read_text(encoding="utf-8")
    assert "Network peer: peer-node (10.0.0.2), mode host" in log and log.count("Net result:") >= 8
    data = json.loads(next((tmp_path / "logs").glob("*.json")).read_text())
    assert data["profile"] == "net" and any(r["name"] == "tcp-up" for r in data["net_results"])


def test_run_network_findings_are_reported(tmp_path):
    res = _run(tmp_path, env={"FAKE_NET_SPEED": "100", "FAKE_NET_ERR": "7", "FAKE_NET_LOSS": "4"})
    assert res.returncode == 0
    assert "negotiated only 100 Mb/s" in res.stdout and "rx_errors +7" in res.stdout and "lost 4 %" in res.stdout


def test_run_network_pod_mode_has_no_link_jobs(tmp_path):
    res = _run(tmp_path, "--net-mode", "pod")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "10.42.0.9" in res.stdout and "Network job 1/6 ping" in res.stdout


def test_run_network_job_failure(tmp_path):
    res = _run(tmp_path, env={"FAKE_NET_FAIL": "tcp-down"})
    assert res.returncode != 0 and "network job" in res.stdout and "Test completed" not in res.stdout


def test_run_network_needs_a_peer(tmp_path):
    res = run_tool(tmp_path, "--profile", "net", "--net-time", "5", env_extra={"FAKE_RUN": "5"})
    assert res.returncode != 0 and "No other Ready node" in res.stdout


def test_run_network_server_failure(tmp_path):
    res = _run(tmp_path, env={"FAKE_NET_SERVER_FAIL": "1"})
    assert res.returncode != 0 and "iperf3 could not be installed" in res.stdout


def test_run_network_peer_is_not_the_tested_node(tmp_path):
    res = _run(tmp_path, "--net-peer", "fake-node")
    assert res.returncode != 0 and "different node" in res.stdout


def test_findings_ignore_a_few_dropped_packets_but_not_errors():
    few = NetResult("link-errors", {"rx_errors": 0, "rx_dropped": 2, "tx_errors": 0, "tx_dropped": 0})
    assert findings([few]) == []
    many = NetResult("link-errors", {"rx_errors": 0, "rx_dropped": 50, "tx_errors": 0, "tx_dropped": 0})
    assert "rx_dropped +50" in " ".join(findings([many]))
    err = NetResult("link-errors", {"rx_errors": 1, "rx_dropped": 0, "tx_errors": 0, "tx_dropped": 0})
    assert "rx_errors +1" in " ".join(findings([err]))
