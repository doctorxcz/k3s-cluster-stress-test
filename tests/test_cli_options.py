"""--dry-run, --list-nodes, --schedule: units (parsing) + integration against fake_kubectl."""
import os
import subprocess
import sys
import time

import pytest

from stress_test import cli
from stress_test.cli import schedule_arg
from test_integration import FAKE, ROOT, run_tool


# --- schedule_arg (units) -----------------------------------------------------------
def test_schedule_arg_relative_duration():
    now = time.time()
    target = schedule_arg("30m")
    assert 1790 < target - now < 1810


def test_schedule_arg_clock_today_or_tomorrow():
    t = time.localtime(time.time() + 3600)
    hh, mm = t.tm_hour, t.tm_min
    target = schedule_arg(f"{hh:02d}:{mm:02d}")
    assert target > time.time()
    assert time.strftime("%H:%M", time.localtime(target)) == f"{hh:02d}:{mm:02d}"


def test_schedule_arg_invalid():
    with pytest.raises(Exception):
        schedule_arg("25:99")


# --- --dry-run (integration) --------------------------------------------------------------
def test_dry_run_does_not_start_pod(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--dry-run", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Dry run" in res.stdout
    assert not list((tmp_path / "logs").glob("*.log"))


# --- --list-nodes (integration) -----------------------------------------------------------
def test_list_nodes_prints_roles(tmp_path):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path),
              PYTHONPATH=str(ROOT),
              FAKE_NODES='[{"name": "w1"}, {"name": "m1", "master": true}]')
    cmd = [sys.executable, "-m", "stress_test", "--list-nodes"]
    res = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "w1" in res.stdout and "worker" in res.stdout
    assert "m1" in res.stdout and "master" in res.stdout


# --- --schedule (integration, short duration) -------------------------------------------------
def test_schedule_waits_before_running(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--schedule", "1s", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Scheduled for" in res.stdout
    assert "Test completed" in res.stdout
