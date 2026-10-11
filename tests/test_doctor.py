"""--doctor: the findings are pure functions of the collected data; the report, its files and the comparison with the last check."""
import json
import re
import time

import pytest

from stress_test import dashboard as d, doctor
from test_dashboard import node

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def titles(findings, level=None):
    return [f.title for f in findings if level is None or f.level == level]


def view_of(*nodes, **kw):
    v = d.ClusterView(nodes=list(nodes), api_ms=kw.pop("api_ms", 40.0))
    for k, val in kw.items():
        setattr(v, k, val)
    return v


# ---------------- API and pods ---------------------------------------------------------------------------------------------

def test_api_speed_levels():
    assert doctor.api_findings(view_of(api_ms=40.0))[0].level == "OK"
    assert doctor.api_findings(view_of(api_ms=800.0))[0].level == "WARN"
    assert doctor.api_findings(view_of(api_ms=3000.0))[0].level == "FAIL"
    assert doctor.api_findings(view_of(api_ms=None))[0].level == "INFO"


def test_problem_pods_events_and_pending_volumes_are_reported():
    out = doctor.api_findings(view_of(problems=[("w1", "default/crash", "CrashLoopBackOff", 7)], ev_counts={"BackOff": 12},
                                      pvcs={"Pending": 2}))
    assert any(f.level == "WARN" and "not fine" in f.title for f in out)
    assert any("warning event" in f.title and f.level == "WARN" for f in out)
    assert any("Pending" in f.title for f in out)
    assert "All pods are running" in titles(doctor.api_findings(view_of()))


# ---------------- one node --------------------------------------------------------------------------------------------------

def test_a_node_that_is_fine_has_no_warning():
    n = node("w1", temp=45, cpu=10)
    n.gov, n.has_baseline, n.mem_total_mib, n.mem_used_mib = ("performance", 800, 3600, 3600), True, 8000, 2000
    n.pods = []
    out = doctor.node_findings(n)
    assert not [f for f in out if f.level in ("WARN", "FAIL")], titles(out)


def test_not_ready_node_stops_there_and_a_node_without_probe_is_noted():
    n = node("w1", ready=False, probe=False)
    n.conditions = {"Ready": ("False", "KubeletNotReady")}
    out = doctor.node_findings(n)
    assert [f.level for f in out] == ["FAIL"] and "NOT Ready" in out[0].title
    quiet = node("w2", probe=False)
    quiet.ready = True
    assert any("probe pod did not start" in t for t in titles(doctor.node_findings(quiet), "WARN"))


@pytest.mark.parametrize("setup, level, text", [
    (lambda n: setattr(n, "temps", {"CPU": 85}), "FAIL", "85 °C"),
    (lambda n: (setattr(n, "temps", {"CPU": 74}), setattr(n, "cpu_pct", 5.0)), "WARN", "74 °C"),
    (lambda n: setattr(n, "throttle_count", 12), "WARN", "throttling"),
    (lambda n: setattr(n, "gov", ("powersave", 800, 3600, 3600)), "INFO", "powersave"),
    (lambda n: setattr(n, "turbo_off", True), "INFO", "turbo"),
    (lambda n: (setattr(n, "mem_used_mib", 23000), setattr(n, "mem_total_mib", 23400)), "WARN", "of RAM is free"),
    (lambda n: (setattr(n, "swap_total_mib", 2048), setattr(n, "swap_used_mib", 500)), "WARN", "Swap is used"),
    (lambda n: setattr(n, "swap_total_mib", 2048), "INFO", "Swap is enabled"),
    (lambda n: setattr(n, "psi", {"cpu some": 25.0}), "WARN", "Pressure stall"),
    (lambda n: setattr(n, "nics", [{"name": "eno1", "speed": 1000, "state": "up", "duplex": "half"}]), "WARN", "half duplex"),
    (lambda n: setattr(n, "nics", [{"name": "eno1", "speed": 1000, "state": "up", "rx_err": 3, "tx_err": 1}]), "WARN", "4 transmit/receive errors"),
    (lambda n: setattr(n, "peers", {"w2": None}), "WARN", "No ping"),
    (lambda n: setattr(n, "peers", {"w2": 40.0}), "INFO", "Slow ping"),
    (lambda n: setattr(n, "pressure", {"Memory": True}), "WARN", "Memory pressure"),
    (lambda n: setattr(n, "unschedulable", True), "INFO", "cordoned"),
])
def test_node_findings(setup, level, text):
    n = node("w1", temp=45, cpu=10)
    n.pods = []
    setup(n)
    out = doctor.node_findings(n)
    assert any(f.level == level and text in f.title and f.node == "w1" for f in out), titles(out)
    assert all(f.advice for f in out if f.level in ("WARN", "FAIL"))              # every problem comes with what to do


def test_requests_restarts_and_the_master_on_a_hdd():
    n = node("w1", temp=45, cpu=10)
    n.alloc = {"cpu": "2", "memory": "8000Mi"}
    n.mem_alloc_mib = 8000
    n.pods = [{"name": "a", "ns": "x", "phase": "Running", "restarts": 6, "cpu_m": 1900, "mem_mib": 7600}]
    out = titles(doctor.node_findings(n), "WARN")
    assert any("CPU requests" in t for t in out) and any("RAM requests" in t for t in out)
    assert not any("restarts" in t for t in out)                                              # cumulative restarts are only a note the first time
    assert any("restarts" in t and "cumulative" in t for t in titles(doctor.node_findings(n), "INFO"))
    assert any("6 new container restarts" in t for t in titles(doctor.node_findings(n, 0), "WARN"))
    assert not any("restarts" in t for t in titles(doctor.node_findings(n, 5)))                # one new one: nothing
    m = node("m1", master=True, temp=45, cpu=10)
    m.pods = []
    m.disks = [{"name": "sda", "size_gb": 500, "kind": "HDD", "model": "x"}]
    assert any("spinning disk" in t for t in titles(doctor.node_findings(m), "WARN"))
    m.disks = [{"name": "sda", "size_gb": 500, "kind": "SSD/NVMe", "model": "x"}]
    assert not any("spinning disk" in t for t in titles(doctor.node_findings(m)))


# ---------------- the nodes together ----------------------------------------------------------------------------------------

def test_consistency_of_versions_links_and_mtu():
    a, b = node("a"), node("b")
    b.kubelet, b.kernel = "v1.31.0+k3s1", "6.9.0"
    out = doctor.consistency_findings([a, b])
    assert any(f.level == "WARN" and "Kubernetes (k3s) version" in f.title for f in out)
    assert any(f.level == "INFO" and "kernel" in f.title for f in out)
    b.nics = [{"name": "eno1", "speed": 100, "state": "up", "mtu": 1400}]
    out = doctor.consistency_findings([a, b])
    assert any("slower link" in f.title and "b has" in f.title for f in out)
    a.nics = [{"name": "eno1", "speed": 1000, "state": "up", "mtu": 1500}]
    assert any("different MTUs" in f.title for f in doctor.consistency_findings([a, b]))
    assert doctor.consistency_findings([node("x")]) == []


def test_clock_and_dns_extras():
    parsed = doctor.parse_extra("now 1000\ndns ok\n", 996.0, 998.0)
    assert parsed == {"skew_s": 3.0, "dns_ok": True}
    assert doctor.parse_extra("junk", 0, 1) == {"skew_s": None, "dns_ok": None}
    out = doctor.extras_findings({"a": {"skew_s": 0.2, "dns_ok": True}, "b": {"skew_s": 5.0, "dns_ok": False}, "c": {"skew_s": 120.0, "dns_ok": True}})
    assert [(f.node, f.level) for f in out if f.area == "clock"] == [("b", "WARN"), ("c", "FAIL")]
    assert any(f.node == "b" and "DNS" in f.title for f in out)
    good = doctor.extras_findings({"a": {"skew_s": 0.1, "dns_ok": True}})
    assert titles(good) == ["The clocks of all nodes agree", "DNS works inside the cluster"]


# ---------------- the mini load test ----------------------------------------------------------------------------------------

def test_load_findings_compare_with_rest_the_last_check_and_older_logs():
    rest = {"api": 50.0, "w1": 100.0, "w2": 100.0, "w3": 100.0}
    loaded = {"api": 80.0, "w1": 150.0, "w2": 450.0, "w3": 1500.0}
    out = doctor.load_findings(rest, loaded)
    by = {f.node: f.level for f in out if f.node}
    assert by == {"w1": "OK", "w2": "WARN", "w3": "FAIL"}
    assert any("API server under load" in f.title and f.level == "OK" for f in out)
    assert doctor.load_findings({"api": 50.0}, {"api": 900.0})[0].level == "WARN"
    prev = {"w1": {"loaded": 60.0}}
    assert any("last check" in f.title for f in doctor.load_findings(rest, {"w1": 150.0}, prev))
    logs = {"w1": {"peak_temp": 70, "measured_temp": 69, "avg_freq": 3600, "measured_freq": 2800}}
    texts = titles(doctor.load_findings(rest, {"w1": 150.0}, None, logs), "WARN")
    assert any("as hot" in t for t in texts) and any("Clock 2800 MHz" in t for t in texts)


def test_older_load_reference_reads_the_newest_log_of_the_node(tmp_path):
    day = tmp_path / "2026-10-01"
    day.mkdir()
    line = "[10:00:{s:02d}] CPU: 100% | Temp: CPU: {t}°C | Clock: {f} MHz\n"
    (day / "w1-test.log").write_text("=== KUBERNETES STRESS-NG LOG ===\nNode: w1\n" + "".join(line.format(s=i, t=60 + i, f=3500) for i in range(5)),
                                      encoding="utf-8")
    ref = doctor.older_load_reference("w1", tmp_path)
    assert ref["peak_temp"] == 64 and ref["avg_freq"] == 3500
    light = day / "w1-zzz.log"                                                         # a newer but lighter (50 %) test is no reference
    light.write_text("=== KUBERNETES STRESS-NG LOG ===\n[10:05:01] CPU: 50% | Temp: CPU: 50°C | Clock: 2000 MHz\n", encoding="utf-8")
    import os
    os.utime(light, (time.time() + 50, time.time() + 50))
    assert doctor.older_load_reference("w1", tmp_path)["peak_temp"] == 64
    assert doctor.older_load_reference("nobody", tmp_path) is None


# ---------------- the report -------------------------------------------------------------------------------------------------

def make_report(**kw):
    ok, bad = node("w1", temp=45, cpu=10), node("w2", temp=88, cpu=10)
    for n in (ok, bad):
        n.pods = []
    return doctor.build_report(view_of(ok, bad), version="1.20", when=1_800_000_000.0, **kw)


def test_scores_verdict_and_counts():
    r = make_report()
    assert r.scores["w2"] < r.scores["w1"] and r.scores["cluster"] <= r.scores["w2"]
    assert r.worst == "FAIL" and r.count("FAIL") >= 1
    assert r.verdict in ("healthy", "mostly fine", "needs attention", "in trouble")
    clean = doctor.build_report(view_of(), version="x")
    assert clean.scores["cluster"] == 100 and clean.verdict == "healthy"


def test_changes_since_the_previous_report():
    first = make_report()
    assert doctor.changes(None, first)["first"] is True
    nodes = [node("w1", temp=45, cpu=10), node("w2", temp=45, cpu=10)]
    for n in nodes:
        n.pods = []
    healed = doctor.build_report(view_of(*nodes), when=1.0)
    diff = doctor.changes(first.to_dict(), healed)
    assert any("85" in f["title"] or "88" in f["title"] for f in diff["fixed"]) and diff["new"] == [] and diff["first"] is False
    worse = doctor.changes(healed.to_dict(), first)
    assert any(f["node"] == "w2" for f in worse["new"])


def test_files_roundtrip_and_markdown(tmp_path):
    r = make_report()
    jpath, mpath = doctor.save(r, tmp_path)
    assert jpath.parent == tmp_path / "doctor" and jpath.suffix == ".json" and mpath.suffix == ".md"
    data = json.loads(jpath.read_text())
    assert data["schema"] == doctor.SCHEMA and data["scores"] == r.scores and data["findings"][0].keys() >= {"level", "title", "advice"}
    assert doctor.latest_report(tmp_path)["when"] == r.when
    assert doctor.latest_report(tmp_path, before=r.when) is None
    md = mpath.read_text()
    assert md.startswith("# Cluster doctor") and "what to do:" in md and "| w2 |" in md
    assert doctor.latest_report(tmp_path / "nothing") is None
    (tmp_path / "doctor" / "9999-bad.json").write_text("{not json")
    assert doctor.latest_report(tmp_path)["when"] == r.when                         # a damaged file is skipped


@pytest.mark.parametrize("width", [50, 70, 100, 150])
@pytest.mark.parametrize("on", [False, True])
def test_render_fits_the_width_and_has_no_colour_codes_without_colour(width, on):
    r = make_report()
    lines = doctor.render(r, on, width, doctor.changes(None, r), show_ok=True)
    assert lines and all(d.ui.visible_len(line) <= width for line in lines)
    text = "\n".join(lines)
    assert "CLUSTER DOCTOR" in text and "w2" in text and ("\x1b[" in text) == on
    assert "first check" in ANSI.sub("", text)


def test_responsiveness_uses_the_probe_pods():
    class K:
        calls = []

        def run(self, *a, **k):
            self.calls.append(a)
            return "ok"

        def exec(self, pod, script, timeout=0):
            self.calls.append((pod, script))
            if pod == "bad":
                raise RuntimeError("x")
            return ""
    ticks = iter(i * 0.01 for i in range(1000))
    out = doctor.responsiveness(K(), {"w1": "p1", "w2": "bad"}, samples=2, clock=lambda: next(ticks))
    assert out["api"] and out["w1"] and out["w2"] is None


# ---------------- the whole run (stubs) -------------------------------------------------------------------------------------

class Collector:
    stopped = False

    def stop(self):
        Collector.stopped = True


def stub_collect(view, extras=None):
    def collect(kube, only, say):
        Collector.stopped = False
        return view, extras or {}, {n.name: f"probe-{n.name}" for n in view.nodes}, Collector()
    return collect


def args_of(**kw):
    import argparse
    base = dict(nodes="", doctor_load=False, doctor_load_ok=False, non_interactive=False, yes=False, log_dir=None)
    base.update(kw)
    return argparse.Namespace(**base)


def healthy_view():
    nodes = [node("w1", temp=45, cpu=10), node("m1", master=True, temp=45, cpu=10)]
    for n in nodes:
        n.pods = []
    return view_of(*nodes)


class Kube:
    def run(self, *a, **k):
        return "ok"

    def exec(self, *a, **k):
        return ""


def test_run_writes_the_report_prints_it_and_removes_the_probes(tmp_path, capsys):
    messages = []
    code = doctor.run(args_of(), Kube(), say=messages.append, root=tmp_path, collect_fn=stub_collect(healthy_view()))
    out = capsys.readouterr().out
    assert code == 0 and Collector.stopped is True
    assert "CLUSTER DOCTOR" in out and list((tmp_path / "doctor").glob("*.md")) and list((tmp_path / "doctor").glob("*.json"))
    assert any("Report:" in m for m in messages)
    again = doctor.run(args_of(), Kube(), say=messages.append, root=tmp_path, collect_fn=stub_collect(healthy_view()))
    assert again == 0 and "since the last check" in capsys.readouterr().out


def test_exit_code_is_one_when_something_failed(tmp_path):
    v = healthy_view()
    v.nodes[0].temps = {"CPU": 90}
    assert doctor.run(args_of(), Kube(), say=lambda t: None, root=tmp_path, collect_fn=stub_collect(v)) == 1


def test_the_load_test_needs_an_explicit_ok_and_never_loads_the_master(tmp_path):
    calls = []

    def runner(kube, probes, workers, log_dir, say):
        calls.append(workers)
        return {"api": 90.0, "w1": 400.0}

    def go(args, answer="n"):
        calls.clear()
        return doctor.run(args, Kube(), ask=lambda p, d="": answer, say=lambda t: None, root=tmp_path,
                          load_runner=runner, collect_fn=stub_collect(healthy_view()))
    go(args_of(doctor_load=True), "n")
    assert calls == []                                                   # asked, answered no
    go(args_of(doctor_load=True), "y")
    assert calls == [["w1"]]                                             # only the worker
    go(args_of(doctor_load=True, non_interactive=True))
    assert calls == []                                                   # a non-interactive run does not load by itself
    go(args_of(doctor_load=True, non_interactive=True, yes=True))
    assert calls == [["w1"]]
    go(args_of(doctor_load=True, non_interactive=True, doctor_load_ok=True))
    assert calls == [["w1"]]                                             # pre-authorised once (the scheduled check)
    go(args_of(doctor_load=False), "y")
    assert calls == []
    saved = doctor.latest_report(tmp_path)
    assert saved is None or "findings" in saved


def test_load_part_lands_in_the_report(tmp_path):
    def runner(kube, probes, workers, log_dir, say):
        return {"api": 90.0, "w1": 5000.0}
    doctor.run(args_of(doctor_load=True, yes=True), Kube(), say=lambda t: None, root=tmp_path, load_runner=runner,
               collect_fn=stub_collect(healthy_view()))
    data = doctor.latest_report(tmp_path)
    assert data["load"]["nodes"]["w1"]["loaded"] == 5000.0
    assert any(f["area"] == "load" for f in data["findings"])


def test_load_command_is_accepted_by_the_program_itself():
    """The child command must be one the real program accepts (the first version passed --workers together with --nodes and the child
    answered 'Give only one of ...' and ended at once - a live test on the cluster found it)."""
    from stress_test import cli
    for workers in (["w1", "w2"], []):
        cmd = doctor.load_command(workers, "/x")
        args = cli.build_parser().parse_args(cmd[3:])                          # no argparse error
        assert cli._choose_scope(args) in ("nodes", "workers")                  # exactly one way of naming the nodes
        assert [bool(args.workers), bool(args.nodes)].count(True) == 1
        assert args.parallel and args.cpu_load == 50 and args.ram_pct == 50 and args.duration == 60 and args.cooldown == 0
        assert args.non_interactive and args.yes and not args.include_master


def test_load_command_and_the_sampling_loop():
    cmd = doctor.load_command(["w1", "w2"], "/x")
    assert cmd[1:3] == ["-m", "stress_test"] and "--workers" not in cmd and "--parallel" in cmd and "--cpu-load" in cmd
    assert cmd[cmd.index("--cpu-load") + 1] == "50" and cmd[cmd.index("--time") + 1] == "60" and "--include-master" not in cmd
    assert cmd[cmd.index("--nodes") + 1] == "w1,w2" and "--non-interactive" in cmd and "--yes" in cmd
    assert "--workers" in doctor.load_command([]) and "--nodes" not in doctor.load_command([])

    class Child:
        n = 0
        killed = False

        def poll(self):
            Child.n += 1
            return 0 if Child.n > 14 else None

        def terminate(self):
            Child.killed = True
    clock = iter(range(0, 10000, 5))
    import time as _t
    real = _t.monotonic
    _t.monotonic = lambda: next(clock)
    try:
        out = doctor.run_load(Kube(), {"w1": "p"}, ["w1"], None, lambda t: None, sleep=lambda s: None, popen=lambda *a, **k: Child())
    finally:
        _t.monotonic = real
    assert "api" in out and "w1" in out


def test_newest_log_stats_ignores_logs_from_before(tmp_path):
    day = tmp_path / "2026-10-11"
    day.mkdir()
    log = day / "w1-x.log"
    log.write_text("=== KUBERNETES STRESS-NG LOG ===\n[10:00:01] CPU: 50% | Temp: CPU: 61°C | Clock: 3000 MHz\n[10:00:02] CPU: 50% | Temp: CPU: 63°C | Clock: 3200 MHz\n",
                   encoding="utf-8")
    assert doctor.newest_log_stats("w1", log.stat().st_mtime + 100, tmp_path) is None
    stats = doctor.newest_log_stats("w1", 0, tmp_path)
    assert stats == {"measured_temp": 63, "measured_freq": 3100}


# ---------------- the program: the command line and the menu ---------------------------------------------------------------

def test_menu_entry_returns_the_doctor_options(monkeypatch):
    from stress_test import menu

    def scripted(*answers):
        it = iter(answers)
        return lambda prompt, default="": next(it)
    assert menu.build_action("8", scripted("5", "n")) == ["--doctor"]
    assert menu.build_action("8", scripted("5", "y")) == ["--doctor", "--doctor-load", "--doctor-load-ok"]


def test_doctor_against_the_fake_cluster(tmp_path):
    import os
    import subprocess
    import sys
    from test_integration import FAKE, ROOT
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), NO_COLOR="1",
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
    res = subprocess.run([sys.executable, "-m", "stress_test", "--doctor", "--log-dir", str(tmp_path / "logs")], cwd=tmp_path, env=env,
                         text=True, capture_output=True, timeout=120)
    assert res.returncode in (0, 1), res.stdout[-800:] + res.stderr[-800:]
    assert "CLUSTER DOCTOR" in res.stdout and "Report:" in res.stdout
    assert list((tmp_path / "logs" / "doctor").glob("*.json")) and list((tmp_path / "logs" / "doctor").glob("*.md"))
    again = subprocess.run([sys.executable, "-m", "stress_test", "--doctor", "--log-dir", str(tmp_path / "logs")], cwd=tmp_path, env=env,
                           text=True, capture_output=True, timeout=120)
    assert "since the last check" in again.stdout


def test_a_mini_test_that_never_ran_is_a_failure_not_silence(tmp_path):
    class Child:
        def __init__(self, *a, **k):
            k["stdout"].write("Give only one of the options --workers, --cluster, --nodes\n")
            k["stdout"].flush()

        def poll(self):
            return 2

        def terminate(self):
            pass
    out = doctor.run_load(Kube(), {"w1": "p"}, ["w1"], None, lambda t: None, sleep=lambda s: None, popen=Child, output_dir=tmp_path)
    assert "Give only one of" in out["_error"] and "code 2" in out["_error"]
    report = doctor.build_report(healthy_view(), load={"rest": {}, "loaded": {}, "error": out["_error"], "nodes": {}}, version="x")
    assert any(f.level == "FAIL" and "did not run" in f.title for f in report.findings) and not any(f.area == "load" and f.level == "OK" for f in report.findings)


def test_run_reports_the_failed_mini_test(tmp_path, capsys):
    doctor.run(args_of(doctor_load=True, yes=True), Kube(), say=lambda t: None, root=tmp_path, collect_fn=stub_collect(healthy_view()),
               load_runner=lambda *a: {"_error": "the test ended with code 2: nope"})
    data = doctor.latest_report(tmp_path)
    assert any(f["level"] == "FAIL" and "did not run" in f["title"] for f in data["findings"])


def test_diff_ignores_nodes_that_are_missing_from_one_report():
    a, b = node("w1", temp=45, cpu=10), node("m1", master=True, temp=88, cpu=10)
    for n in (a, b):
        n.pods = []
    both = doctor.build_report(view_of(a, b), when=1.0)
    only_w1 = doctor.build_report(view_of(a), when=2.0)
    diff = doctor.changes(both.to_dict(), only_w1)
    assert not any(f["node"] == "m1" for f in diff["fixed"])                                  # the master was not checked this time


def test_a_positive_ping_line_and_stored_restarts():
    n = node("w1", temp=45, cpu=10)
    n.pods = [{"name": "a", "ns": "x", "phase": "Running", "restarts": 9, "cpu_m": 1, "mem_mib": 1}]
    n.peers = {"w2": 0.4}
    assert any(f.level == "OK" and "Ping to the other nodes works" in f.title for f in doctor.node_findings(n))
    first = doctor.build_report(view_of(n), when=1.0)
    assert first.restarts == {"w1": 9}
    second = doctor.build_report(view_of(n), when=2.0, previous=first.to_dict())
    assert not any("restarts" in f.title and f.level in ("WARN", "INFO") for f in second.findings)       # nothing new since the first check


def test_ctrl_c_at_a_question_ends_quietly(tmp_path):
    import subprocess
    import sys
    import os
    from test_integration import FAKE, ROOT
    code = "from stress_test import cli; raise SystemExit(cli.main(['--doctor-every', '14', '--doctor-at', '03:00']))"
    # a KeyboardInterrupt raised anywhere inside the program is reported in one line (no traceback)
    code = ("import sys; from stress_test import cli\n"
            "cli._main = lambda args: (_ for _ in ()).throw(KeyboardInterrupt)\n"
            "sys.exit(cli.main(['--doctor-last']))")
    res = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, text=True, capture_output=True, timeout=60,
                         env=dict(os.environ, PYTHONPATH=str(ROOT), KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), NO_COLOR="1"))
    assert res.returncode == 130 and "Interrupted" in res.stdout and "Traceback" not in res.stderr


def test_saved_reports_are_private(tmp_path):
    import stat
    jpath, mpath = doctor.save(doctor.Report(when=1_700_000_000), tmp_path)
    assert stat.S_IMODE(jpath.stat().st_mode) == 0o600 and stat.S_IMODE(mpath.stat().st_mode) == 0o600
