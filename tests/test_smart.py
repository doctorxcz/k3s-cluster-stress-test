"""SMART disk health preflight (--smart)."""
import json

import pytest

from stress_test.manifests import SMART_SCRIPT, smart_pod
from stress_test.models import PodNames, StressConfig
from stress_test.parallel import child_args
from stress_test import series
from stress_test.smart import parse_disk, parse_smart_output
from test_integration import run_tool

ATA = {"model_name": "Samsung SSD", "smart_status": {"passed": True}, "temperature": {"current": 38},
       "power_on_time": {"hours": 5000},
       "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}, {"id": 197, "raw": {"value": 0}},
                                          {"id": 198, "raw": {"value": 0}}]}}
NVME = {"model_name": "WD Blue NVMe", "smart_status": {"passed": True}, "temperature": {"current": 45},
        "nvme_smart_health_information_log": {"percentage_used": 3, "media_errors": 0, "critical_warning": 0}}


def test_parse_healthy_ata_and_nvme():
    a, n = parse_disk("/dev/sda", json.dumps(ATA)), parse_disk("/dev/nvme0", json.dumps(NVME))
    assert a.passed and not a.fatal and not a.problems and a.reallocated == 0 and a.power_on_hours == 5000
    assert n.percentage_used == 3 and not n.fatal and "wear 3 %" in n.line()


def test_parse_failed_and_warnings():
    bad = dict(ATA, smart_status={"passed": False})
    assert parse_disk("/dev/sda", json.dumps(bad)).fatal
    worn = dict(NVME, nvme_smart_health_information_log={"percentage_used": 95, "media_errors": 2,
                                                          "critical_warning": 0})
    d = parse_disk("/dev/nvme0", json.dumps(worn))
    assert not d.fatal and len(d.problems) == 2
    crit = dict(NVME, nvme_smart_health_information_log={"percentage_used": 1, "critical_warning": 4})
    assert parse_disk("/dev/nvme0", json.dumps(crit)).fatal
    re = json.loads(json.dumps(ATA))
    re["ata_smart_attributes"]["table"][0]["raw"]["value"] = 8
    assert "8 reallocated" in parse_disk("/dev/sda", json.dumps(re)).problems[0]


def test_parse_garbage_does_not_raise():
    d = parse_disk("/dev/sda", "not json")
    assert d.error and d.passed is None and not d.fatal


def test_parse_smart_output_markers_and_notes():
    text = f"SMART-BEGIN /dev/sda\n{json.dumps(ATA, indent=1)}\nSMART-END\nSMART-BEGIN /dev/nvme0\n{json.dumps(NVME)}\nSMART-END\n"
    disks, note = parse_smart_output(text)
    assert [d.dev for d in disks] == ["/dev/sda", "/dev/nvme0"] and note == ""
    assert parse_smart_output("SMART-UNAVAILABLE apt")[1].startswith("smartmontools could not")
    assert parse_smart_output("")[1].startswith("no disks")


def test_smart_pod_is_privileged_and_uses_hw_name():
    names = PodNames.new()
    pod = smart_pod("n1", names)
    assert pod["metadata"]["name"] == names.hw and pod["spec"]["containers"][0]["securityContext"] == {"privileged": True}
    assert "smartctl -j" in SMART_SCRIPT


def test_child_args_smart_flags():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    a = child_args(StressConfig(node="w", smart=True, allow_bad_disk=True), opts, "/l/w.log", 1)
    assert "--smart" in a and "--allow-bad-disk" in a
    b = child_args(StressConfig(node="w"), opts, "/l/w.log", 1)
    assert "--smart" not in b and "--allow-bad-disk" not in b


def test_smart_off_by_default_no_pod_privileged(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0 and "SMART" not in res.stdout


def test_smart_ok_is_printed_and_logged(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--smart", "--log", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Disk health OK (SMART)" in res.stdout and "Fake SSD 256GB" in res.stdout
    log = next((tmp_path / "logs").glob("fake-node-5s-*.log")).read_text(encoding="utf-8")
    assert "=== DISK HEALTH (SMART) ===" in log and "/dev/sda: Fake SSD 256GB | PASSED" in log
    data = json.loads(next((tmp_path / "logs").glob("*.json")).read_text())
    assert any("Fake SSD" in line for line in data["disk_health"])


def test_smart_failed_disk_refuses_test(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--smart", env_extra={"FAKE_RUN": "3", "FAKE_SMART_FAIL": "1"})
    assert res.returncode != 0 and "SMART health check FAILED" in res.stdout
    assert "Test completed" not in res.stdout


def test_smart_failed_disk_allowed_with_flag(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--smart", "--allow-bad-disk",
                   env_extra={"FAKE_RUN": "3", "FAKE_SMART_FAIL": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--allow-bad-disk" in res.stdout


def test_smart_warning_does_not_stop(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--smart", env_extra={"FAKE_RUN": "3", "FAKE_SMART_REALLOC": "12"})
    assert res.returncode == 0 and "12 reallocated sectors" in res.stdout


def test_smart_unavailable_continues(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--smart", env_extra={"FAKE_RUN": "3", "FAKE_SMART_RAW": "SMART-UNAVAILABLE apt"})
    assert res.returncode == 0 and "could not be installed" in res.stdout
