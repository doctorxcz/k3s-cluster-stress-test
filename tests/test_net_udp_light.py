"""no-udp extra, soft UDP failure (NET-SKIPPED), light summary and cooldown default 0 for net/disk profiles."""
import json
import os
import subprocess

import pytest

from stress_test import cli
from stress_test.net import (EXTRAS, build_net_command, job_names, net_duration, normalize_extras)
from test_integration import run_tool

NODES = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "peer-node", "ip": "10.0.0.2"}])


def test_no_udp_is_a_known_extra_but_not_part_of_all():
    assert "no-udp" in EXTRAS
    assert normalize_extras(["no-udp"]) == ("no-udp",)
    assert "no-udp" not in normalize_extras(["all"])


@pytest.mark.parametrize("mode", ["host", "pod"])
def test_job_names_without_udp(mode):
    assert "udp" in job_names(mode, ())
    names = job_names(mode, ["no-udp"])
    assert "udp" not in names and "no-udp" not in names and "tcp-x4" in names


def test_net_duration_shorter_by_one_test():
    assert net_duration(10, ()) - net_duration(10, ["no-udp"]) == 10
    assert net_duration(10, ["no-udp", "dns"]) == net_duration(10, ["dns"]) - 10


def test_build_net_command_no_udp_and_job_count():
    with_udp = build_net_command("10.0.0.2", 30001, "pod", 5)
    without = build_net_command("10.0.0.2", 30001, "pod", 5, extras=["no-udp"])
    assert "-u -b" in with_udp and "-u -b" not in without
    n_with, n_without = len(job_names("pod")), len(job_names("pod", ["no-udp"]))
    assert n_without == n_with - 1
    assert f"NET-JOB $i/{n_with} ping" in with_udp and f"NET-JOB $i/{n_without} ping" in without


def test_udp_job_is_soft_and_others_are_hard():
    script = build_net_command("10.0.0.2", 30001, "pod", 5)
    assert script.count("NET-SKIPPED udp") == 1 and "NET-FAILED udp" not in script
    for hard in ("tcp-up", "tcp-down", "tcp-x4"):
        assert f"NET-FAILED {hard}" in script and f"NET-SKIPPED {hard}" not in script
    assert "timeout 15 iperf3 -c 10.0.0.2 -p 30001 -t 5 -J -u" in script       # soft job wrapped in timeout
    assert "timeout 15 iperf3 -c 10.0.0.2 -p 30001 -t 5 -J -R" not in script


def _run_script_with_stubs(tmp_path, udp_fails):
    """Executes the generated bash script (pod mode) with stub iperf3 / ping."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "iperf3").write_text(
        "#!/bin/sh\n" + ('case "$*" in *" -u "*) echo "iperf3: error - control socket has closed unexpectedly"; exit 1;; esac\n'
                         if udp_fails else "") +
        'echo \'{"end":{"sum_received":{"bits_per_second":9.4e8},"sum":{"lost_percent":0}}}\'\n')
    (bin_dir / "ping").write_text("#!/bin/sh\necho '5 packets transmitted, 5 received, 0% packet loss'\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    script = build_net_command("10.0.0.2", 30001, "pod", 1)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")).stdout


def test_script_udp_failure_is_skipped_and_test_goes_on(tmp_path):
    out = _run_script_with_stubs(tmp_path, udp_fails=True)
    assert "NET-SKIPPED udp" in out and "control socket has closed" in out
    assert "NET-FAILED" not in out and out.strip().endswith("NET-DONE")
    assert "NET-RESULT tcp-x4" in out


def test_script_udp_ok_gives_result(tmp_path):
    out = _run_script_with_stubs(tmp_path, udp_fails=False)
    assert "NET-RESULT udp" in out and "NET-SKIPPED" not in out and out.strip().endswith("NET-DONE")


# ---------------- cooldown default and the light summary ---------------------------------------------------

def _cfg(*extra):
    args = cli.build_parser().parse_args(["--node", "n", "--non-interactive", "--no-background", "--notes", "", *extra])
    return cli.build_config(args, "n", master_mode=False)


@pytest.mark.parametrize("profile", ["net", "disk"])
def test_net_and_disk_profiles_default_to_no_cooldown(profile):
    assert _cfg("--profile", profile).cooldown == 0
    assert _cfg("--profile", profile, "--cooldown", "30").cooldown == 30


def test_cpu_profile_keeps_default_cooldown():
    assert _cfg("--profile", "classic").cooldown > 0


def _run(tmp_path, *extra):
    return run_tool(tmp_path, *extra, env_extra={"FAKE_NODES": NODES, "FAKE_RUN": "5"})


def test_net_run_prints_light_summary(tmp_path):
    res = _run(tmp_path, "--profile", "net", "--net-time", "5")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "NETWORK TEST SUMMARY" in res.stdout and "DISK BENCHMARK SUMMARY" not in res.stdout
    assert "Node CPU load (info)" in res.stdout
    assert "Test completed" in res.stdout


def test_net_run_no_udp_has_no_udp_result(tmp_path):
    res = _run(tmp_path, "--profile", "net", "--net-time", "5", "--net-extra", "no-udp")
    assert res.returncode == 0, res.stdout + res.stderr
    log = next((tmp_path / "logs").rglob("fake-node-*.log")).read_text(encoding="utf-8")
    assert "Net result: tcp-x4" in log and "Net result: udp" not in log


def test_disk_run_prints_light_summary(tmp_path):
    res = _run(tmp_path, "--profile", "disk", "--disk-size", "128", "--disk-job-time", "5")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DISK BENCHMARK SUMMARY" in res.stdout and "NETWORK TEST SUMMARY" not in res.stdout
