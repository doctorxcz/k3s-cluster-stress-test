"""Network test extras: dns, internet, mtr, service (Kubernetes Service), Wi-Fi signal."""
import json

import pytest

from stress_test import cli
from stress_test.manifests import net_service
from stress_test.models import PROFILE_NET, StressConfig
from stress_test.net import (NetResult, build_net_command, describe, findings, job_names, net_duration,
                             normalize_extras, packages, parse_mtr, parse_payload)
from stress_test.parallel import child_args
from stress_test import series
from test_integration import run_tool

NODES = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "peer-node", "ip": "10.0.0.2"}])
MTR = ("Start: 2026-09-29T14:00:00+0200;HOST: x            Loss%   Snt   Last   Avg  Best  Wrst StDev;"
       "  1.|-- 192.168.88.1   0.0%     5    0.4   0.5   0.3   0.9   0.2;"
       "  2.|-- 10.0.0.2       0.0%     5    0.6   0.7   0.5   1.1   0.2;")


def test_normalize_extras():
    assert normalize_extras(["mtr", "dns"]) == ("dns", "mtr")
    assert normalize_extras(["all"]) == ("dns", "internet", "mtr", "service")
    assert normalize_extras(None) == () and normalize_extras([]) == ()
    with pytest.raises(ValueError, match="Unknown network extra"):
        normalize_extras(["bogus"])


def test_job_names_and_duration_with_extras():
    assert job_names("host", ["dns"])[-2:] == ("dns", "link-end")
    assert job_names("pod", ["service"])[-1] == "tcp-svc" and "tcp-svc" not in job_names("host", ["service"])
    assert net_duration(10, ["dns", "internet", "mtr"]) == net_duration(10) + 3 + 12 + 5
    assert net_duration(10, ["service"], "pod") == net_duration(10) + 10 and net_duration(10, ["service"], "host") == net_duration(10)
    assert packages(()) == "iperf3 iputils-ping" and packages(["internet", "mtr"]) == "iperf3 iputils-ping curl mtr-tiny"


def test_parse_dns_internet_mtr():
    dns = parse_payload("dns", "cluster_ms=1.20 external_ms=4.50 fails=0")
    assert dns.values == {"cluster_ms": 1.2, "external_ms": 4.5, "fails": 0} and "average of 10" in describe(dns)
    ping = parse_payload("internet-ping", "5 packets transmitted, 5 received, 0% packet loss, time 800ms rtt min/avg/max/mdev = 5.0/6.0/7.0/0.8 ms")
    assert ping.values["avg_ms"] == 6.0
    down = parse_payload("internet-down", "speed=25000000 ttfb=0.056 code=206")
    assert down.values == {"mbps": 200.0, "ttfb_ms": 56.0, "code": 206} and "200.0 Mbit/s" in describe(down)
    assert parse_payload("internet-down", "").values if False else parse_payload("internet-down", "") is None
    mtr = parse_mtr("mtr", MTR)
    assert mtr.values == {"hops": 2, "loss_pct": 0.0, "avg_ms": 0.7, "worst_loss": 0.0}
    assert parse_mtr("mtr", "garbage") is None


def test_failed_download_is_described():
    down = parse_payload("internet-down", "speed=0.000 ttfb=0.000 code=000")
    assert "download failed" in describe(down)


def test_findings_for_extras():
    res = [NetResult("link", {"if": "wlan0", "speed": 300, "duplex": "full", "wifi_dbm": -78}),
           NetResult("dns", {"cluster_ms": 120.0, "external_ms": 500.0, "fails": 2}),
           NetResult("internet-ping", {"loss_pct": 10.0}), NetResult("internet-down", {"mbps": 0.0, "code": 0}),
           NetResult("mtr", {"hops": 1, "loss_pct": 20.0, "avg_ms": 1.0, "worst_loss": 20.0}),
           NetResult("tcp-up", {"mbps": 900.0}), NetResult("tcp-svc", {"mbps": 300.0})]
    text = " ".join(findings(res))
    for expected in ("Wi-Fi signal is weak", "2 DNS lookups failed", "Cluster DNS (CoreDNS) is slow",
                     "External DNS is slow", "internet ping lost 10 %", "internet download test failed",
                     "mtr: the last hop lost 20 %", "Through the Service"):
        assert expected in text


def test_findings_clean_extras():
    res = [NetResult("dns", {"cluster_ms": 1.2, "external_ms": 4.5, "fails": 0}),
           NetResult("internet-ping", {"loss_pct": 0.0}), NetResult("internet-down", {"mbps": 200.0, "code": 206}),
           NetResult("tcp-up", {"mbps": 900.0}), NetResult("tcp-svc", {"mbps": 880.0})]
    assert findings(res) == []


def test_command_contains_extras():
    cmd = build_net_command("10.0.0.2", 5210, "pod", 5, 0, ["dns", "internet", "mtr", "service"], "10.43.0.7")
    assert "NET-JOB $i/10" in cmd and "getent hosts kubernetes.default.svc.cluster.local" in cmd
    assert "curl -s -o /dev/null --range 0-49999999" in cmd and "mtr -r -w -n -c 5 10.0.0.2" in cmd
    assert "iperf3 -c 10.43.0.7 -p 5210" in cmd
    without_ip = build_net_command("10.0.0.2", 5210, "pod", 5, 0, ["service"], "")
    assert "tcp-svc" not in without_ip and "NET-JOB $i/6" in without_ip
    host = build_net_command("10.0.0.2", 5210, "host", 5, 0, ["service"], "10.43.0.7")
    assert "tcp-svc" not in host


def test_net_service_manifest():
    svc = net_service("net-svc-abc", "abc", 5210)
    assert svc["kind"] == "Service" and svc["spec"]["selector"] == {"run-id": "abc", "role": "net-server"}
    assert svc["spec"]["ports"][0]["port"] == 5210


def _args(*extra):
    return cli.build_parser().parse_args(["--node", "n", "--profile", "net", "--max-temp", "85", "--cooldown", "60",
                                          "--no-background", "--notes", "", "--non-interactive", *extra])


def test_build_config_extras_and_bad_value():
    cfg = cli.build_config(_args("--net-extra", "dns, mtr", "--net-time", "5"), "n", master_mode=False)
    assert cfg.net_extra == ("dns", "mtr") and cfg.duration == net_duration(5, ("dns", "mtr"))
    cfg.validate()
    pod = cli.build_config(_args("--net-extra", "all", "--net-mode", "pod"), "n", master_mode=False)
    assert pod.duration == net_duration(10, pod.net_extra, "pod")
    pod.validate()
    with pytest.raises(ValueError):
        cli.build_config(_args("--net-extra", "nope"), "n", master_mode=False)


def test_child_args_extras_roundtrip():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    cfg = StressConfig(node="w", profile=PROFILE_NET, net_time=5, net_extra=("dns", "mtr"),
                       duration=net_duration(5, ("dns", "mtr")))
    child = cli.build_config(cli.build_parser().parse_args(child_args(cfg, opts, "/l/w.log", 1)), "w", master_mode=False)
    assert child.net_extra == ("dns", "mtr") and child.duration == cfg.duration


def _run(tmp_path, *extra, env=None):
    return run_tool(tmp_path, "--profile", "net", "--net-time", "5", *extra,
                    env_extra={"FAKE_NODES": NODES, "FAKE_RUN": "6", **(env or {})})


def test_run_with_all_extras_in_pod_mode(tmp_path):
    res = _run(tmp_path, "--net-mode", "pod", "--net-extra", "all")
    assert res.returncode == 0, res.stdout + res.stderr
    for expected in ("cluster name 1.2 ms", "download 200.0 Mbit/s", "hop(s), last hop", "via the Service / kube-proxy",
                     "Nothing suspicious", "Test completed"):
        assert expected in res.stdout
    log = next((tmp_path / "logs").glob("fake-node-*.log")).read_text(encoding="utf-8")
    for name in ("dns", "internet-ping", "internet-down", "mtr", "tcp-svc"):
        assert f"Net result: {name} |" in log


def test_run_service_extra_skipped_in_host_mode(tmp_path):
    res = _run(tmp_path, "--net-extra", "service,dns")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "needs --net-mode pod, skipping" in res.stdout and "via the Service" not in res.stdout


def test_run_reports_slow_service_and_dns(tmp_path):
    res = _run(tmp_path, "--net-mode", "pod", "--net-extra", "dns,service",
               env={"FAKE_NET_SVC_MBPS": "300", "FAKE_DNS_MS": "150", "FAKE_DNS_FAILS": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Through the Service" in res.stdout and "Cluster DNS (CoreDNS) is slow" in res.stdout
    assert "3 DNS lookups failed" in res.stdout


def test_run_service_without_address_continues(tmp_path):
    res = _run(tmp_path, "--net-mode", "pod", "--net-extra", "service", env={"FAKE_NO_SERVICE_IP": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "got no address, skipping the Service test" in res.stdout
