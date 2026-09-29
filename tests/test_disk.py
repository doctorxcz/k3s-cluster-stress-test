"""Disk benchmark (fio): parsing, command, config, menu, run against the fake kubectl, baseline."""
import json

import pytest

from stress_test import baseline, cli, series
from stress_test.disk import (JOBS, DiskResult, build_disk_command, jobs, parse_fio_json,
                              parse_result_line)
from stress_test.logparse import parse_log
from stress_test.manifests import build_stress_command, stress_pod
from stress_test.models import PROFILE_DISK, PodNames, StressConfig
from stress_test.parallel import child_args
from test_integration import run_tool

FIO = {"jobs": [{"read": {"io_bytes": 0}, "write": {"io_bytes": 5_000_000, "bw_bytes": 250_000_000, "iops": 61.0,
                                                    "lat_ns": {"mean": 4_000_000.0},
                                                    "clat_ns": {"percentile": {"99.000000": 9_000_000}}}}]}


def _cfg(**kw):
    base = dict(node="n", profile=PROFILE_DISK, disk_size=512, disk_job_time=10, duration=40)
    base.update(kw)
    return StressConfig(**base)


def test_parse_fio_json_picks_the_active_side():
    r = parse_fio_json("seq-write", json.dumps(FIO))
    assert r.mb_s == pytest.approx(250.0) and r.iops == 61.0
    assert r.lat_avg_ms == pytest.approx(4.0) and r.lat_p99_ms == pytest.approx(9.0)
    assert parse_fio_json("x", "garbage") is None and parse_fio_json("x", '{"jobs": []}') is None


def test_parse_fio_json_old_bw_field_in_kib():
    data = {"jobs": [{"read": {"io_bytes": 9, "bw": 1000, "iops": 5}, "write": {"io_bytes": 0}}]}
    assert parse_fio_json("r", json.dumps(data)).mb_s == pytest.approx(1.024)


def test_result_log_line_roundtrip():
    r = DiskResult("rand-read", 41.5, 10600.0, 0.3, 1.25)
    back = parse_result_line(r.log_line())
    assert (back.name, back.mb_s, back.iops, back.lat_avg_ms, back.lat_p99_ms) == ("rand-read", 41.5, 10600.0, 0.3, 1.25)
    assert parse_result_line(DiskResult("x", 1.0, 2.0).log_line()).lat_p99_ms is None
    assert parse_result_line("[10:00:00] CPU: 1%") is None


def test_jobs_and_command():
    assert len(jobs()) == 4 and [j[0] for j in jobs(True)] == ["seq-read", "rand-read"]
    cmd = build_disk_command(512, 10)
    assert "--size=512M" in cmd and "--runtime=10" in cmd and "--direct=1" in cmd and "--direct=0" in cmd
    assert "seq-write" in cmd and "DISK-DONE" in cmd and "DISK-FAILED" in cmd
    ro = build_disk_command(256, 10, read_only=True)
    assert "write" not in ro.replace("DISK-", "")            # no write job at all
    assert build_stress_command(_cfg(), None) == build_disk_command(512, 10, False)


def test_stress_pod_installs_fio_with_scratch_volume():
    pod = stress_pod("n", PodNames.new(), 900, "echo hi", package="fio", scratch_mib=768)
    c = pod["spec"]["containers"][0]
    assert "install -y -qq fio" in c["command"][-1] and c["volumeMounts"][0]["mountPath"] == "/bench"
    assert pod["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] == "768Mi"
    plain = stress_pod("n", PodNames.new(), 900, "echo hi")
    assert "stress-ng" in plain["spec"]["containers"][0]["command"][-1] and not plain["spec"]["volumes"]


def test_config_validation_and_master_limits():
    _cfg().validate()
    for kw in ({"disk_size": 50}, {"disk_job_time": 2}, {"duration": 41}, {"ram_pct": 10}, {"hdd": True}):
        with pytest.raises(ValueError):
            _cfg(**kw).validate()
    cfg = _cfg(disk_size=1024)
    msgs = cfg.apply_master_limits()
    assert cfg.disk_read_only and cfg.disk_size == 256 and cfg.duration == 20 and any("disk benchmark" in m for m in msgs)
    cfg.validate()


def _args(*extra):
    return cli.build_parser().parse_args(["--node", "n", "--profile", "disk", "--max-temp", "85", "--cooldown", "60",
                                          "--no-background", "--notes", "", "--non-interactive", *extra])


def test_build_config_disk():
    cfg = cli.build_config(_args("--disk-size", "300", "--disk-job-time", "8"), "n", master_mode=False)
    assert cfg.disk and cfg.disk_size == 300 and cfg.disk_job_time == 8 and cfg.duration == 32 and cfg.log
    cfg.validate()
    default = cli.build_config(_args(), "n", master_mode=False)
    assert default.disk_size == 1024 and default.duration == 60


def test_ask_profile_disk(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": "4")
    assert cli.ask_profile() == PROFILE_DISK


def test_child_args_disk_roundtrip():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    a = child_args(_cfg(), opts, "/l/w.log", 1)
    child = cli.build_config(cli.build_parser().parse_args(a), "w", master_mode=False)
    assert (child.disk, child.disk_size, child.disk_job_time, child.duration) == (True, 512, 10, 40)


def test_parse_log_reads_disk_profile_and_results():
    text = ("=== KUBERNETES STRESS-NG LOG ===\nNode: n1\nStarted: 2026-09-29 10:00:00\n"
            "Profile: disk - fio benchmark, 4 jobs of 15 s, 1024 MiB file\n"
            + DiskResult("seq-read", 500.0, 500.0, 1.0, 2.0).log_line() + "\n"
            + DiskResult("seq-write", 250.0, 250.0, 2.0, 4.0).log_line() + "\n")
    run = parse_log(text)
    assert run.profile == "disk" and [r.name for r in run.disk_results] == ["seq-read", "seq-write"]


def test_run_disk_benchmark_against_fake(tmp_path):
    res = run_tool(tmp_path, "--profile", "disk", "--disk-size", "128", "--disk-job-time", "5",
                   env_extra={"FAKE_RUN": "4", "FAKE_DISK_MBS": "300"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DISK BENCHMARK" in res.stdout and "seq-write" in res.stdout and "300.0 MB/s" in res.stdout
    assert "Test completed" in res.stdout
    log = next((tmp_path / "logs").glob("fake-node-20s-*.log")).read_text(encoding="utf-8")
    assert log.count("Disk result:") == 4 and "Profile: disk" in log
    data = json.loads(next((tmp_path / "logs").glob("*.json")).read_text())
    assert len(data["disk_results"]) == 4 and data["profile"] == "disk"


def test_run_disk_failure_is_reported(tmp_path):
    res = run_tool(tmp_path, "--profile", "disk", "--disk-size", "128", "--disk-job-time", "5",
                   env_extra={"FAKE_RUN": "4", "FAKE_DISK_FAIL": "1"})
    assert res.returncode != 0 and "failed" in res.stdout
    assert "Test completed" not in res.stdout


def test_baseline_flags_slower_disk(tmp_path):
    def run(mbs, name):
        text = ("=== KUBERNETES STRESS-NG LOG ===\nNode: n1\nStarted: 2026-09-29 10:00:00\nProfile: disk - fio\n"
                "[10:00:00] CPU: 5%, RAM: 100 MiB (1%) | Temp: CPU: 50°C, GPU: 50°C | Clock: 3000 MHz\n"
                + DiskResult("seq-read", mbs, 100.0, 1.0, 2.0).log_line() + "\n")
        return parse_log(text, name)
    baseline.save_baseline(run(500.0, "a.log"), tmp_path)
    assert baseline.check_run(run(480.0, "b.log"), tmp_path)["status"] == "ok"
    res = baseline.check_run(run(300.0, "c.log"), tmp_path)
    assert res["status"] == "regression" and any("seq-read throughput" in w for w in res["warnings"])
