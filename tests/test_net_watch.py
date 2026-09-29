"""--net-watch: latency to another node during ANY test (probe ping, log line, summary)."""
import json

from stress_test.logparse import parse_log
from stress_test.models import ProbeData, Sample
from stress_test.monitor import format_reading
from stress_test.parallel import child_args
from stress_test import series
from stress_test.models import StressConfig
from stress_test.parsing import parse_probe_output
from stress_test.summary import build_summary, run_stats
from test_compare import make_log
from test_integration import run_tool

NODES = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "master-1", "ip": "10.0.0.9", "master": True},
                    {"name": "worker-2", "ip": "10.0.0.2"}])


def test_parse_probe_ping_lines():
    assert parse_probe_output("ping_ms 0.42").ping_ms == 0.42
    p = parse_probe_output("ping_ms lost")
    assert p.ping_lost and p.ping_ms is None
    assert parse_probe_output("freq 3000").ping_ms is None


def test_format_reading_and_parse_back():
    ok = format_reading(50.0, ProbeData(temps={"CPU": 60}, freq_mhz=3000, ping_ms=0.4), now="10:00:00", power_w=12.0)
    assert ok.endswith("| Power: 12.0 W | Ping: 0.40 ms")
    lost = format_reading(50.0, ProbeData(freq_mhz=3000, ping_lost=True), now="10:00:01")
    assert lost.endswith("Clock: 3000 MHz | Ping: lost")
    run = parse_log("=== KUBERNETES STRESS-NG LOG ===\nNode: n\nNetwork watch: m (10.0.0.9)\n" + ok + "\n" + lost + "\n")
    assert run.net_watch == "m (10.0.0.9)"
    assert run.samples[0].ping_ms == 0.4 and run.samples[0].power_w == 12.0 and not run.samples[0].ping_lost
    assert run.samples[1].ping_lost and run.samples[1].ping_ms is None


def test_old_logs_have_no_ping():
    run = parse_log(make_log((50, 55), (52,), node="n"), "n.log")
    assert all(s.ping_ms is None and not s.ping_lost for s in run.samples)


def _samples(values):
    return [Sample(t=float(i * 5), phase="test", cpu_temp=60, freq_mhz=3000, cpu_pct=99.0,
                   ping_ms=None if v is None else v, ping_lost=v is None) for i, v in enumerate(values)]


def test_run_stats_ping():
    st = run_stats(_samples([0.4, 0.6, None, 2.0]), None)
    assert st.ping_n == 4 and st.ping_lost == 1 and st.ping_max == 2.0 and abs(st.ping_avg - 1.0) < 1e-9


def test_summary_reports_loss_and_latency_rise():
    base = ProbeData(temps={"CPU": 40}, ping_ms=0.4)
    lost = "\n".join(build_summary(_samples([0.5, None, 0.6]), base, []))
    assert "Network latency (test)" in lost and "lost 1 of 3" in lost and "disturbs the network" in lost
    rise = "\n".join(build_summary(_samples([5.0, 6.0]), base, []))
    assert "rose from 0.40 ms" in rise
    calm = "\n".join(build_summary(_samples([0.5, 0.6]), base, []))
    assert "Network latency (test)" in calm and "disturbs" not in calm and "rose" not in calm
    assert "Network latency" not in "\n".join(build_summary([Sample(t=0.0, phase="test", cpu_temp=50)], base, []))


def test_child_args_pass_net_watch():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    assert "--net-watch" in child_args(StressConfig(node="w", net_watch="auto"), opts, "/l/w.log", 1)
    assert "--net-watch" not in child_args(StressConfig(node="w"), opts, "/l/w.log", 1)


def test_run_with_net_watch_logs_and_summarizes(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--net-watch",
                   env_extra={"FAKE_NODES": NODES, "FAKE_RUN": "4", "FAKE_PING_LOAD": "5.0"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Network watch: pinging master-1 (10.0.0.9)" in res.stdout and "Ping: 5.00 ms" in res.stdout
    assert "Network latency (test)" in res.stdout and "rose from 0.40 ms" in res.stdout
    log = next((tmp_path / "logs").glob("fake-node-5s-*.log")).read_text(encoding="utf-8")
    assert "Network watch: master-1 (10.0.0.9)" in log


def test_net_watch_lost_pings_under_load(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--net-watch", "worker-2",
                   env_extra={"FAKE_NODES": NODES, "FAKE_RUN": "4", "FAKE_PING_LOST_UNDER_LOAD": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "worker-2 (10.0.0.2)" in res.stdout and "Ping: lost" in res.stdout and "disturbs the network" in res.stdout


def test_net_watch_without_other_node_continues(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--net-watch", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "no other node with an address" in res.stdout and "Network latency" not in res.stdout


def test_net_watch_itself_is_ignored(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--net-watch", "fake-node", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0 and "the tested node itself" in res.stdout
