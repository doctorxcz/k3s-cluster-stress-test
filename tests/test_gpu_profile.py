"""GPU profile (gpu-burn + nvidia-smi) against the fake kubectl: whole flow, failures, cleanup."""
import csv
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from stress_test.logparse import read_log
from test_integration import (FAKE, PAR_ENV, ROOT, TWO, console_files, event_times, kill_leftovers, node_logs,
                              run_series, run_tool, used_nodes, wait_for)

GPU = {"FAKE_GPU": "1", "FAKE_RUN": "4"}
BASE = ("--profile", "gpu", "--time", "30", "--yes")


def env(**kw):
    return dict(GPU, **kw)


def go(tmp_path, *extra, **kw):
    return run_tool(tmp_path, *BASE, *extra, env_extra=env(**kw))


def manifests(tmp_path):
    return [json.loads(p.read_text()) for p in tmp_path.glob("manifest-stress-test-*.json")]


def clean(tmp_path):
    """Every load pod that was created was also deleted."""
    made = {p.name[len("manifest-"):-len(".json")] for p in tmp_path.glob("manifest-stress-test-*.json")}
    gone = {p.name[len("deleted-"):] for p in tmp_path.glob("deleted-stress-test-*")}
    return made <= gone


def log_text(tmp_path):
    return next((tmp_path / "logs").rglob("*.log")).read_text(encoding="utf-8")


# ---- success -----------------------------------------------------------------------------------

def test_gpu_run_succeeds_and_logs_gpu_fields(tmp_path):
    res = go(tmp_path, "--export", "both")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Test completed" in res.stdout and "GPU TEST SUMMARY" in res.stdout
    assert "GPU INFO" in res.stdout and "Driver: 580.178.04" in res.stdout and "Memory: 2048 MiB" in res.stdout
    assert "GPU temperature:          max 60" in res.stdout and "nothing suspicious" in res.stdout
    text = log_text(tmp_path)
    assert "Profile: gpu" in text and " | GPU: 60°C 55.0W 1300MHz 100% 1800/2048MiB thr=0x0" in text
    # the pod: runtime nvidia, one GPU, unprivileged, no hostPath
    (pod,) = manifests(tmp_path)
    assert pod["spec"]["runtimeClassName"] == "nvidia"
    assert pod["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "hostPath" not in json.dumps(pod) and "privileged" not in json.dumps(pod).replace("allowPrivilegeEscalation", "")
    assert clean(tmp_path)


def test_gpu_exports_and_log_roundtrip_and_compare(tmp_path):
    for _ in range(2):
        assert go(tmp_path, "--export", "both").returncode == 0
        time.sleep(1.1)                                      # distinct log names
    data = json.loads(next((tmp_path / "logs").rglob("*.json")).read_text())
    assert data["profile"] == "gpu"
    gpu_samples = [s for s in data["samples"] if s["gpu_temp"] is not None]
    assert gpu_samples and gpu_samples[0]["gpu_power_w"] == 55.0 and gpu_samples[0]["gpu_util_pct"] == 100
    rows = list(csv.DictReader(next((tmp_path / "logs").rglob("*.csv")).open()))
    assert rows and rows[0]["gpu_temp"] == "60" and rows[0]["gpu_sm_mhz"] == "1300"
    log = sorted((tmp_path / "logs").rglob("*.log"))[0]
    run = read_log(str(log))
    assert run.profile == "gpu"
    test = [s for s in run.samples if s.phase == "test"]
    assert test and test[0].gpu_temp == 60 and test[0].gpu_throttle == 0 and test[0].gpu_mem_mib == 1800
    res = run_tool(tmp_path, "--compare", "fake-node")
    assert res.returncode == 0 and "Traceback" not in res.stdout + res.stderr, res.stdout + res.stderr


def test_power_na_like_quadro_p620_does_not_break(tmp_path):
    res = go(tmp_path, "--export", "both", FAKE_GPU_POWER="na")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "N/A (this card does not report power)" in res.stdout
    text = log_text(tmp_path)
    assert "GPU: 60°C ? 1300MHz" in text
    run = read_log(str(next((tmp_path / "logs").rglob("*.log"))))
    assert run.profile == "gpu" and all(s.gpu_power_w is None for s in run.samples if s.phase == "test")
    assert [s for s in run.samples if s.phase == "test"][0].gpu_temp == 60
    data = json.loads(next((tmp_path / "logs").rglob("*.json")).read_text())
    assert data["samples"][0]["gpu_power_w"] is None


def test_warning_below_the_limit_does_not_abort(tmp_path):
    res = go(tmp_path, FAKE_GPU_TEMP="77")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "GPU HOT" in log_text(tmp_path) and "GPU reached 77" in res.stdout
    assert "Test stopped" not in res.stdout


def test_throttle_mask_warns_in_summary(tmp_path):
    res = go(tmp_path, FAKE_GPU_THROTTLE="0x20")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "sw_thermal" in res.stdout and "Thermal throttling" in res.stdout and "thr=0x20" in log_text(tmp_path)


def test_idle_only_throttle_bit_is_harmless(tmp_path):
    res = go(tmp_path, FAKE_GPU_THROTTLE="0x1")
    assert res.returncode == 0 and "nothing suspicious" in res.stdout


# ---- GPU overheating -----------------------------------------------------------------------------

def test_gpu_overheat_aborts_with_gpu_message_and_deletes_pod(tmp_path):
    res = go(tmp_path, "--time", "60", FAKE_GPU_TEMP="90", FAKE_RUN="30")
    assert res.returncode == 3, res.stdout + res.stderr
    assert "Test stopped because of overheating" in res.stdout and "GPU 90°C ≥ 80°C" in res.stdout
    assert "Test stopped because of overheating" in log_text(tmp_path) and clean(tmp_path)


def test_custom_gpu_max_temp(tmp_path):
    res = go(tmp_path, "--time", "60", "--gpu-max-temp", "70", FAKE_GPU_TEMP="72", FAKE_RUN="30")
    assert res.returncode == 3 and "GPU 72°C ≥ 70°C" in res.stdout


def test_cpu_overheat_still_reported_as_cpu_in_gpu_test(tmp_path):
    res = go(tmp_path, "--time", "60", FAKE_TEMP="95", FAKE_RUN="30")
    assert res.returncode == 3, res.stdout + res.stderr
    assert "CPU 95°C" in res.stdout or "overheating" in res.stdout


# ---- unusable nodes ------------------------------------------------------------------------------

def test_node_without_gpu_gives_clear_error_and_nothing_stays(tmp_path):
    res = run_tool(tmp_path, *BASE, env_extra={"FAKE_RUN": "4"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "no NVIDIA GPU" in res.stdout and "Traceback" not in res.stdout + res.stderr
    assert manifests(tmp_path) == [] and clean(tmp_path)


def test_plugin_missing_is_explained(tmp_path):
    res = run_tool(tmp_path, *BASE, env_extra={"FAKE_RUN": "4", "FAKE_GPU": "plugin-missing"})
    assert res.returncode == 1 and "device plugin" in res.stdout and "nvidia.com/gpu" in res.stdout
    assert manifests(tmp_path) == []


def test_gpu_already_in_use_by_another_pod(tmp_path):
    res = go(tmp_path, FAKE_GPU_BUSY="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "already used by another pod" in res.stdout and manifests(tmp_path) == []


def test_master_node_gpu_test_runs(tmp_path):
    res = go(tmp_path, FAKE_MASTER="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Test completed" in res.stdout and clean(tmp_path)


# ---- gpu-burn outcomes ---------------------------------------------------------------------------

def test_faulty_gpu_is_an_error(tmp_path):
    res = go(tmp_path, FAKE_GPU_FAIL="faulty")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "faulty" in res.stdout and "The GPU test ended with an error" in res.stdout and "Test completed" not in res.stdout
    assert clean(tmp_path)


def test_gpu_burn_not_finishing_is_an_error(tmp_path):
    res = go(tmp_path, FAKE_GPU_FAIL="nofinish")
    assert res.returncode == 1 and "did not finish correctly" in res.stdout


def test_gpu_burn_build_failure_is_reported_and_cleaned(tmp_path):
    res = go(tmp_path, FAKE_GPU_FAIL="build")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "gpu-burn could not be started" in res.stdout and clean(tmp_path)


def test_nvidia_smi_failure_stops_the_test(tmp_path):
    # without nvidia-smi the GPU temperature cannot be watched -> the test stops (3 failed readings in a row)
    res = go(tmp_path, "--time", "60", FAKE_GPU_SMI_FAIL="1", FAKE_RUN="30")
    assert res.returncode == 3, res.stdout + res.stderr
    assert "nvidia-smi did not respond" in res.stdout and "Traceback" not in res.stdout + res.stderr
    assert clean(tmp_path)


# ---- parameters ----------------------------------------------------------------------------------

def test_gpu_image_goes_into_the_manifest_and_flags_into_the_script(tmp_path):
    res = go(tmp_path, "--gpu-image", "registry.local:5000/gpu/burn:1.0", "--gpu-mem-pct", "70", "--gpu-double")
    assert res.returncode == 0, res.stdout + res.stderr
    (pod,) = manifests(tmp_path)
    c = pod["spec"]["containers"][0]
    assert c["image"] == "registry.local:5000/gpu/burn:1.0"
    assert "-m 70% -d 30" in c["command"][-1] and "gpu_burn" in c["command"][-1]


def test_gpu_image_with_shell_characters_is_refused(tmp_path):
    res = go(tmp_path, "--gpu-image", "x;touch /tmp/pwned")
    assert res.returncode != 0 and manifests(tmp_path) == []
    assert "image" in res.stdout.lower()


@pytest.mark.parametrize("args", [("--time", "19"), ("--time", "3601"), ("--gpu-max-temp", "49"),
                                  ("--gpu-max-temp", "96"), ("--gpu-mem-pct", "5"), ("--gpu-mem-pct", "99")])
def test_parameter_bounds_are_refused(tmp_path, args):
    res = run_tool(tmp_path, "--profile", "gpu", "--yes", *args, env_extra=GPU)
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr
    assert manifests(tmp_path) == []


@pytest.mark.parametrize("args", [("--time", "20"), ("--time", "3600"), ("--gpu-max-temp", "50"),
                                  ("--gpu-max-temp", "95"), ("--gpu-mem-pct", "10"), ("--gpu-mem-pct", "95")])
def test_parameter_bounds_are_accepted_at_the_edge(tmp_path, args):
    a = ["--profile", "gpu", "--yes", "--time", "20", *args] if args[0] != "--time" else ["--profile", "gpu", "--yes", *args]
    if args[0] == "--time" and args[1] == "3600":
        pytest.skip("a one hour run is not simulated")
    res = run_tool(tmp_path, *a, env_extra=env(FAKE_RUN="2", FAKE_GPU_TEMP="45"))
    assert res.returncode == 0, res.stdout + res.stderr


def test_ram_and_hdd_are_ignored_for_gpu_profile(tmp_path):
    res = go(tmp_path, "--ram-pct", "50", "--hdd")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "loads only the GPU" in res.stdout
    assert "--vm" not in manifests(tmp_path)[0]["spec"]["containers"][0]["command"][-1]


def test_other_profiles_are_not_affected_by_gpu_env(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra=env(FAKE_RUN="2"))
    assert res.returncode == 0
    (pod,) = manifests(tmp_path)
    assert "runtimeClassName" not in pod["spec"]


# ---- interruption, background, several nodes -----------------------------------------------------

def test_ctrl_c_cleans_up_the_gpu_pod(tmp_path):
    e = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT),
             STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), **env(FAKE_RUN="60"))
    p = subprocess.Popen([sys.executable, "-m", "stress_test", "--node", "fake-node", "--non-interactive",
                          "--interval", "0.5", "--cooldown", "0", "--log-dir", str(tmp_path / "logs"),
                          "--profile", "gpu", "--time", "60", "--yes"], cwd=tmp_path, env=e,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert wait_for(lambda: (tmp_path / "events.txt").exists() and "start" in (tmp_path / "events.txt").read_text())
        time.sleep(1.5)
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out
    assert clean(tmp_path) and "Traceback" not in out


def test_gpu_in_background(tmp_path):
    try:
        res = run_tool(tmp_path, "--profile", "gpu", "--time", "20", "--background",
                       env_extra=env(FAKE_RUN="3"))
        assert res.returncode == 0, res.stdout + res.stderr
        assert wait_for(lambda: any("Test completed" in f.read_text(encoding="utf-8")
                                    for f in (tmp_path / "logs").rglob("*.log")))
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
        assert any("GPU:" in f.read_text(encoding="utf-8") for f in (tmp_path / "logs").rglob("*.log"))
    finally:
        kill_leftovers(tmp_path)


def test_parallel_workers_keep_the_gpu_profile(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--profile", "gpu", "--time", "20",
                     env_extra=dict(PAR_ENV, FAKE_NODES=TWO, FAKE_RUN="4", FAKE_GPU="1"))
    assert res.returncode == 0, res.stdout + res.stderr
    pods = manifests(tmp_path)
    assert len(pods) == 2 and all(p["spec"].get("runtimeClassName") == "nvidia" for p in pods)
    assert all("Profile: gpu" in f.read_text(encoding="utf-8") for f in node_logs(tmp_path))
    assert clean(tmp_path)


def test_sequential_workers_with_gpu_profile(tmp_path):
    res = run_series(tmp_path, "--workers", "--no-parallel", "--yes", "--profile", "gpu", "--time", "20",
                     env_extra=dict(FAKE_NODES=TWO, FAKE_RUN="3", FAKE_GPU="1"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert used_nodes(tmp_path) == ["w1", "w2"] and clean(tmp_path)


def test_gpu_pod_deadline_covers_image_pull_and_build(tmp_path):
    """The first pull of the 3 GB CUDA image + the gpu-burn build must fit into activeDeadlineSeconds."""
    assert go(tmp_path).returncode == 0
    (pod,) = manifests(tmp_path)
    assert pod["spec"]["activeDeadlineSeconds"] >= 30 + 1800
    other = tmp_path / "other"
    other.mkdir()
    assert run_tool(other, "--time", "5", env_extra={"FAKE_RUN": "2"}).returncode == 0
    (plain,) = manifests(other)
    assert plain["spec"]["activeDeadlineSeconds"] < 30 + 1800


def test_hot_log_line_is_parsed_back(tmp_path):
    from stress_test.logparse import parse_log
    text = ("=== KUBERNETES STRESS-NG LOG ===\nNode: n1\nStarted: 2026-10-01 10:00:00\nProfile: gpu - gpu-burn\n"
            "[10:00:05] CPU: 5%, RAM: 100 MiB (1%) | Temp: CPU: 40°C | Clock: 3000 MHz"
            " | GPU: 83°C ? 1200MHz 100% 1700MiB thr=0x20 ⚠️ GPU HOT!\n"
            "[10:00:10] CPU: 5%, RAM: 100 MiB (1%) | Temp: CPU: 40°C, GPU: 51°C | Clock: 3000 MHz\n")
    run = parse_log(text, "x.log")
    assert run.profile == "gpu" and len(run.samples) == 2
    assert (run.samples[0].gpu_temp, run.samples[0].gpu_throttle, run.samples[0].gpu_power_w) == (83, 0x20, None)
    assert run.samples[1].gpu_temp is None                         # an old log: "GPU: 51°C" is the CPU board sensor


# ---- cooldown, missing driver, limit (2026-10-01) -----------------------------------------------

def test_gpu_cooldown_reads_the_gpu_after_the_load(tmp_path):
    res = go(tmp_path, "--cooldown", "8", "--export", "json", FAKE_GPU_TEMP="70", FAKE_GPU_IDLE_TEMP="40")
    assert res.returncode == 0, res.stdout + res.stderr
    run = read_log(next((tmp_path / "logs").rglob("*.log")))
    cool = [s.gpu_temp for s in run.samples if s.phase == "cooldown" and s.gpu_temp is not None]
    assert cool, "the cooldown must contain GPU readings"
    assert "GPU cooldown:" in res.stdout and clean(tmp_path)


def test_missing_gpu_resource_with_no_hw_names_driver_and_plugin(tmp_path):
    res = run_tool(tmp_path, *BASE, "--no-hw", env_extra={"FAKE_RUN": "4"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert "driver" in res.stdout and "device plugin" in res.stdout and "--no-hw" in res.stdout


def test_gpu_limit_default_is_80_and_warning_5_below(tmp_path):
    res = go(tmp_path, FAKE_GPU_TEMP="76")
    assert res.returncode == 0 and "Stop at 80" in res.stdout and "warning from 75" in res.stdout
