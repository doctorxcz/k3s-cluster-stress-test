import pytest

from stress_test.manifests import (IMAGE_BUSYBOX, IMAGE_UBUNTU, build_stress_command,
                                    stress_memory_limit_mib, stress_pod)
from stress_test.models import PodNames, StressConfig
from stress_test.parsing import (calc_ram_target_mib, describe_duration,
                                 format_duration, label_for_sensor,
                                 node_from_json, parse_duration,
                                 parse_probe_output, parse_quantity_mib,
                                 parse_top)


@pytest.mark.parametrize("text,expected", [
    ("3163Mi", 3163), ("8Gi", 8192), ("1Ti", 1048576),
    ("2048Ki", 2), ("1073741824", 1024),
])
def test_parse_quantity_mib(text, expected):
    assert parse_quantity_mib(text) == expected


def test_parse_quantity_invalid():
    with pytest.raises(ValueError):
        parse_quantity_mib("<unknown>")


def test_parse_top_ok():
    top = parse_top("control-1 4190m 52% 3163Mi 43%")
    assert top.cpu == "4190m" and top.cpu_pct == "52%"
    assert top.mem_mib == 3163 and top.mem_pct == "43%"


@pytest.mark.parametrize("line", ["", "node <unknown> <unknown> <unknown> <unknown>", "a b"])
def test_parse_top_invalid(line):
    assert parse_top(line) is None


def test_parse_probe_output_full():
    text = "temp coretemp 38000\ntemp nvme 39900\ntemp nvme 46900\nfreq 3100\n"
    data = parse_probe_output(text)
    assert data.temps == {"CPU": 38, "NVMe": 46}   # the maximum is taken
    assert data.cpu_temp == 38
    assert data.freq_mhz == 3100


def test_parse_probe_output_no_cpu_sensor():
    data = parse_probe_output("temp nvme 40000\n")
    assert data.cpu_temp is None


def test_parse_probe_output_ignores_garbage():
    data = parse_probe_output("nonsense\ntemp coretemp abc\nfreq x\n")
    assert data.temps == {} and data.freq_mhz is None


def test_label_for_sensor():
    assert label_for_sensor("k10temp") == "CPU"
    assert label_for_sensor("amdgpu") == "GPU"
    assert label_for_sensor("nvme") == "NVMe"
    assert label_for_sensor("drivetemp") == "HDD"


def test_calc_ram_target_respects_reserve():
    # free 4000 MiB, 80 % = 3200
    assert calc_ram_target_mib(7000, 3000, 80) == 3200
    # 100 % would use up everything -> reserve 256 MiB
    assert calc_ram_target_mib(7000, 3000, 100) == 3744
    # almost full -> never negative
    assert calc_ram_target_mib(1000, 990, 80) == 0


def test_node_from_json():
    data = {
        "metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": "true"}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"memory": "7300000Ki"},
            "capacity": {"cpu": "8"},
            "nodeInfo": {"osImage": "Ubuntu", "kernelVersion": "7.0", "architecture": "amd64",
                         "containerRuntimeVersion": "containerd://2"},
        },
    }
    node = node_from_json(data)
    assert node.ready and node.is_control_plane
    assert node.allocatable_mem_mib == 7128
    assert node.capacity_cpu == "8"


def test_node_from_json_worker_not_ready():
    node = node_from_json({"metadata": {"name": "w"},
                           "status": {"conditions": [{"type": "Ready", "status": "False"}]}})
    assert not node.ready and not node.is_control_plane


def test_build_stress_command_variants():
    full = build_stress_command(StressConfig(node="n", duration=30, cpu_load=100), None)
    assert full == ("stress-ng --cpu 0 --cpu-method matrixprod --matrix 0 "
                    "--timeout 30s --metrics-brief")
    part = build_stress_command(
        StressConfig(node="n", duration=60, cpu_load=50, hdd=True), 512)
    assert "--cpu-load 50" in part and "--vm-bytes 512M" in part and "--hdd 0" in part


def test_stress_pod_has_deadline_node_name_and_labels():
    names = PodNames.new("abc123")
    pod = stress_pod("n1", names, 700, "stress-ng --cpu 0")
    assert pod["metadata"]["name"] == "stress-test-abc123"
    assert pod["metadata"]["labels"] == {"app": "stress-test-tool",
                                         "run-id": "abc123", "role": "stress"}
    assert pod["spec"]["nodeName"] == "n1"
    assert pod["spec"]["activeDeadlineSeconds"] == 700
    assert "STRESS-NG STARTED" in pod["spec"]["containers"][0]["command"][2]


@pytest.mark.parametrize("text,expected", [
    ("90", 90), ("90s", 90), ("45sec", 45), ("5m", 300), ("5min", 300),
    ("1h", 3600), ("1h30m", 5400), ("1h 5m 30s", 3930), (" 2H ", 7200),
    ("10m15s", 615),
])
def test_parse_duration_ok(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "0s", "5x", "1h-5m", "m5", "1.5h"])
def test_parse_duration_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("seconds,expected", [
    (0, "0 s"), (45, "45 s"), (60, "1 min"), (90, "1 min 30 s"),
    (3600, "1 h"), (3930, "1 h 5 min 30 s"), (7200, "2 h"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_describe_duration():
    assert describe_duration(45) == "45 s"
    assert describe_duration(300) == "5 min (300 s)"
    assert describe_duration(5400) == "1 h 30 min (5400 s)"


# ---------------- unique pod names and concurrent tests ---------------------------

from stress_test.manifests import hw_pod, probe_pod
from stress_test.parsing import parse_stressng_outcome, tool_pods_from_json


def test_pod_names_are_unique_per_run():
    a, b = PodNames.new(), PodNames.new()
    assert a.run_id != b.run_id
    assert set(a.all).isdisjoint(b.all)          # no common name -> they do not delete each other
    assert a.stress == f"stress-test-{a.run_id}"
    assert a.probe == f"temp-probe-{a.run_id}" and a.hw == f"hw-info-{a.run_id}"


def test_all_pods_of_a_run_share_run_id_label():
    n = PodNames.new("x1")
    for pod in (hw_pod("n", n), probe_pod("n", n, 100), stress_pod("n", n, 100, "c")):
        assert pod["metadata"]["labels"]["run-id"] == "x1"
        assert pod["metadata"]["labels"]["app"] == "stress-test-tool"


def test_tool_pods_from_json():
    data = {"items": [
        {"metadata": {"name": "stress-test-a", "labels": {"run-id": "a"}},
         "spec": {"nodeName": "dell"}, "status": {"phase": "Running"}},
        {"metadata": {"name": "temp-probe-a", "labels": {"run-id": "a"}},
         "spec": {}, "status": {}},
    ]}
    pods = tool_pods_from_json(data)
    assert (pods[0].name, pods[0].node, pods[0].phase, pods[0].run_id) == \
        ("stress-test-a", "dell", "Running", "a")
    assert pods[1].node == "" and pods[1].phase == ""
    assert tool_pods_from_json({}) == []


def test_parse_stressng_outcome():
    ok = ["stress-ng: info:  [428] successful run completed in 1 min 5.95 secs"]
    bad = ["stress-ng: info:  [428] unsuccessful run completed in 3.2 secs"]
    assert parse_stressng_outcome(ok) == "ok"
    assert parse_stressng_outcome(bad) == "failed"      # "unsuccessful" is not a success
    assert parse_stressng_outcome(["stress-ng: running..."]) == "unknown"
    assert parse_stressng_outcome([]) == "unknown"


# ---------------- node workload, MemAvailable, tightened manifests --------------------

from stress_test.parsing import format_workload, workload_from_json


def _pod(ns, name, phase="Running", labels=None):
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {}},
            "status": {"phase": phase}}


def test_workload_from_json_filters_and_counts():
    data = {"items": [
        _pod("minecraft-lobby", "lobby-1"),
        _pod("minecraft-lobby", "lobby-2"),
        _pod("factorio", "factorio-1", phase="Pending"),
        _pod("kube-system", "coredns-1"),
        _pod("kube-system", "traefik-1"),
        _pod("minecraft-java", "report-1", phase="Succeeded"),            # finished
        _pod("default", "stress-test-abc", labels={"app": "stress-test-tool"}),   # tool
    ]}
    work = workload_from_json(data)
    assert work.user_pods == (("factorio", "factorio-1"), ("minecraft-lobby", "lobby-1"),
                              ("minecraft-lobby", "lobby-2"))
    assert work.system_count == 2
    assert workload_from_json({}).user_pods == ()


def test_format_workload_groups_by_namespace():
    work = workload_from_json({"items": [_pod("a", f"p{i}") for i in range(5)]
                               + [_pod("b", "x")]})
    lines = format_workload(work, per_namespace=2)
    assert lines == ["a (5): p0, p1 (+3 more)", "b (1): x"]


def test_parse_probe_output_mem_available():
    data = parse_probe_output("temp coretemp 40000\nmem_available_kb 8388608\n")
    assert data.mem_available_mib == 8192
    assert parse_probe_output("temp coretemp 40000\n").mem_available_mib is None


def test_pinned_images_and_hardening():
    n = PodNames.new("t1")
    stress = stress_pod("n", n, 100, "c", memory_limit_mib=5120)
    c = stress["spec"]["containers"][0]
    assert IMAGE_UBUNTU == "ubuntu:24.04" and c["image"].startswith("ghcr.io/doctorxcz/k3s-stress-tools:")      # fallback pinned, normal pod = the prebuilt image
    assert ":" in IMAGE_BUSYBOX and not IMAGE_UBUNTU.endswith("latest")
    assert stress["spec"]["automountServiceAccountToken"] is False
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["resources"]["limits"]["memory"] == "5120Mi"
    assert c["resources"]["requests"]["memory"] == "64Mi"      # small request -> OOM first
    probe = probe_pod("n", n, 100)["spec"]["containers"][0]
    assert IMAGE_BUSYBOX == "busybox:1.36" and probe["image"].startswith("ghcr.io/doctorxcz/k3s-stress-probe:")
    assert probe["volumeMounts"][0]["readOnly"] is True


def test_hw_pod_is_not_privileged_by_default_and_has_no_hostpath():
    n = PodNames.new("t2")
    pod = hw_pod("n", n)
    c = pod["spec"]["containers"][0]
    assert "privileged" not in c["securityContext"]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["spec"]["volumes"] == [] and "volumeMounts" not in c
    assert c["env"] == [{"name": "HW_PRIVILEGED", "value": "0"}]
    priv = hw_pod("n", n, privileged=True)["spec"]["containers"][0]
    assert priv["securityContext"] == {"privileged": True}
    assert priv["env"] == [{"name": "HW_PRIVILEGED", "value": "1"}]


def test_stress_memory_limit():
    assert stress_memory_limit_mib(None) == 1536
    assert stress_memory_limit_mib(4096) == 4096 + 1024


# ---------------- stepped test: command, stage markers, result --------------------------------------

from stress_test.manifests import build_stepped_command
from stress_test.parsing import (STAGE_RE, count_completed_runs, describe_steps, parse_steps,
                                 split_stage_lines)


def test_parse_steps_accepts_common_separators():
    assert parse_steps("25,50,75,100") == (25, 50, 75, 100)
    assert parse_steps("25/50 75 100%") == (25, 50, 75, 100)
    assert parse_steps("100") == (100,)
    assert describe_steps((25, 50, 70)) == "25/50/70"


@pytest.mark.parametrize("bad", ["", "abc", "50,25", "25,25", "0,50", "50,150"])
def test_parse_steps_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_steps(bad)


def test_stepped_command_runs_all_stages_in_one_loop_with_markers():
    cmd = build_stepped_command((25, 50, 75, 100), 180)
    assert "for p in 25 50 75 100; do" in cmd
    assert 'echo "STRESS-STAGE $i/4 $p%"' in cmd
    assert "--cpu 0 --cpu-method matrixprod --cpu-load $p --timeout 180s --metrics-brief" in cmd
    assert "--matrix" not in cmd and "|| break" in cmd                    # uniform method, stop on error


def test_build_stress_command_classic_is_unchanged_and_stepped_uses_loop():
    classic = build_stress_command(StressConfig(node="n", duration=60), None)
    assert classic == ("stress-ng --cpu 0 --cpu-method matrixprod --matrix 0 "
                       "--timeout 60s --metrics-brief")
    stepped = build_stress_command(StressConfig(node="n", profile="stepped", duration=720), None)
    assert stepped.startswith("i=0; for p in 25 50 75 100;")


def test_stage_marker_regex_and_splitting():
    assert STAGE_RE.match("STRESS-STAGE 2/4 50%").groups() == ("2", "4", "50")
    assert STAGE_RE.match("stress-ng: info: ...") is None
    lines = ["STRESS-NG STARTED", "STRESS-STAGE 1/2 25%", "a", "b", "STRESS-STAGE 2/2 50%", "c"]
    assert split_stage_lines(lines) == {1: ["a", "b"], 2: ["c"]}


def test_count_completed_runs_ignores_unsuccessful():
    lines = ["stress-ng: info: [1] successful run completed in 3 mins",
             "stress-ng: info: [2] unsuccessful run completed in 1 min",
             "stress-ng: info: [3] successful run completed in 3 mins", "x"]
    assert count_completed_runs(lines) == 2


def test_cpu_summary_name_cores_threads_and_freq():
    from stress_test.parsing import cpu_summary
    assert cpu_summary("CPU: Intel(R) Core(TM) i7-4790S CPU @ 3.20GHz\nThreads: 8\nCores: 4") == \
        "Intel Core i7-4790S · 4c/8t @ 3.2 GHz"
    assert cpu_summary("CPU: Intel(R) Celeron(R) CPU G1840 @ 2.80GHz\nThreads: 2\nCores: 2") == \
        "Intel Celeron G1840 · 2c/2t @ 2.8 GHz"
    assert cpu_summary("CPU: AMD A10-9700E\nThreads: 4") == "AMD A10-9700E · 4t"        # no Cores line (old logs / lscpu missing)
    assert cpu_summary("Threads: 4") == ""
