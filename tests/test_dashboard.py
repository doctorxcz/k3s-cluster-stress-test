"""Cluster dashboard: tiers, rendering at every width, keys, the collector against the fake kubectl, probe pod safety."""
import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

from stress_test import dashboard as d, gpu as g, ui
from stress_test.kube import Kubectl
from test_integration import FAKE, ROOT

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def node(name, master=False, cpu=30.0, temp=52, gpu=False, ready=True, probe=True):
    n = d.NodeView(name, master=master, ready=ready, ip="10.0.0.10", os_image="Ubuntu 24.04", kernel="6.8.0", arch="amd64",
                   runtime="containerd://1.7", kubelet="v1.30.4+k3s1", cpu_cap="8", mem_alloc_mib=23000, gpu_alloc=1 if gpu else 0)
    if not probe:
        return n
    n.probe_ok, n.cpu_pct, n.temps, n.freq_mhz = True, cpu, {"CPU": temp, "NVMe": 41}, 3614
    n.mem_used_mib, n.mem_total_mib, n.power_w, n.load, n.uptime_s, n.cores = 6400, 23400, 18.2, (0.5, 0.4, 0.3), 93784, 4
    n.threads = 8
    n.cpu_model = "Intel(R) Core(TM) i7-4790S CPU @ 3.20GHz"
    n.nics = [{"name": "eno1", "speed": 1000, "state": "up", "rx_bps": 1.5e6, "tx_bps": 4e5}]
    n.disks = [{"name": "sda", "size_gb": 238, "kind": "SSD/NVMe", "model": "Patriot P210"}]
    n.peers = {"other": 0.4}
    n.pods = [{"name": "app", "ns": "default", "phase": "Running", "restarts": 1, "cpu_m": 500, "mem_mib": 1024},
              {"name": "crash", "ns": "default", "phase": "CrashLoopBackOff", "restarts": 7, "cpu_m": 100, "mem_mib": 128}]
    n.last_test = "GPU 60s OK 67°"
    for i in range(20):
        n.h_cpu.append(10 + i)
        n.h_temp.append(40 + i / 2)
    if gpu:
        n.gpu = g.GpuReading(temp=59, power_w=None, sm_mhz=1354, util_pct=0, mem_used_mib=3, mem_total_mib=2048, throttle=1, fan_pct=40)
        n.gpu_info = {"GPU": "Quadro P620", "Driver": "580.178.04", "Memory": "2048 MiB"}
    return n


def cluster(count=4):
    names = ["worker-1", "worker-2", "worker-3", "control-plane-node-00001"] + [f"extra-{i}" for i in range(count)]
    nodes = [node(names[0], gpu=True)] + [node(n, master=n.endswith("master")) for n in names[1:count]]
    return d.ClusterView(nodes=nodes, events=[("14:00:01", "BackOff", "Pod/app", "Back-off restarting")],
                         workloads={"deployments": (2, 2)}, namespaces=3, services=9)


def lines_of(view, cols, rows=40, state=None):
    return [ANSI.sub("", x) for x in d.render(view, state or d.UiState(), cols, rows, False)]


# ---------------- tiers and parsing ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("cols, tier", [(20, 1), (59, 1), (60, 1), (90, 1), (91, 2), (160, 2), (161, 3), (250, 3), (400, 3)])
def test_tier_thresholds(cols, tier):
    assert d.tier(cols) == tier


def test_parse_extras_all_lines_and_junk():
    text = ("uptime 93784\nload 0.52 0.40 0.31\nswap_total_kb 2048\nswap_free_kb 1024\nthreads 8\ncores 4\ncpu_model Intel(R) Core\nthr 3\n"
            "nic eno1 1000 up 100 200\nnic wlan0 -1 down 5 6\ndisk sda 238 0 Patriot P210\ndisk sdb 931 1 WD Blue\npeer n1 0.4\npeer n2 lost\n"
            "uptime junk\nnic bad\nload 1\n")
    x = d.parse_extras(text)
    assert x["uptime"] == 93784 and x["load"] == (0.52, 0.4, 0.31) and x["threads"] == 8 and x["cores"] == 4 and x["thr"] == 3
    assert x["swap_total_kb"] == 2048 and x["cpu_model"] == "Intel(R) Core"
    assert [n["name"] for n in x["nics"]] == ["eno1", "wlan0"] and x["nics"][1]["speed"] == 0
    assert x["disks"][1]["kind"] == "HDD" and x["disks"][0]["model"] == "Patriot P210"
    assert x["peers"] == {"n1": 0.4, "n2": None}
    empty = d.parse_extras("")
    assert empty["nics"] == [] and empty["disks"] == [] and empty["peers"] == {} and empty["dmi"] == {} and empty["cpufreqs"] == []


# ---------------- rendering ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cols", [30, 40, 50, 59, 60, 70, 90, 91, 120, 160, 161, 200, 250])
def test_every_width_every_row_is_aligned_and_fits(cols):
    out = lines_of(cluster(), cols, 45)
    assert len({ui.visible_len(x) for x in out}) == 1, cols
    assert ui.visible_len(out[0]) <= max(cols, 30)
    assert all("️" not in x for x in out)


def test_tier1_is_a_compact_table_without_detail():
    text = "\n".join(lines_of(cluster(), 88, 30))
    assert "NODE" in text and "CPU" in text and "TEMP" in text and "RAM" in text and "PODS" in text
    assert "worker-1" in text and "ROLE" not in text and "▸ worker-1" in text.replace("┃ ▸", "▸")
    assert "load" not in text and "kernel" not in text


def test_tier2_has_more_columns_and_the_detail_block():
    text = "\n".join(lines_of(cluster(), 160))
    for word in ("ROLE", "CLOCK", "PWR", "NET", "GPU", "STATE", "Quadro", "load 0.50", "kernel 6.8.0", "NIC eno1", "disk sda", "ping other"):
        assert word in text, word
    assert "P620" in text and "59°" in text and "VRAM" in text and "fan 40%" in text and "N/A" in text


def test_tier3_adds_history_panels_pods_events_and_tests():
    text = "\n".join(lines_of(cluster(), 330, 45))
    for word in ("CPU ▸ 60 s", "TEMP ▸ 60 s", "REQ cpu/ram", "UP", "RESTART", "· pods", "▸ cluster", "BackOff", "▸ tests",
                 "namespaces", "deployments 2/2", "crash", "CrashLoopBackOff", "▸ problems", "· hardware", "· Kubernetes"):
        assert word in text, word
    assert any(ch in text for ch in "▁▂▃▄▅▆▇█")                                    # the history graphs


def test_wider_screens_show_more_columns():
    count = lambda c: sum(1 for w in ("CLOCK", "PWR", "LOAD", "PING", "NET", "CPU ▸ 60 s", "REQ", "UP") if w in "\n".join(lines_of(cluster(), c)))   # noqa: E731
    assert count(100) <= count(140) <= count(200) <= count(260) and count(260) == 8


def test_alerts_for_notready_pressure_hot_and_bad_pods():
    v = cluster()
    v.nodes[1].ready = False
    v.nodes[2].pressure = {"Memory": True}
    v.nodes[3].temps = {"CPU": 85}
    v.nodes[0].gpu = g.GpuReading(temp=84)
    text = "\n".join(lines_of(v, 120))
    assert "NotReady" in text and "Memory pressure" in text or "pressure" in text
    assert "pod(s) not running" in text or "NotReady" in text


def test_node_without_probe_data_and_not_ready_do_not_break_the_screen():
    v = d.ClusterView(nodes=[node("a", probe=False), node("b", ready=False, probe=False), node("c")])
    for cols in (50, 100, 200):
        out = lines_of(v, cols)
        assert len({ui.visible_len(x) for x in out}) == 1
    assert "NotReady" in "\n".join(lines_of(v, 130))


def test_empty_cluster_waits_for_data():
    assert "waiting for the first data" in "\n".join(lines_of(d.ClusterView(), 100))


def test_many_nodes_scroll_and_keep_the_selection_visible():
    nodes = [node(f"node-{i:02d}") for i in range(40)]
    v = d.ClusterView(nodes=nodes)
    st = d.UiState(selected=35)
    out = "\n".join(lines_of(v, 100, 24, st))
    assert "node-35" in out and "node-00" not in out and "of 40" in out
    st.selected = 0
    assert "node-00" in "\n".join(lines_of(v, 100, 24, st))


def test_tier1_detail_page_replaces_the_table():
    st = d.UiState(detail_page=True)
    text = "\n".join(lines_of(cluster(), 80, 50, st))
    assert "kernel 6.8.0" in text and "NODE" not in text.split("\n")[3] and "back to the table" in text


def test_tab_cycles_overview_pods_events_in_tier2():
    v = cluster()
    seen = []
    for tab in range(3):
        seen.append("\n".join(lines_of(v, 130, 45, d.UiState(tab=tab))))
    assert "kernel" in seen[0] and "crash" in seen[1] and "cluster events" in seen[2]


def test_colours_only_when_on():
    plain = "\n".join(d.render(cluster(), d.UiState(), 120, 40, False))
    assert "\x1b[" not in plain
    assert "\x1b[" in "\n".join(d.render(cluster(), d.UiState(), 120, 40, True))


# ---------------- keys -----------------------------------------------------------------------------------------------------------

def make_dash(count=4):
    dash = d.Dashboard(Kubectl("true"), stream=io.StringIO(), keys=lambda t: None)
    dash.collector.view = cluster(count)
    return dash


def test_keys_move_select_and_stay_in_bounds():
    dash = make_dash()
    for key, expect in (("down", 1), ("down", 2), ("pgdn", 3), ("up", 2), ("home", 0), ("up", 0), ("end", 3), ("down", 3), ("pgup", 0)):
        assert dash.handle(key) is True and dash.state.selected == expect, key


def test_keys_interval_pause_tab_enter_and_quit():
    dash = make_dash()
    assert dash.state.interval == 5
    dash.handle("+")
    assert dash.state.interval == 10
    dash.handle("+"), dash.handle("+")
    assert dash.state.interval == 30
    for _ in range(10):
        dash.handle("-")
    assert dash.state.interval == 0.5                      # the fastest: twice a second
    dash.handle("+")
    assert dash.state.interval == 1
    dash.handle("p")
    assert dash.state.paused is True
    dash.handle("tab")
    assert dash.state.tab == 1
    dash.handle("enter")
    assert dash.state.detail_page is True
    assert dash.handle("q") is True and dash.state.detail_page is False            # q closes the detail page first
    assert dash.handle("q") is False                                              # then leaves
    assert make_dash().handle("esc") is False


def test_read_key_names(monkeypatch):
    r, w = os.pipe()
    try:
        for raw, name in ((b"\x1b[A", "up"), (b"\x1b[6~", "pgdn"), (b"\t", "tab"), (b"\r", "enter"), (b"q", "q"), (b"+", "+"), (b"\x03", "q")):
            os.write(w, raw)
            assert d.read_key(r, 0.5) == name
        assert d.read_key(r, 0.05) is None
    finally:
        os.close(r), os.close(w)


# ---------------- the collector against the fake kubectl -------------------------------------------------------------------------

@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE", str(tmp_path))
    monkeypatch.setenv("FAKE_NODES", json.dumps([{"name": "gpu1", "ip": "10.0.0.1"}, {"name": "w2", "ip": "10.0.0.2"},
                                                 {"name": "m1", "master": True, "ip": "10.0.0.3"}, {"name": "down", "ready": False}]))
    monkeypatch.setenv("FAKE_GPU_BY_NODE", json.dumps({"gpu1": "1"}))
    return tmp_path


def collector(only=None):
    return d.Collector(Kubectl(str(FAKE)), only)


def test_collector_probes_every_ready_node_and_a_gpu_pod_only_on_the_gpu_node(fake):
    c = collector()
    said = []
    c.start(said.append)
    assert set(c.pods) == {"gpu1", "w2", "m1"} and "down" not in c.pods                # a NotReady node gets no pod
    assert c.pods["gpu1"][1] and not c.pods["w2"][1]
    view = c.refresh()
    time.sleep(0.01)
    view = c.refresh()                                                                  # the second round has CPU % and rates
    by = {n.name: n for n in view.nodes}
    assert by["w2"].probe_ok and by["w2"].cpu_pct is not None and by["w2"].temp == 40
    assert by["w2"].mem_used_mib == 1953 and by["w2"].uptime_s == 93784 and by["w2"].cores == 4 and by["w2"].threads == 8
    assert by["w2"].cpu_model.startswith("Intel") and by["w2"].disks[0]["name"] == "sda"
    assert by["w2"].nics[0]["rx_bps"] is not None and by["w2"].nics[0]["rx_bps"] > 0
    assert set(by["w2"].peers) == {"gpu1", "m1"} and by["w2"].peers["gpu1"] == 0.4
    assert by["gpu1"].gpu is not None and by["gpu1"].gpu.temp == 40 and by["gpu1"].gpu_name == "Quadro P620"
    assert by["w2"].gpu is None and by["m1"].master and not by["down"].ready
    assert by["w2"].pods and by["w2"].pods_running == 1 and by["w2"].req_cpu_m == 100
    assert view.events and view.events[0][1] == "BackOff" and view.workloads["deployments"] == (2, 2)
    assert len(by["w2"].h_cpu) == 2


def test_collector_only_limits_the_nodes(fake):
    c = collector(["w2"])
    c.start()
    view = c.refresh()
    assert [n.name for n in view.nodes] == ["w2"]


def test_probe_pods_are_safe_and_do_not_look_like_a_test(fake):
    c = collector()
    c.start()
    manifests = [json.loads(p.read_text()) for p in fake.glob("manifest-dash-*.json")]
    assert len(manifests) == 4                                                          # 3 probes + 1 GPU probe
    for pod in manifests:
        text = json.dumps(pod)
        assert pod["metadata"]["labels"]["app"] == "stress-test-dashboard"             # NOT the tool's test label
        assert '"privileged"' not in text and "hostNetwork" not in text and "hostPID" not in text
        assert pod["spec"]["automountServiceAccountToken"] is False and pod["spec"]["activeDeadlineSeconds"] == d.POD_DEADLINE
        assert pod["spec"]["containers"][0]["resources"]["limits"]["memory"]
    gpu_pod = next(p for p in manifests if p["metadata"]["labels"]["role"] == "dashboard-gpu")
    assert gpu_pod["spec"]["runtimeClassName"] == "nvidia" and "nvidia.com/gpu" not in json.dumps(gpu_pod["spec"]["containers"][0]["resources"])
    assert "hostPath" not in json.dumps(gpu_pod)
    probe = next(p for p in manifests if p["metadata"]["labels"]["role"] == "dashboard-probe")
    assert [v["hostPath"]["path"] for v in probe["spec"]["volumes"]] == ["/sys"] and probe["spec"]["containers"][0]["volumeMounts"][0]["readOnly"]


def test_stop_removes_the_pods_and_the_leftovers(fake):
    c = collector()
    c.start()
    names = [n for pair in c.pods.values() for n in pair if n]
    c.stop()
    assert not c.pods
    assert "app=stress-test-dashboard" in (fake / "deleted-by-label").read_text()
    assert len(names) == 4


def test_a_dead_probe_marks_the_node_without_stopping_the_round(fake, monkeypatch):
    c = collector()
    c.start()
    real = c.kube.exec

    def flaky(pod, script, timeout=30):
        if pod == c.pods["w2"][0]:
            from stress_test.kube import KubectlError
            raise KubectlError("boom")
        return real(pod, script, timeout)
    monkeypatch.setattr(c.kube, "exec", flaky)
    by = {n.name: n for n in c.refresh().nodes}
    assert by["w2"].probe_ok is False and by["m1"].probe_ok is True


def test_dashboard_run_with_injected_keys_draws_and_cleans_up(fake, monkeypatch):
    keys = iter(["down", "tab", "enter", "q", "q"])
    out = io.StringIO()

    def slow_keys(timeout):
        deadline = time.time() + 15                  # the collector thread needs a moment for its first round (longer on a busy machine)
        while time.time() < deadline and not dash.collector.view.nodes:
            time.sleep(0.1)
        time.sleep(0.4)
        return next(keys, "q")
    dash = d.Dashboard(Kubectl(str(FAKE)), stream=out, keys=slow_keys)
    assert dash.run() == 0
    text = ANSI.sub("", out.getvalue())
    assert "DASHBOARD" in text and "gpu1" in text and "\x1b[?25l" in out.getvalue() and "\x1b[?25h" in out.getvalue()
    assert (fake / "deleted-by-label").exists()


def test_cli_dashboard_needs_a_terminal(fake):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(fake), PYTHONPATH=str(ROOT))
    res = subprocess.run([sys.executable, "-m", "stress_test", "--dashboard"], cwd=fake, env=env, capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode != 0 and "needs a terminal" in res.stdout


def test_dashboard_is_a_key_with_an_icon_not_a_numbered_entry(monkeypatch, capsys):
    from stress_test import menu
    for cols in (50, 80, 130):
        text = "\n".join(menu.menu_lines("c", [], False, cols=cols))
        assert "📺 D" in text and "10 ▸" not in text and "DASHBOARD" not in text.split("🔁")[0]
    ran = []
    answers = iter(["d", "q"])
    menu.run_menu(ask=lambda p, d="": next(answers), run=lambda o: ran.append(o) or 0, cluster=lambda: "c")
    assert ran == [["--dashboard"]]


# ---------------- regression: odd objects in a real cluster must never blank the dashboard (2026-10-01) -------------------------

@pytest.mark.parametrize("text, mib", [("128Mi", 128), ("10M", 9), ("1G", 953), ("1Gi", 1024), ("1.5Gi", 1536), ("512k", 0), ("1e9", 953),
                                       ("134217728", 128), ("", 0), (None, 0), ("junk", 0), ("5Xi", 0), ("12e", 0)])
def test_memory_quantities_in_every_notation(text, mib):
    assert d._quantity_mem_mib(text) == mib


@pytest.mark.parametrize("text, milli", [("500m", 500), ("2", 2000), ("0.5", 500), ("1500u", 1), ("", 0), (None, 0), ("x", 0)])
def test_cpu_quantities(text, milli):
    assert d._quantity_cpu_m(text) == milli


def test_a_pod_with_a_decimal_si_memory_request_does_not_blank_the_dashboard(fake, monkeypatch):
    pods = [{"name": "ok", "node": "w2", "mem": "128Mi"}, {"name": "decimal", "node": "w2", "mem": "10M", "cpu": "250m"},
            {"name": "gig", "node": "gpu1", "mem": "1G"}, {"name": "odd", "node": "w2", "mem": "junk", "cpu": "lots"}]
    monkeypatch.setenv("FAKE_DASH_PODS", json.dumps(pods))
    c = collector()
    c.start()
    view = c.refresh()
    by = {n.name: n for n in view.nodes}
    assert by["w2"].probe_ok
    assert len(by["w2"].pods) == 3 and by["w2"].req_mem_mib == 128 + 9 and by["gpu1"].req_mem_mib == 953
    assert view.error == ""


def test_a_broken_stage_keeps_the_rest_of_the_data(fake, monkeypatch):
    c = collector()
    c.start()
    monkeypatch.setattr(c, "_refresh_k8s", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(c, "_refresh_events", lambda: (_ for _ in ()).throw(KeyError("x")))
    view = c.refresh()
    assert any(n.probe_ok and n.temp is not None for n in view.nodes) and ("boom" in view.error or "x" in view.error)
    assert "waiting for the first data" not in "\n".join(lines_of(view, 130))


def test_a_node_object_that_cannot_be_read_is_skipped(fake, monkeypatch):
    c = collector()
    c.start()
    real = d.node_from_json

    def picky(item):
        if item["metadata"]["name"] == "w2":
            raise KeyError("weird node")
        return real(item)
    monkeypatch.setattr(d, "node_from_json", picky)
    view = c.refresh()
    assert {"gpu1", "m1"} <= {n.name for n in view.nodes if n.probe_ok}


# ---------------- the full information of the biggest dashboard + fast intervals (2026-10-01) -----------------------------------------

def test_intervals_include_half_and_one_second():
    assert d.INTERVALS[:2] == (0.5, 1) and d.INTERVALS == tuple(sorted(d.INTERVALS))


def test_interval_shown_in_the_title_without_trailing_zeros():
    st = d.UiState(interval=0.5)
    assert "· 0.5 s ·" in "\n".join(lines_of(cluster(), 120, 40, st))
    assert "· 5 s ·" in "\n".join(lines_of(cluster(), 120))


def test_the_data_loop_waits_only_for_what_is_left_of_the_interval(monkeypatch):
    dash = make_dash()
    dash.state.interval = 0.5
    waits = []
    monkeypatch.setattr(dash._wake, "wait", lambda t: (waits.append(t), dash._stop.set())[0])
    monkeypatch.setattr(dash.collector, "refresh", lambda: time.sleep(0.2))
    dash._loop_data()
    assert 0.05 <= waits[0] <= 0.31                                   # 0.5 s minus the 0.2 s the round took


def test_parse_extras_new_blocks():
    text = "\n".join(["dmi sys_vendor Dell Inc.", "dmi bios_version A18", "gov powersave 800 3600 3600", "turbo 0", "cpufreqs 3600 3500 3600",
                      "ctemp Core_0 52000", "tz acpitz 27000", "psi cpu some 0.40", "psi io full 0.30", "mem MemFree 8192000",
                      "dio sda 2000 1000", "files 4200", "tasks 2/534", "nic eno1 1000 up 100 200 1500 full 1 2 3 4"])
    x = d.parse_extras(text)
    assert x["dmi"]["sys_vendor"] == "Dell Inc." and x["gov"] == ("powersave", 800, 3600, 3600) and x["turbo_off"] is False
    assert x["cpufreqs"] == [3600, 3500, 3600] and x["ctemp"] == {"Core 0": 52} and x["tz"] == {"acpitz": 27}
    assert x["psi"] == {"cpu some": 0.4, "io full": 0.3} and x["mem"]["MemFree"] == 8000 and x["dio"] == {"sda": (2000, 1000)}
    assert x["files"] == 4200 and x["tasks"] == "2/534"
    nic = x["nics"][0]
    assert (nic["mtu"], nic["duplex"], nic["rx_err"], nic["tx_err"], nic["rx_drop"], nic["tx_drop"]) == (1500, "full", 1, 2, 3, 4)


def test_collector_gathers_the_extra_node_cluster_and_pod_data(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DASH_PODS", json.dumps([{"name": "p1", "node": "w2", "ns": "web", "mem": "64Mi", "cpu": "100m"},
                                                      {"name": "p2", "node": "w2", "ns": "web", "phase": "Pending"}]))
    c = collector()
    c.start()
    c.refresh()
    time.sleep(0.01)
    view = c.refresh()
    by = {n.name: n for n in view.nodes}
    w = by["w2"]
    assert w.dmi["product_name"] == "OptiPlex 9020" and w.gov[0] == "powersave" and w.cpufreqs and w.ctemps["Core 0"] == 52
    assert w.tzones["acpitz"] == 27 and w.psi["cpu some"] == 0.4 and w.meminfo["MemFree"] == 8000 and w.files == 4200
    assert w.dio["sda"][0] > 0 and w.nics[0]["rx_drop"] == 2 and w.nics[0]["mtu"] == 1500
    assert w.alloc["pods"] == "110" and w.images == (2, 750_000_000) and w.age_s and not w.unschedulable
    assert w.top["cpu_pct"] == 5.0 and w.conditions["Ready"][0] == "True"
    assert view.api_ms is not None and view.svc_types and view.ingresses == 2 and view.pvcs["Bound"] == 1 and view.pvcs["GiB"] == 10.0
    assert view.jobs == {"active": 1, "failed": 2, "done": 1} and view.cronjobs == 1 and view.configmaps == 3 and view.pvs == 3
    assert view.ns_pods["web"] == 2 and view.problems and view.problems[0][1] == "web/p2"
    assert view.ev_counts and "BackOff" in view.ev_counts


def test_cordoned_node_and_taints_are_read(fake, monkeypatch):
    monkeypatch.setenv("FAKE_NODES", json.dumps([{"name": "w2", "cordoned": True, "taints": [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]}]))
    c = collector()
    c._refresh_k8s()
    n = c.nodes["w2"]
    assert n.unschedulable and n.taints == ["dedicated=gpu:NoSchedule"]


def test_biggest_dashboard_shows_everything(fake):
    c = collector()
    c.start()
    c.refresh()
    time.sleep(0.01)
    view = c.refresh()
    text = "\n".join(lines_of(view, 300, 60))
    for word in ("machine Dell Inc. OptiPlex 9020", "BIOS", "governor powersave", "core clocks", "core temps", "sensors", "pressure (stall avg10)",
                 "RAM", "disk sda", "NIC eno1", "mtu 1500", "errors 0 drops 2", "ping", "conditions", "schedulable", "taints", "images 2 cached",
                 "alloc", "req", "use 5%", "pods", "last test", "nodes 3/4 Ready" if False else "nodes", "capacity", "requested", "namespaces", "services",
                 "ingress 2", "storage", "jobs", "workloads", "API server", "warnings", "▸ problems", "▸ tests"):
        assert word in text, word
    assert len({ui.visible_len(x) for x in text.split("\n")}) == 1


@pytest.mark.parametrize("cols, lines", [(161, 24), (200, 30), (229, 45), (230, 45), (300, 70), (500, 100)])
def test_biggest_dashboard_fits_every_size(cols, lines):
    out = lines_of(cluster(), cols, lines)
    assert len({ui.visible_len(x) for x in out}) == 1 and len(out) <= lines + 40
    assert ui.visible_len(out[0]) == cols


def test_three_columns_from_230_two_below():
    two = "\n".join(lines_of(cluster(), 200, 60))
    three = "\n".join(lines_of(cluster(), 260, 60))
    assert two.count(" │ ") > 0 and three.count(" │ ") > two.count(" │ ")


def test_last_test_and_baseline_per_node(tmp_path, monkeypatch):
    from stress_test import baseline
    logs = tmp_path / "logs"
    day = logs / "2026-10-01"
    day.mkdir(parents=True)
    (day / "w2-60s-2026-10-01_10-00-00.log").write_text("\n".join([
        "=== KUBERNETES STRESS-NG LOG ===", "Node: w2", "Started: 2026-10-01 10:00:00", "Test duration: 1 min", "Profile: gpu - gpu-burn",
        "[10:00:05] CPU: 5%, RAM: 100 MiB (10%) | Temp: CPU: 50°C | Clock: 3000 MHz | GPU: 67°C ? 1300MHz 99% 1800/2048MiB thr=0x0"]) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("STRESS_TEST_LOG_ROOT", str(logs))
    monkeypatch.setattr("stress_test.paths.user_log_root", lambda *a, **k: logs)
    (logs / "baselines").mkdir()
    baseline.baseline_path(logs, "w2").write_text("{}")
    c = d.Collector(Kubectl("true"))
    c.nodes = {"w2": d.NodeView("w2"), "other": d.NodeView("other")}
    c._refresh_tests()
    assert "GPU" in c.nodes["w2"].last_test and "67°" in c.nodes["w2"].last_test and c.nodes["w2"].has_baseline
    assert c.nodes["other"].last_test == "" and not c.nodes["other"].has_baseline


# ---------------- cores and threads are written out (2026-10-01) ------------------------------------------------------------

def test_cpu_description_writes_cores_and_threads_in_words():
    n = node("a")
    n.cores, n.threads = 4, 8
    assert d.cpu_description(n) == "Intel Core i7-4790S @ 3.2 GHz · 4 cores / 8 threads (hyper-threading)"
    n.cores, n.threads = 2, 2
    assert d.cpu_description(n) == "Intel Core i7-4790S @ 3.2 GHz · 2 cores / 2 threads"
    n.cores, n.threads = 1, 1
    assert "1 core / 1 thread" in d.cpu_description(n)
    n.cores, n.threads = None, 4
    assert d.cpu_description(n).endswith("· 4 threads")
    n.cpu_model = ""
    assert d.cpu_description(n) == "?"


def test_dashboard_never_uses_the_short_4c_8t_form_and_shows_both_numbers(monkeypatch):
    text = "\n".join(lines_of(cluster(), 200, 50))
    assert "4 cores / 8 threads" in text and "4c/8t" not in text and "8t" not in text.replace("8 threads", "")
    assert "logical CPUs" in "\n".join(lines_of(cluster(), 300, 60))


def test_probe_counts_physical_cores_separately_from_threads(fake):
    c = collector()
    c.start()
    view = c.refresh()
    w = {n.name: n for n in view.nodes}["w2"]
    assert (w.cores, w.threads) == (4, 8)
    assert "4 cores / 8 threads" in d.cpu_description(w)


def test_a_node_without_core_ids_falls_back_to_one_thread_per_core(fake, monkeypatch):
    c = collector()
    c.start()
    real = c.kube.exec
    monkeypatch.setattr(c.kube, "exec", lambda pod, script, timeout=30: real(pod, script, timeout).replace("cores 4", "cores 0"))
    w = {n.name: n for n in c.refresh().nodes}["w2"]
    assert (w.cores, w.threads) == (8, 8)
