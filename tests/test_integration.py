"""The whole flow against the fake kubectl (without a cluster)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FAKE = HERE / "fake_kubectl.py"
ROOT = HERE.parent


def run_tool(tmp_path, *extra, env_extra=None, timeout=60):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path),
               PYTHONPATH=str(ROOT),
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
    env.update(env_extra or {})
    extra = list(extra)
    if "--log-dir" not in extra:      # tests must not write into the real logs/ folder
        extra += ["--log-dir", str(tmp_path / "logs")]
    if "--cooldown" not in extra and "--status" not in extra and "--stop" not in extra:
        extra += ["--cooldown", "0"]  # cooldown is tried only in dedicated tests
    cmd = [sys.executable, "-m", "stress_test", "--node", "fake-node",
           "--non-interactive", "--interval", "0.5", *extra]
    return subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=timeout)


def test_normal_run_completes_and_writes_log(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--notes", "test",
                   env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Test completed" in res.stdout
    logs = list((tmp_path / "logs").glob("fake-node-5s-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "Notes: test" in text and "Fake CPU" in text
    assert "Temp: CPU: 40°C" in text and "Clock: 3000 MHz" in text


def test_overheat_stops_test(tmp_path):
    res = run_tool(tmp_path, "--time", "60", "--max-temp", "85", "--log",
                   env_extra={"FAKE_TEMP": "92", "FAKE_RUN": "30"})
    assert res.returncode == 3, res.stdout + res.stderr
    assert "stopped because of overheating" in res.stdout
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "stopped because of overheating" in log


def test_no_cpu_sensor_refused_without_flag(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_NO_SENSOR": "1"})
    assert res.returncode == 1
    assert "will NOT work" in res.stdout


def test_no_cpu_sensor_allowed_with_flag(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--allow-no-sensor",
                   env_extra={"FAKE_NO_SENSOR": "1", "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout


def test_master_capped_and_ram_disabled(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--yes", "--ram-pct", "80",
                   env_extra={"FAKE_MASTER": "1", "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "limited from 100 % to 70 %" in res.stdout
    assert "RAM test turned off" in res.stdout


def test_master_requires_confirmation_in_non_interactive(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_MASTER": "1"})
    assert res.returncode == 0
    assert "Exiting." in res.stdout and "Test running" not in res.stdout


def test_force_env_skips_master_protection(tmp_path):
    res = run_tool(tmp_path, "--time", "5",
                   env_extra={"FAKE_MASTER": "1", "FORCE": "1", "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "MASTER" not in res.stdout


def test_refuses_to_start_when_node_already_hot(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_TEMP_IDLE": "90"})
    assert res.returncode == 1
    assert "is already at" in res.stdout and "Test running" not in res.stdout


def test_summary_and_duration_units(tmp_path):
    res = run_tool(tmp_path, "--time", "1m", "--log",
                   env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "TEST SETTINGS" in res.stdout
    assert "1 min (60 s)" in res.stdout          # given as 1m
    assert "ends around" in res.stdout
    assert "Actual run time:" in res.stdout
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "Test duration: 1 min (60 s)" in log


def test_invalid_time_is_rejected(tmp_path):
    res = run_tool(tmp_path, "--time", "abc")
    assert res.returncode == 2                     # argparse error
    assert "Invalid duration" in res.stderr


def test_remaining_time_printed_and_logged(tmp_path):
    res = run_tool(tmp_path, "--time", "10m", "--log", "--remaining-every", "2",
                   env_extra={"FAKE_RUN": "4"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Remaining" in res.stdout and "of 10 min" in res.stdout
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "Remaining" in log


def test_remaining_can_be_disabled(tmp_path):
    res = run_tool(tmp_path, "--time", "10m", "--remaining-every", "0",
                   env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0
    assert "Remaining" not in res.stdout


def test_logs_go_to_given_folder_not_cwd(tmp_path):
    assert not (tmp_path / "logs").exists()
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    logs = list((tmp_path / "logs").glob("fake-node-5s-*.log"))
    assert len(logs) == 1
    assert list(tmp_path.glob("*.log")) == []      # nothing in the launch folder outside logs/
    assert str((tmp_path / "logs").resolve()) in res.stdout   # the full path is printed


def test_custom_log_dir(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--log-dir", str(tmp_path / "mine" / "results"),
                   env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(list((tmp_path / "mine" / "results").glob("*.log"))) == 1


# ---------------- hidden technical (debug) log ----------------------------------

def debug_logs(tmp_path):
    return sorted((tmp_path / "debug").glob("*.log"))


def test_debug_log_written_per_run_with_kubectl_calls(tmp_path):
    env = {"FAKE_RUN": "2", "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "5", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    files = debug_logs(tmp_path)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "===== START stress_test" in text
    assert "kubectl: " in text and "get node fake-node" in text
    assert "apply pod stress-test" in text
    assert "OUT " in text and "Test completed" in text      # and the user-facing output as well
    assert "===== END, exit code 0" in text


def test_each_run_gets_its_own_debug_log(tmp_path):
    env = {"FAKE_RUN": "1", "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    run_tool(tmp_path, "--time", "5", env_extra=env)
    run_tool(tmp_path, "--time", "5", env_extra=env)
    assert len(debug_logs(tmp_path)) == 2                    # nothing is overwritten


def test_debug_log_records_overheat_and_ticks(tmp_path):
    env = {"FAKE_TEMP": "92", "FAKE_RUN": "30",
           "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "60", env_extra=env)
    assert res.returncode == 3
    text = debug_logs(tmp_path)[0].read_text(encoding="utf-8")
    assert "OVERHEATING: CPU 92" in text
    assert "sample #1" in text and "guard: hot in a row=" in text
    assert "exit code 3" in text


def test_error_prints_debug_log_hint_and_logs_reason(tmp_path):
    env = {"FAKE_NO_SENSOR": "1", "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "5", env_extra=env)
    assert res.returncode == 1
    path = debug_logs(tmp_path)[0]
    assert "Technical details for debugging" in res.stdout and str(path) in res.stdout
    assert "Test failed" in path.read_text(encoding="utf-8")


def test_missing_kubectl_is_logged(tmp_path):
    env = {"KUBECTL": str(tmp_path / "does not exist"),
           "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "5", env_extra=env)
    assert res.returncode == 1
    assert "kubectl was not found" in debug_logs(tmp_path)[0].read_text(encoding="utf-8")


# ---------------- premature end and concurrent tests ---------------------------------

def test_premature_end_is_not_reported_as_success(tmp_path):
    """The pod disappears from outside after 2 s of 60 s -> no 'Test completed', code 4."""
    env = {"FAKE_RUN": "30", "FAKE_KILL_AFTER": "2",
           "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "60", "--log", env_extra=env)
    assert res.returncode == 4, res.stdout + res.stderr
    assert "PREMATURE" in res.stdout
    assert "of 1 min" in res.stdout
    assert "Test completed" not in res.stdout
    assert "the pod no longer exists" in res.stdout
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "PREMATURE" in log and "Test completed" not in log
    dbg = next((tmp_path / "debug").glob("*.log")).read_text(encoding="utf-8")
    assert "Premature end" in dbg


def test_normal_end_still_reports_success(tmp_path):
    res = run_tool(tmp_path, "--time", "60", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "Test completed" in res.stdout and "PREMATURE" not in res.stdout


def test_other_test_on_same_node_is_refused(tmp_path):
    others = '[{"name":"stress-test-abc","node":"fake-node","phase":"Running","run":"abc"}]'
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_OTHER_PODS": others})
    assert res.returncode == 1
    assert "another test is running" in res.stdout and "stress-test-abc" in res.stdout
    assert "Test running" not in res.stdout


def test_other_test_on_different_node_is_allowed(tmp_path):
    others = '[{"name":"stress-test-abc","node":"hp-g2-celeron","phase":"Running","run":"abc"}]'
    res = run_tool(tmp_path, "--time", "5",
                   env_extra={"FAKE_OTHER_PODS": others, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "Another test is running on node hp-g2-celeron" in res.stdout


def test_finished_pods_of_other_runs_do_not_block(tmp_path):
    others = '[{"name":"stress-test-old","node":"fake-node","phase":"Succeeded","run":"old"}]'
    res = run_tool(tmp_path, "--time", "5",
                   env_extra={"FAKE_OTHER_PODS": others, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout


def test_pod_names_in_debug_log_are_unique_between_runs(tmp_path):
    env = {"FAKE_RUN": "1", "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    run_tool(tmp_path, "--time", "5", env_extra=env)
    run_tool(tmp_path, "--time", "5", env_extra=env)
    import re
    ids = set()
    for f in (tmp_path / "debug").glob("*.log"):
        ids |= set(re.findall(r"apply pod (stress-test-[0-9a-f]{6})", f.read_text(encoding="utf-8")))
    assert len(ids) == 2                        # two runs = two different names


# ---------------- running in the background ------------------------------------------------------

import json
import signal
import time


def wait_for(predicate, timeout=45.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def kill_leftovers(tmp_path):
    """If the test failed and the child stayed running, terminate it."""
    for f in (tmp_path / "running").glob("*.json"):
        try:
            os.kill(json.loads(f.read_text())["pid"], signal.SIGKILL)
        except (OSError, ValueError, KeyError):
            pass


def console_files(tmp_path):
    return list((tmp_path / "logs").glob("*.console.txt"))


def result_logs(tmp_path):
    return list((tmp_path / "logs").glob("*.log"))


def test_background_returns_immediately_and_logs_automatically(tmp_path):
    started = time.time()
    try:
        res = run_tool(tmp_path, "--time", "5", "--background",
                       env_extra={"FAKE_RUN": "3"})          # without --log!
        assert res.returncode == 0, res.stdout + res.stderr
        assert time.time() - started < 6                      # the parent does not wait for the test
        assert "TEST RUNNING IN THE BACKGROUND" in res.stdout
        assert "Logging turned on automatically" in res.stdout   # logging turned on by itself
        assert "tail -f" in res.stdout and "--stop" in res.stdout
        # the test finishes in the background and writes the results and the output
        assert wait_for(lambda: any("Test completed" in f.read_text(encoding="utf-8")
                                    for f in result_logs(tmp_path)))
        assert len(console_files(tmp_path)) == 1
        console = console_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "TEST SETTINGS" in console and "Test completed" in console
        # the registry of the running test disappears when it ends
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
    finally:
        kill_leftovers(tmp_path)


def test_background_ignores_no_log(tmp_path):
    try:
        res = run_tool(tmp_path, "--time", "5", "--background", "--no-log",
                       env_extra={"FAKE_RUN": "2"})
        assert res.returncode == 0, res.stdout
        assert "--no-log is ignored" in res.stdout
        assert wait_for(lambda: len(result_logs(tmp_path)) == 1)
    finally:
        kill_leftovers(tmp_path)


def test_foreground_is_default_and_has_no_console_file(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0
    assert "IN THE BACKGROUND" not in res.stdout and "Test completed" in res.stdout
    assert console_files(tmp_path) == []


def test_status_shows_running_background_test_and_stop_ends_it(tmp_path):
    try:
        res = run_tool(tmp_path, "--time", "1h", "--background",
                       env_extra={"FAKE_RUN": "60"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert wait_for(lambda: len(list((tmp_path / "running").glob("*.json"))) == 1)
        # --status sees the running test
        st = run_tool(tmp_path, "--status", env_extra={"FAKE_OTHER_PODS": "[]"})
        assert st.returncode == 0
        assert "Background tests:" in st.stdout and "node fake-node" in st.stdout
        # the test is really already measuring (according to the background output)
        assert wait_for(lambda: any("Test running" in f.read_text(encoding="utf-8")
                                    for f in console_files(tmp_path)))
        # --stop ends it gracefully (SIGTERM -> pod cleanup)
        stop = run_tool(tmp_path, "--stop", "fake-node")
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert "stopped" in stop.stdout
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
        console = console_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "Test interrupted" in console
        assert "Test completed" not in console
    finally:
        kill_leftovers(tmp_path)


def test_stop_unknown_target(tmp_path):
    res = run_tool(tmp_path, "--stop", "does not exist")
    assert res.returncode == 1
    assert "matches" in res.stdout


def test_status_with_nothing_running(tmp_path):
    res = run_tool(tmp_path, "--status")
    assert res.returncode == 0
    assert "No background test is running." in res.stdout


def test_background_preflight_refuses_second_test_on_same_node_in_terminal(tmp_path):
    others = '[{"name":"stress-test-abc","node":"fake-node","phase":"Running","run":"abc"}]'
    res = run_tool(tmp_path, "--time", "5", "--background",
                   env_extra={"FAKE_OTHER_PODS": others})
    assert res.returncode == 1                               # the error is visible right away
    assert "another test is running" in res.stdout
    assert "TEST RUNNING IN THE BACKGROUND" not in res.stdout
    assert list((tmp_path / "running").glob("*.json")) == []


def test_interactive_question_default_is_no_and_yes_starts_background(tmp_path):
    def interactive(answers):
        env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path),
                   PYTHONPATH=str(ROOT), FAKE_RUN="2",
                   STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
        cmd = [sys.executable, "-m", "stress_test", "--node", "fake-node",
               "--interval", "0.5", "--cooldown", "0",
               "--log-dir", str(tmp_path / "logs")]
        return subprocess.run(cmd, cwd=tmp_path, env=env, input=answers,
                              capture_output=True, text=True, timeout=60)
    try:
        # order of questions: test type, duration, temperature, CPU, RAM, disk, BACKGROUND, (note)
        no = interactive("1\n5\n85\n100\nn\nn\n\nn\n")           # just Enter = default no
        assert no.returncode == 0, no.stdout + no.stderr
        assert "Run in the background?" in no.stdout and "TEST RUNNING IN THE BACKGROUND" not in no.stdout
        yes = interactive("1\n5\n85\n100\nn\nn\ny\n\n")
        assert yes.returncode == 0, yes.stdout + yes.stderr
        assert "TEST RUNNING IN THE BACKGROUND" in yes.stdout
        assert "Logging turned on automatically" in yes.stdout
        assert wait_for(lambda: len(console_files(tmp_path)) == 1)
    finally:
        kill_leftovers(tmp_path)


# ---------------- item 1: workload, RAM from MemAvailable, hw, permissions -------------------------

import stat


def manifest(tmp_path, prefix):
    files = sorted(tmp_path.glob(f"manifest-{prefix}*.json"))
    assert files, f"no manifest {prefix}*"
    return json.loads(files[-1].read_text())


LOBBY = '[{"ns":"minecraft-lobby","name":"lobby-abc"},{"ns":"kube-system","name":"coredns-1"}]'


def test_workload_refused_in_non_interactive_without_yes(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_NODE_PODS": LOBBY})
    assert res.returncode == 0
    assert "minecraft-lobby (1): lobby-abc" in res.stdout
    assert "Test refused" in res.stdout and "--yes" in res.stdout
    assert "Test running" not in res.stdout
    assert not list(tmp_path.glob("manifest-stress-test*.json"))       # nothing was started


def test_workload_confirmed_with_yes(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--yes",
                   env_extra={"FAKE_NODE_PODS": LOBBY, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "minecraft-lobby (1): lobby-abc" in res.stdout
    assert "+ 1 system pods" in res.stdout
    assert "Test completed" in res.stdout


def test_only_system_pods_need_no_confirmation(tmp_path):
    only_sys = '[{"ns":"kube-system","name":"coredns-1"},{"ns":"kube-system","name":"traefik-1"}]'
    res = run_tool(tmp_path, "--time", "5",
                   env_extra={"FAKE_NODE_PODS": only_sys, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "Only system pods are running on the node (2× kube-system)" in res.stdout


def test_finished_and_tool_pods_do_not_count_as_workload(tmp_path):
    pods = ('[{"ns":"minecraft","name":"old-job","phase":"Succeeded"},'
            '{"ns":"default","name":"stress-test-x","labels":{"app":"stress-test-tool"}}]')
    res = run_tool(tmp_path, "--time", "5",
                   env_extra={"FAKE_NODE_PODS": pods, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "Test refused" not in res.stdout


def test_master_confirmation_is_not_asked_twice(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--yes",
                   env_extra={"FAKE_MASTER": "1", "FAKE_NODE_PODS": LOBBY, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "MASTER" in res.stdout and "minecraft-lobby (1)" in res.stdout


def test_interactive_workload_question_defaults_to_no(tmp_path):
    def interactive(answers):
        env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path),
                   PYTHONPATH=str(ROOT), FAKE_RUN="2", FAKE_NODE_PODS=LOBBY,
                   STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
        cmd = [sys.executable, "-m", "stress_test", "--node", "fake-node",
               "--interval", "0.5", "--cooldown", "0",
               "--log-dir", str(tmp_path / "logs")]
        return subprocess.run(cmd, cwd=tmp_path, env=env, input=answers,
                              capture_output=True, text=True, timeout=60)
    no = interactive("\n")                                  # Enter = default no
    assert "Continue with the test?" in no.stdout
    assert "Exiting." in no.stdout and "TEST SETTINGS" not in no.stdout
    yes = interactive("y\n1\n5\n85\n100\nn\nn\nn\nn\n")         # y, duration, temperature, CPU, RAM, disk, background, log
    assert yes.returncode == 0, yes.stdout + yes.stderr
    assert "TEST SETTINGS" in yes.stdout


def test_ram_target_from_mem_available_and_memory_limit(tmp_path):
    env = {"FAKE_MEM_AVAILABLE_KB": "8388608", "FAKE_RUN": "2",           # 8192 MiB free
           "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "5", "--ram-pct", "50", "--log", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "RAM test: 4096 MiB (50 % of 8192 MiB free, source: MemAvailable)" in res.stdout
    pod = manifest(tmp_path, "stress-test")
    cmd = pod["spec"]["containers"][0]["command"][2]
    assert "--vm-bytes 4096M" in cmd
    assert pod["spec"]["containers"][0]["resources"]["limits"]["memory"] == "5120Mi"
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "RAM target: 4096 MiB" in log
    dbg = next((tmp_path / "debug").glob("*.log")).read_text(encoding="utf-8")
    assert "memory limit 5120 MiB" in dbg


def test_ram_target_keeps_512_mib_reserve(tmp_path):
    env = {"FAKE_MEM_AVAILABLE_KB": "2097152", "FAKE_RUN": "2"}           # 2048 MiB free
    res = run_tool(tmp_path, "--time", "5", "--ram-pct", "95", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert "RAM test: 1536 MiB" in res.stdout                            # 2048 - 512, not 1945


def test_ram_test_disabled_when_almost_no_memory(tmp_path):
    env = {"FAKE_MEM_AVAILABLE_KB": "409600", "FAKE_RUN": "2"}            # 400 MiB free
    res = run_tool(tmp_path, "--time", "5", "--ram-pct", "80", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert "Little free memory" in res.stdout and "turning the RAM test off" in res.stdout
    cmd = manifest(tmp_path, "stress-test")["spec"]["containers"][0]["command"][2]
    assert "--vm" not in cmd


def test_ram_fallback_to_kubectl_top_without_mem_available(tmp_path):
    env = {"FAKE_NO_MEM": "1", "FAKE_RUN": "2"}
    res = run_tool(tmp_path, "--time", "5", "--ram-pct", "50", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert "source: kubectl top" in res.stdout


def test_cpu_only_test_has_base_memory_limit(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0
    pod = manifest(tmp_path, "stress-test")
    assert pod["spec"]["containers"][0]["resources"]["limits"]["memory"] == "1536Mi"
    assert pod["spec"]["containers"][0]["image"] == "ubuntu:24.04"


def test_hw_pod_unprivileged_by_default(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0
    hw = manifest(tmp_path, "hw-info")
    assert "privileged" not in hw["spec"]["containers"][0]["securityContext"]
    assert hw["spec"]["volumes"] == []
    assert "PRIVILEGED" not in res.stdout


def test_hw_privileged_flag_is_explicit_and_announced(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--hw-privileged", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0
    assert manifest(tmp_path, "hw-info")["spec"]["containers"][0]["securityContext"] == {"privileged": True}
    assert "PRIVILEGED" in res.stdout


def test_no_hw_skips_the_hw_pod(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--no-hw", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout
    assert "Hardware detection skipped" in res.stdout
    assert not list(tmp_path.glob("manifest-hw-info*.json"))
    assert list(tmp_path.glob("manifest-temp-probe*.json"))              # the probe keeps running


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_log_files_and_dirs_are_private(tmp_path):
    env = {"FAKE_RUN": "2", "STRESS_TEST_DEBUG_DIR": str(tmp_path / "debug")}
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert mode(next((tmp_path / "logs").glob("*.log"))) == 0o600         # results
    assert mode(next((tmp_path / "debug").glob("*.log"))) == 0o600        # debug log
    assert mode(tmp_path / "logs") == 0o700 and mode(tmp_path / "debug") == 0o700


def test_background_console_file_and_record_are_private(tmp_path):
    try:
        res = run_tool(tmp_path, "--time", "1h", "--background", env_extra={"FAKE_RUN": "60"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert wait_for(lambda: len(list((tmp_path / "running").glob("*.json"))) == 1)
        assert mode(console_files(tmp_path)[0]) == 0o600
        assert mode(next((tmp_path / "running").glob("*.json"))) == 0o600
        run_tool(tmp_path, "--stop", "fake-node")
    finally:
        kill_leftovers(tmp_path)


# ---------------- summary at the end and cooldown ------------------------------------------------

def test_cooldown_measures_after_test_and_summary_is_shown_and_logged(tmp_path):
    env = {"FAKE_TEMP": "60", "FAKE_TEMP_IDLE": "40", "FAKE_COOL_STEP": "5", "FAKE_RUN": "3"}
    res = run_tool(tmp_path, "--time", "5", "--log", "--cooldown", "3", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "The load has ended. For another 3 s" in out
    assert "❄️" in out                                        # measurement lines during cooldown
    assert "TEST SUMMARY" in out and "Cooldown (" in out
    assert "cpu 658.9 | matrix 1458.5" in out                 # stress-ng performance
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "TEST SUMMARY" in log and "Cooldown (" in log     # the summary is in the log too
    assert "[cooldown] " in log and "Cooldown after test: 3 s" in log
    # order: test result, cooldown, summary, only then the log path
    assert out.index("Test completed") < out.index("The load has ended") < out.index("TEST SUMMARY")


def test_summary_is_shown_even_without_cooldown(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "2"})   # cooldown 0
    assert res.returncode == 0
    assert "TEST SUMMARY" in res.stdout and "The load has ended" not in res.stdout
    assert "Cooldown (" not in res.stdout


def test_cpu_and_ram_are_live_from_probe_not_from_metrics_server(tmp_path):
    env = {"FAKE_CPU_BUSY": "100", "FAKE_MEM_AVAILABLE_KB": "6000000", "FAKE_RUN": "3"}
    res = run_tool(tmp_path, "--time", "5", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert "CPU: 100%, RAM: 1953 MiB (25%)" in res.stdout      # (8000000-6000000) kB of 8000000 kB
    assert "top node" not in res.stdout


def test_throttling_is_reported_in_summary(tmp_path):
    env = {"FAKE_FREQ_DROP": "1", "FAKE_RUN": "8"}
    res = run_tool(tmp_path, "--time", "60", env_extra=env)
    assert res.returncode == 0, res.stdout
    assert "SUSPECTED THROTTLING" in res.stdout


def test_overheat_still_gets_cooldown_and_summary(tmp_path):
    env = {"FAKE_TEMP": "92", "FAKE_RUN": "30"}
    res = run_tool(tmp_path, "--time", "60", "--cooldown", "2", env_extra=env)
    assert res.returncode == 3, res.stdout + res.stderr
    out = res.stdout
    assert "stopped because of overheating" in out and "The load has ended" in out
    assert "TEST SUMMARY" in out and "max 92 °C" in out and "check the cooling" in out


def test_invalid_cooldown_is_rejected(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--cooldown", "abc")
    assert res.returncode == 2 and "Invalid duration" in res.stderr


def test_interrupt_during_test_prints_summary_without_cooldown(tmp_path):
    try:
        res = run_tool(tmp_path, "--time", "1h", "--background", "--cooldown", "60",
                       env_extra={"FAKE_RUN": "60"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert wait_for(lambda: any(
            "CPU: 100%" in f.read_text(encoding="utf-8") for f in console_files(tmp_path)))
        run_tool(tmp_path, "--stop", "fake-node")
        assert wait_for(lambda: any(
            "TEST SUMMARY" in f.read_text(encoding="utf-8") for f in console_files(tmp_path)))
        console = console_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "Test interrupted" in console and "The load has ended" not in console
    finally:
        kill_leftovers(tmp_path)


def test_stop_during_cooldown_skips_it_and_still_prints_summary(tmp_path):
    try:
        res = run_tool(tmp_path, "--time", "5", "--background", "--cooldown", "300",
                       env_extra={"FAKE_RUN": "2"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert wait_for(lambda: any(
            "The load has ended" in f.read_text(encoding="utf-8") for f in console_files(tmp_path)))
        stopped = time.time()
        stop = run_tool(tmp_path, "--stop", "fake-node")
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert time.time() - stopped < 25                       # did not wait 300 s
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
        console = console_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "Cooldown skipped" in console and "TEST SUMMARY" in console
    finally:
        kill_leftovers(tmp_path)


# ---------------- two cooldown phases and --compare (version 1.4.1) ------------------------------------

def _run_logged(tmp_path, temp, run="4"):
    env = {"FAKE_TEMP": str(temp), "FAKE_TEMP_IDLE": "40", "FAKE_COOL_STEP": "4", "FAKE_RUN": run}
    res = run_tool(tmp_path, "--time", "5", "--log", "--cooldown", "3", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    return res


def test_summary_and_log_contain_jump_and_slow_cooling_rows(tmp_path):
    res = _run_logged(tmp_path, 70)
    assert "Drop after load stops:" in res.stdout and "Slow cooldown:" in res.stdout
    log = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "Drop after load stops:" in log and "Slow cooldown:" in log


def test_compare_two_real_logs_by_node_name_and_by_paths(tmp_path):
    _run_logged(tmp_path, 70)
    time.sleep(1.1)                                       # a different timestamp in the log name
    _run_logged(tmp_path, 60)
    logs = sorted((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 2

    by_node = run_tool(tmp_path, "--compare", "fake-node")
    assert by_node.returncode == 0, by_node.stdout + by_node.stderr
    out = by_node.stdout
    assert "TEST COMPARISON" in out and logs[0].name in out and logs[1].name in out
    max_line = next(l for l in out.splitlines() if l.startswith("CPU temp max:"))
    assert "70 °C" in max_line and "60 °C" in max_line and "(better)" in max_line
    assert "Idle before test:" in out and "40 °C" in out
    assert "Performance cpu (bogo ops/s):" in out

    by_paths = run_tool(tmp_path, "--compare", str(logs[1]), logs[0].name)   # reversed: B is older
    assert by_paths.returncode == 0, by_paths.stdout
    swapped = next(l for l in by_paths.stdout.splitlines() if l.startswith("CPU temp max:"))
    assert "(worse)" in swapped                            # 60 -> 70 is worse


def test_compare_errors_are_reported_without_traceback(tmp_path):
    one = run_tool(tmp_path, "--compare", "nonexistent-node")
    assert one.returncode == 1 and "only 0 log" in one.stdout and "Traceback" not in one.stderr
    bad = tmp_path / "logs"
    bad.mkdir(exist_ok=True)
    (bad / "a.log").write_text("nothing\n", encoding="utf-8")
    (bad / "b.log").write_text("nothing\n", encoding="utf-8")
    res = run_tool(tmp_path, "--compare", "a.log", "b.log")
    assert res.returncode == 1 and "is not a result log" in res.stdout
    three = run_tool(tmp_path, "--compare", "a", "b", "c")
    assert three.returncode == 1 and "Give two logs" in three.stdout


# ---------------- version 1.5.0: stepped test and node series ------------------------------------------------

import re as _re

THREE = '[{"name":"w1"},{"name":"w2"},{"name":"m1","master":true}]'
TWO = '[{"name":"w1"},{"name":"w2"}]'


def run_series(tmp_path, *extra, env_extra=None, timeout=180, stdin=None, interactive=False):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT),
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
    env.update(env_extra or {})
    extra = list(extra)
    if "--log-dir" not in extra:
        extra += ["--log-dir", str(tmp_path / "logs")]
    if not any(a in extra for a in ("--cooldown", "--status", "--stop")):
        extra += ["--cooldown", "0"]
    cmd = [sys.executable, "-m", "stress_test", "--interval", "0.5"]
    if not interactive:
        cmd.append("--non-interactive")
    cmd += extra
    return subprocess.run(cmd, cwd=tmp_path, env=env, input=stdin, capture_output=True,
                          text=True, timeout=timeout)


def used_nodes(tmp_path):
    """Nodes on which the load was actually started (from the saved pod manifests)."""
    nodes = []
    for f in sorted(tmp_path.glob("manifest-stress-test-*.json"), key=lambda p: p.stat().st_mtime):
        nodes.append(json.loads(f.read_text())["spec"]["nodeName"])
    return nodes


def node_logs(tmp_path, prefix=""):
    return sorted(p for p in (tmp_path / "logs").glob(f"{prefix}*.log") if not p.name.startswith("cluster-"))


def cluster_logs(tmp_path):
    return sorted((tmp_path / "logs").glob("cluster-*.log"))


STEPPED_ENV = {"FAKE_RUN": "8", "FAKE_TEMP": "80", "FAKE_TEMP_IDLE": "40"}


def test_stepped_single_node_runs_all_stages_and_logs(tmp_path):
    env = dict(STEPPED_ENV, STRESS_TEST_DEBUG_DIR=str(tmp_path / "debug"))
    res = run_tool(tmp_path, "--profile", "stepped", "--step-time", "30s", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "Logging turned on automatically (stepped test)" in out
    assert "stepped 25/50/75/100 % for 30 s" in out and "2 min (120 s) in total" in out
    for i, pct in enumerate((25, 50, 75, 100), 1):
        assert f"▶ Stage {i}/4: {pct} % (30 s)" in out
    assert "STRESS-STAGE" not in out                       # the markers are not printed raw
    assert "Test completed" in out and "PREMATURE" not in out
    assert "Stages (target → measured):" in out
    rows = {int(m.group(1)): m.group(0) for m in _re.finditer(r"^  (\d)/4 .*$", out, _re.M)}
    assert set(rows) == {1, 2, 3, 4}
    assert "max 80 °C" in rows[4]                          # 100 % = full temperature
    assert int(_re.search(r"max (\d+) °C", rows[1]).group(1)) <= 60     # 25 % is cool
    assert "no signs of throttling in any stage" in out
    # the pod command is a loop over the stages
    pod = manifest(tmp_path, "stress-test")
    assert "for p in 25 50 75 100" in pod["spec"]["containers"][0]["command"][2]
    # log: header with the profile, stage markers, summary
    log = node_logs(tmp_path)[0].read_text(encoding="utf-8")
    assert "Profile: stepped 25/50/75/100 % for 30 s" in log and "CPU load: gradually 25/50/75/100 %" in log
    assert all(f"▶ Stage {i}/4" in log for i in (1, 2, 3, 4))
    assert "Stages (target → measured):" in log and "TEST SUMMARY" in log


def test_stepped_stops_being_success_when_a_later_stage_is_killed(tmp_path):
    """Two stages finish (messages 'successful run completed'), the third is interrupted -> premature end."""
    env = {"FAKE_RUN": "16", "FAKE_KILL_AFTER": "10"}         # 4 s per stage, end at 10 s
    res = run_tool(tmp_path, "--profile", "stepped", "--step-time", "30s", env_extra=env)
    assert res.returncode == 4, res.stdout + res.stderr
    assert "PREMATURE" in res.stdout and "Test completed" not in res.stdout


def test_stepped_ignores_time_and_ram_and_supports_custom_steps(tmp_path):
    res = run_tool(tmp_path, "--profile", "stepped", "--steps", "40,80", "--step-time", "30s",
                   "--time", "5", "--ram-pct", "50", env_extra={"FAKE_RUN": "4"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--time is ignored" in res.stdout and "RAM and disk are ignored" in res.stdout
    assert "▶ Stage 2/2: 80 %" in res.stdout and "1 min (60 s) in total" in res.stdout
    assert "--vm" not in manifest(tmp_path, "stress-test")["spec"]["containers"][0]["command"][2]


def test_stepped_invalid_steps_are_rejected(tmp_path):
    res = run_tool(tmp_path, "--profile", "stepped", "--steps", "50,25")
    assert res.returncode == 2 and "Stages must be ascending" in res.stderr


def test_stepped_on_master_is_capped_at_70_percent(tmp_path):
    res = run_tool(tmp_path, "--profile", "stepped", "--step-time", "30s", "--yes",
                   env_extra={"FAKE_MASTER": "1", "FAKE_RUN": "6"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stages limited to at most 70 %: 25/50/70" in res.stdout
    assert "▶ Stage 3/3: 70 %" in res.stdout and "4/4" not in res.stdout


def test_classic_profile_is_still_the_default_and_unchanged(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0
    cmd = manifest(tmp_path, "stress-test")["spec"]["containers"][0]["command"][2]
    assert "--matrix 0" in cmd and "STRESS-STAGE" not in cmd
    assert "Profile" not in res.stdout and "Stages" not in res.stdout


def test_compare_two_stepped_runs_shows_per_stage_rows(tmp_path):
    for temp in (84, 70):
        res = run_tool(tmp_path, "--profile", "stepped", "--step-time", "30s", "--cooldown", "2",
                       env_extra={"FAKE_RUN": "8", "FAKE_TEMP": str(temp), "FAKE_TEMP_IDLE": "40"})
        assert res.returncode == 0, res.stdout + res.stderr
        time.sleep(1.1)
    res = run_tool(tmp_path, "--compare", "fake-node")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stepped 25/50/75/100 %" in res.stdout
    row = next(l for l in res.stdout.splitlines() if l.startswith("Stage 4/4 (100 %) max:"))
    assert "84 °C" in row and "70 °C" in row and "(better)" in row


# ---------------- node series -----------------------------------------------------------------------------

def test_workers_series_runs_workers_in_order_without_master(tmp_path):
    res = run_series(tmp_path, "--workers", "--yes", "--time", "5",
                     env_extra={"FAKE_NODES": THREE, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "Test 2 nodes one after another" in out and "Logging turned on automatically (multi-node test)" in out
    assert out.index("NODE 1/2: w1") < out.index("NODE 2/2: w2") and "NODE 3/" not in out
    assert used_nodes(tmp_path) == ["w1", "w2"]
    assert len(node_logs(tmp_path, "w1-")) == 1 and len(node_logs(tmp_path, "w2-")) == 1
    (cluster,) = cluster_logs(tmp_path)
    text = cluster.read_text(encoding="utf-8")
    assert "=== CLUSTER TEST ===" in text and "Nodes: w1, w2" in text
    assert "Node w1: OK" in text and "Node w2: OK" in text and "CLUSTER SUMMARY" in text
    assert mode(cluster) == 0o600
    assert "CLUSTER SUMMARY" in out and "Hottest:" in out


def test_cluster_series_puts_master_last_with_70_percent_cap(tmp_path):
    res = run_series(tmp_path, "--cluster", "--yes", "--time", "5",
                     env_extra={"FAKE_NODES": THREE, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert out.index("NODE 2/3: w2") < out.index("NODE 3/3: m1 (master)")
    assert "CPU load limited from 100 % to 70 %" in out and "m1 (master)" in out.split("CLUSTER SUMMARY")[1]
    assert used_nodes(tmp_path) == ["w1", "w2", "m1"]
    master_pod = next(json.loads(f.read_text()) for f in tmp_path.glob("manifest-stress-test-*.json")
                      if json.loads(f.read_text())["spec"]["nodeName"] == "m1")
    assert "--cpu-load 70" in master_pod["spec"]["containers"][0]["command"][2]


def test_cluster_stepped_series_keeps_master_with_yes_and_lists_stage_temperatures(tmp_path):
    env = dict(STEPPED_ENV, FAKE_NODES=THREE, FAKE_RUN="4")
    res = run_series(tmp_path, "--cluster", "--profile", "stepped", "--step-time", "30s", "--yes",
                     env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert used_nodes(tmp_path) == ["w1", "w2", "m1"]
    assert "Master: stages limited to at most 70 %: 25/50/70." in out
    assert "Temperatures per stage (max):" in out and "100 % →" in out
    assert "Estimate in total:" in out


def test_cluster_stepped_without_confirmation_drops_master_and_then_refuses(tmp_path):
    res = run_series(tmp_path, "--cluster", "--profile", "stepped", "--step-time", "30s",
                     env_extra={"FAKE_NODES": THREE})
    assert res.returncode == 0
    assert "Master left out of the stepped test" in res.stdout and "Test refused" in res.stdout
    assert used_nodes(tmp_path) == []


def test_include_master_flag_keeps_master_without_question(tmp_path):
    res = run_series(tmp_path, "--workers", "--include-master", "--profile", "stepped",
                     "--step-time", "30s", "--steps", "25,100", "--yes",
                     env_extra={"FAKE_NODES": THREE, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert used_nodes(tmp_path) == ["w1", "w2", "m1"]


def test_interactive_series_asks_whether_to_keep_master(tmp_path):
    env = {"FAKE_NODES": THREE, "FAKE_RUN": "2"}
    args = ("--cluster", "--profile", "stepped", "--step-time", "30s", "--steps", "25,100")
    # max. temperature, background, note, KEEP MASTER?, at once?, start?
    no = run_series(tmp_path, *args, env_extra=env, stdin="\nn\n\nn\nn\ny\n", interactive=True)
    assert no.returncode == 0, no.stdout + no.stderr
    assert "Keep the master in the stepped test" in no.stdout and "25/70" in no.stdout
    assert "The master is left out of the test" in no.stdout
    assert used_nodes(tmp_path) == ["w1", "w2"]


def test_interactive_series_can_keep_master(tmp_path):
    env = {"FAKE_NODES": THREE, "FAKE_RUN": "2"}
    args = ("--cluster", "--profile", "stepped", "--step-time", "30s", "--steps", "25,100")
    yes = run_series(tmp_path, *args, env_extra=env, stdin="\nn\n\ny\nn\ny\n", interactive=True)
    assert yes.returncode == 0, yes.stdout + yes.stderr
    assert used_nodes(tmp_path) == ["w1", "w2", "m1"]


def test_interactive_scope_menu_offers_single_workers_and_cluster(tmp_path):
    # scope 2 (workers), type 1 (classic), duration, temperature, CPU, RAM, disk, background, note, start
    res = run_series(tmp_path, env_extra={"FAKE_NODES": THREE, "FAKE_RUN": "2"},
                     stdin="2\n1\n5\n\n\nn\nn\nn\n\nn\ny\n", interactive=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "What to test?" in res.stdout and "the whole cluster" in res.stdout
    assert used_nodes(tmp_path) == ["w1", "w2"]


def test_series_skips_not_ready_node_and_continues(tmp_path):
    nodes = '[{"name":"w1","ready":false},{"name":"w2"}]'
    res = run_series(tmp_path, "--workers", "--yes", "--time", "5",
                     env_extra={"FAKE_NODES": nodes, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Node w1 skipped: node is not Ready" in res.stdout
    summary = res.stdout.split("CLUSTER SUMMARY")[1]
    assert "w1" in summary and "SKIPPED" in summary and "w2" in summary
    assert used_nodes(tmp_path) == ["w2"]


def test_series_continues_after_overheat_and_returns_worst_code(tmp_path):
    res = run_series(tmp_path, "--workers", "--yes", "--time", "60",
                     env_extra={"FAKE_NODES": TWO, "FAKE_TEMP": "92", "FAKE_RUN": "30"})
    assert res.returncode == 3, res.stdout + res.stderr
    assert used_nodes(tmp_path) == ["w1", "w2"]                 # the failure of the first did not stop the series
    summary = res.stdout.split("CLUSTER SUMMARY")[1]
    assert summary.count("OVERHEATED") == 2


def test_series_argument_errors(tmp_path):
    unknown = run_series(tmp_path, "--nodes", "w1,nobody", "--yes", env_extra={"FAKE_NODES": TWO})
    assert unknown.returncode == 1 and "Unknown nodes: nobody" in unknown.stdout
    both = run_series(tmp_path, "--workers", "--cluster", "--yes", env_extra={"FAKE_NODES": TWO})
    assert both.returncode == 1 and "only one of the options" in both.stdout
    with_node = run_series(tmp_path, "--workers", "--node", "w1", "--yes", env_extra={"FAKE_NODES": TWO})
    assert with_node.returncode == 1 and "cannot be combined" in with_node.stdout


def test_nodes_list_can_include_master_explicitly(tmp_path):
    res = run_series(tmp_path, "--nodes", "m1,w1", "--yes", "--time", "5",
                     env_extra={"FAKE_NODES": THREE, "FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert used_nodes(tmp_path) == ["w1", "m1"]                   # master always last


def test_series_refuses_node_with_running_test(tmp_path):
    busy = '[{"name":"stress-test-abc","node":"w1","phase":"Running","run":"abc"}]'
    res = run_series(tmp_path, "--workers", "--yes", "--time", "5",
                     env_extra={"FAKE_NODES": TWO, "FAKE_OTHER_PODS": busy})
    assert res.returncode == 1 and "On nodes w1 another test is running" in res.stdout
    assert used_nodes(tmp_path) == []


def test_series_overview_lists_services_and_needs_confirmation(tmp_path):
    pods = '{"w1": [{"ns":"minecraft","name":"mc-1"}]}'
    res = run_series(tmp_path, "--workers", "--time", "5",
                     env_extra={"FAKE_NODES": TWO, "FAKE_NODE_PODS_BY_NODE": pods})
    assert res.returncode == 0
    assert "services: minecraft (1): mc-1" in res.stdout and "Estimate in total:" in res.stdout
    assert "Test refused: confirm with --yes." in res.stdout
    assert used_nodes(tmp_path) == []


def test_series_in_background_can_be_stopped_and_still_writes_summary(tmp_path):
    try:
        res = run_series(tmp_path, "--workers", "--yes", "--background", "--time", "60",
                         env_extra={"FAKE_NODES": TWO, "FAKE_RUN": "60"})
        assert res.returncode == 0, res.stdout + res.stderr
        assert "TEST RUNNING IN THE BACKGROUND" in res.stdout and "cluster (2 nodes: w1, w2)" in res.stdout
        assert wait_for(lambda: any("Test running" in f.read_text(encoding="utf-8")
                                    for f in console_files(tmp_path)))
        status = run_series(tmp_path, "--status")
        assert "node cluster" in status.stdout
        stop = run_series(tmp_path, "--stop", "cluster")
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
        console = console_files(tmp_path)[0].read_text(encoding="utf-8")
        assert "Series interrupted" in console and "CLUSTER SUMMARY" in console
        assert "not tested (series interrupted)" in console            # w2 was not started anymore
        assert used_nodes(tmp_path) == ["w1"]
    finally:
        kill_leftovers(tmp_path)


# ---------------- version 1.6.0: workers at once, master protection ----------------------------------------------

PAR_ENV = {"STRESS_TEST_TABLE_INTERVAL": "1", "STRESS_TEST_API_INTERVAL": "1"}


def read_events(tmp_path):
    """(time, start|end, node) from the fake kubectl: when the load was actually running on the node."""
    events = []
    for line in (tmp_path / "events.txt").read_text().splitlines():
        t, kind, node_name = line.split()
        events.append((float(t), kind, node_name))
    return events


def event_times(tmp_path, kind):
    return {n: t for t, k, n in read_events(tmp_path) if k == kind}


def children_alive(tmp_path):
    out = subprocess.run(["pgrep", "-f", "--", f"--log-file {tmp_path}"], capture_output=True, text=True)
    return [p for p in out.stdout.split() if p]


def par_env(**kw):
    return dict(PAR_ENV, **kw)


def test_parallel_workers_run_at_the_same_time(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--time", "5",
                     env_extra=par_env(FAKE_NODES=THREE, FAKE_RUN="6"))
    assert res.returncode == 0, res.stdout + res.stderr
    starts, ends = event_times(tmp_path, "start"), event_times(tmp_path, "end")
    assert set(starts) == {"w1", "w2"} and set(ends) == {"w1", "w2"}
    assert max(starts.values()) < min(ends.values())                     # both ran at the same time
    out = res.stdout
    assert "2 workers AT ONCE" in out and "Started 2 workers at once: w1, w2" in out
    assert "node status:" in out and "done: OK" in out and "power draw from the socket" in out
    assert "API guard:" in out and "CLUSTER SUMMARY" in out
    (cluster,) = cluster_logs(tmp_path)
    text = cluster.read_text(encoding="utf-8")
    assert "Concurrency: workers at once, master alone afterwards" in text
    assert "Node w1: OK" in text and "Node w2: OK" in text and "subprocess PID" in text
    logs = node_logs(tmp_path)
    assert len(logs) == 2
    for f in logs:
        body = f.read_text(encoding="utf-8")
        assert "Concurrency: yes (2 nodes at once)" in body and "TEST SUMMARY" in body
        assert mode(f) == 0o600
    assert len(list((tmp_path / "logs").glob("w*.console.txt"))) == 2       # output of the subprocesses
    assert children_alive(tmp_path) == []


def test_parallel_cluster_runs_master_after_workers_and_alone(tmp_path):
    res = run_series(tmp_path, "--cluster", "--parallel", "--yes", "--time", "5",
                     env_extra=par_env(FAKE_NODES=THREE, FAKE_RUN="4"))
    assert res.returncode == 0, res.stdout + res.stderr
    starts, ends = event_times(tmp_path, "start"), event_times(tmp_path, "end")
    assert set(starts) == {"w1", "w2", "m1"}
    assert starts["m1"] >= max(ends["w1"], ends["w2"])                    # master only after the workers
    assert max(starts["w1"], starts["w2"]) < min(ends["w1"], ends["w2"])  # workers together
    out = res.stdout
    assert "NODE 3/3: m1 (master)" in out
    assert "Master: CPU load limited from 100 % to 70 %" in out
    assert "Master: temperature limit lowered from 85 °C to 80 °C" in out
    assert "--max-temp" not in out


def test_parallel_stepped_table_shows_current_stage_of_each_worker(tmp_path):
    env = par_env(FAKE_NODES=TWO, FAKE_RUN="8", FAKE_TEMP="80", FAKE_TEMP_IDLE="40")
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--profile", "stepped",
                     "--step-time", "30s", env_extra=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _re.search(r"^\s+w1\s+▶ Stage \d/4 \(\d+ %\)", res.stdout, _re.M)
    assert _re.search(r"^\s+w2\s+▶ Stage \d/4 \(\d+ %\)", res.stdout, _re.M)
    assert "Temperatures per stage (max):" in res.stdout
    starts, ends = event_times(tmp_path, "start"), event_times(tmp_path, "end")
    assert max(starts.values()) < min(ends.values())


def test_parallel_api_guard_stops_all_tests_and_master_is_not_started(tmp_path):
    env = par_env(FAKE_NODES=THREE, FAKE_RUN="60", FAKE_API_DELAY="2")
    res = run_series(tmp_path, "--cluster", "--parallel", "--yes", "--time", "60",
                     "--api-limit", "0.5", env_extra=env, timeout=240)
    assert res.returncode == 1, res.stdout + res.stderr
    out = res.stdout
    assert "Series stopped: API response" in out and "stopping all tests" in out
    summary = out.split("CLUSTER SUMMARY")[1]
    assert summary.count("INTERRUPTED") == 2 and "not tested (stopped: API response" in summary
    assert sorted(used_nodes(tmp_path)) == ["w1", "w2"]                    # the master was not started
    assert len(list(tmp_path.glob("deleted-stress-test-*"))) == 2          # pods deleted
    assert children_alive(tmp_path) == []


def test_parallel_overheat_of_both_workers_returns_3(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--time", "60",
                     env_extra=par_env(FAKE_NODES=TWO, FAKE_TEMP="92", FAKE_RUN="30"))
    assert res.returncode == 3, res.stdout + res.stderr
    assert res.stdout.split("CLUSTER SUMMARY")[1].count("OVERHEATED") == 2
    assert sorted(used_nodes(tmp_path)) == ["w1", "w2"]


def test_parallel_with_a_single_worker_falls_back_to_sequential(tmp_path):
    nodes = '[{"name":"w1"},{"name":"m1","master":true}]'
    res = run_series(tmp_path, "--cluster", "--parallel", "--yes", "--time", "5",
                     env_extra=par_env(FAKE_NODES=nodes, FAKE_RUN="2"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Running in parallel makes sense from two workers" in res.stdout and "AT ONCE" not in res.stdout
    assert used_nodes(tmp_path) == ["w1", "m1"]


def test_no_parallel_flag_keeps_workers_sequential(tmp_path):
    res = run_series(tmp_path, "--workers", "--no-parallel", "--yes", "--time", "5",
                     env_extra=par_env(FAKE_NODES=TWO, FAKE_RUN="3"))
    assert res.returncode == 0, res.stdout + res.stderr
    starts, ends = event_times(tmp_path, "start"), event_times(tmp_path, "end")
    assert ends["w1"] <= starts["w2"]                                     # the second one started only after the first
    assert "AT ONCE" not in res.stdout and "Concurrency: yes" not in node_logs(tmp_path)[0].read_text(encoding="utf-8")


def test_interactive_question_offers_parallel_and_yes_runs_workers_together(tmp_path):
    # max. temperature, background, note, AT ONCE?, start?
    res = run_series(tmp_path, "--workers", "--profile", "stepped", "--step-time", "30s",
                     "--steps", "25,100", env_extra=par_env(FAKE_NODES=TWO, FAKE_RUN="4"),
                     stdin="\nn\n\ny\ny\n", interactive=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Test the workers AT ONCE?" in res.stdout and "2 workers AT ONCE" in res.stdout
    starts, ends = event_times(tmp_path, "start"), event_times(tmp_path, "end")
    assert max(starts.values()) < min(ends.values())


def test_api_limit_must_be_positive(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--api-limit", "0",
                     env_extra={"FAKE_NODES": TWO})
    assert res.returncode == 1 and "--api-limit must be greater than 0" in res.stdout
    assert used_nodes(tmp_path) == []


def test_parallel_series_in_background_can_be_stopped_and_children_end(tmp_path):
    try:
        res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--background", "--time", "60",
                         env_extra=par_env(FAKE_NODES=TWO, FAKE_RUN="60"))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "cluster (2 nodes: w1, w2)" in res.stdout
        assert wait_for(lambda: (tmp_path / "events.txt").exists()
                        and len(event_times(tmp_path, "start")) == 2)
        assert len(children_alive(tmp_path)) >= 2
        stop = run_series(tmp_path, "--stop", "cluster")
        assert stop.returncode == 0, stop.stdout + stop.stderr
        assert wait_for(lambda: not list((tmp_path / "running").glob("*.json")))
        console = next((tmp_path / "logs").glob("cluster-*.console.txt")).read_text(encoding="utf-8")
        assert "Series interrupted" in console and "CLUSTER SUMMARY" in console
        assert wait_for(lambda: children_alive(tmp_path) == [], timeout=30)
        assert len(list(tmp_path.glob("deleted-stress-test-*"))) == 2       # pods of both nodes deleted
    finally:
        kill_leftovers(tmp_path)
        subprocess.run(["pkill", "-f", "--", f"--log-file {tmp_path}"])
