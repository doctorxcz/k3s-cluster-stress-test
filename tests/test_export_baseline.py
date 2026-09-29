"""Fáze 1: JSON/CSV export and baseline check (unit + against the fake kubectl)."""
import csv
import json

import pytest

from stress_test import baseline
from stress_test.export import export_paths, run_to_dict, write_exports
from stress_test.logparse import parse_log
from stress_test.models import StressConfig
from stress_test.parallel import child_args
from stress_test import series
from test_compare import make_log
from test_integration import run_tool


def _run(temps=(50, 55, 60), ops=(1000.0, 5000.0), name="n1.log", **kw):
    run = parse_log(make_log(temps, (55, 50), node="n1", ops=ops, **kw), name)
    return run


# ---------------- export ---------------------------------------------------------------------------------

def test_run_to_dict_contains_stats_and_samples():
    data = run_to_dict(_run())
    assert data["schema_version"] == 1 and data["node"] == "n1" and data["source_log"] == "n1.log"
    assert data["stats"]["temp_max"] == 60 and len(data["samples"]) == 5
    json.dumps(data)                                    # serializable


def test_export_paths_by_format():
    assert list(export_paths("/x/a.log", "json")) == ["json"]
    assert list(export_paths("/x/a.log", "both")) == ["json", "csv"]
    assert export_paths("/x/a.log", "none") == {}


def test_write_exports_json_and_csv(tmp_path):
    log = tmp_path / "a.log"
    written = write_exports(_run(), str(log), "both", extra={"x": 1})
    assert {p.suffix for p in written} == {".json", ".csv"}
    assert json.loads((tmp_path / "a.json").read_text())["x"] == 1
    rows = list(csv.reader((tmp_path / "a.csv").open()))
    assert rows[0][:3] == ["t", "phase", "stage"] and len(rows) == 6
    assert write_exports(_run(), str(log), "none") == []


# ---------------- baseline -------------------------------------------------------------------------------

def test_baseline_save_load_and_ok_check(tmp_path):
    baseline.save_baseline(_run(), tmp_path)
    assert baseline.load_baseline(tmp_path, "n1")["stats"]["temp_max"] == 60
    res = baseline.check_run(_run(name="new.log"), tmp_path)
    assert res["status"] == "ok"
    assert "Verdict: OK" in "\n".join(baseline.baseline_report(res))


def test_baseline_detects_regression(tmp_path):
    baseline.save_baseline(_run(), tmp_path)
    res = baseline.check_run(_run(temps=(60, 66, 70), ops=(800.0, 4000.0), freq=3000, name="new.log"), tmp_path)
    assert res["status"] == "regression"
    text = " ".join(res["warnings"])
    assert "hotter" in text and "clock lower" in text and "performance lower" in text


def test_baseline_skips_itself_and_missing(tmp_path):
    assert baseline.check_run(_run(), tmp_path) is None            # no baseline saved
    baseline.save_baseline(_run(), tmp_path)
    assert baseline.check_run(_run(), tmp_path) is None            # the same log


def test_baseline_incomparable_profile(tmp_path):
    baseline.save_baseline(_run(), tmp_path)
    other = _run(name="new.log")
    other.profile = "spike"
    assert baseline.check_run(other, tmp_path)["status"] == "incomparable"


def test_baseline_rejects_empty_log(tmp_path):
    empty = parse_log("=== KUBERNETES STRESS-NG LOG ===\nNode: n1\nStarted: 2026-09-29 10:00:00\n", "e.log")
    with pytest.raises(ValueError):
        baseline.save_baseline(empty, tmp_path)


def test_load_baseline_ignores_broken_file(tmp_path):
    (tmp_path / "baselines").mkdir()
    (tmp_path / "baselines" / "n1.json").write_text("{not json")
    assert baseline.load_baseline(tmp_path, "n1") is None


# ---------------- config + subprocess -------------------------------------------------------------------

def test_config_export_validation():
    StressConfig(node="n", export="both").validate()
    with pytest.raises(ValueError):
        StressConfig(node="n", export="xml").validate()


def test_child_args_pass_export_and_baseline_flag():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    a = child_args(StressConfig(node="w", export="csv", baseline_check=False), opts, "/l/w.log", 1)
    assert a[a.index("--export") + 1] == "csv" and "--no-baseline-check" in a
    b = child_args(StressConfig(node="w"), opts, "/l/w.log", 1)
    assert "--no-baseline-check" not in b


# ---------------- CLI against the fake kubectl ----------------------------------------------------------

def test_run_writes_json_next_to_log(tmp_path):
    res = run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    jsons = list((tmp_path / "logs").glob("fake-node-5s-*.json"))
    assert len(jsons) == 1 and json.loads(jsons[0].read_text())["node"] == "fake-node"
    assert "Export saved to" in res.stdout


def test_export_none_writes_nothing(tmp_path):
    res = run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", "--export", "none",
                   env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not list((tmp_path / "logs").glob("*.json"))


def test_set_baseline_then_next_run_is_checked(tmp_path):
    first = run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert first.returncode == 0, first.stdout + first.stderr
    assert "BASELINE CHECK" not in first.stdout
    setb = run_tool(tmp_path, "--set-baseline", "fake-node")
    assert setb.returncode == 0 and "Baseline of fake-node set" in setb.stdout
    assert (tmp_path / "logs" / "baselines" / "fake-node.json").is_file()
    import time
    time.sleep(1.1)                                      # a different log-name timestamp
    second = run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert second.returncode == 0, second.stdout + second.stderr
    assert "BASELINE CHECK" in second.stdout
    third = run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", "--no-baseline-check",
                     env_extra={"FAKE_RUN": "3"})
    assert "BASELINE CHECK" not in third.stdout


def test_export_log_command(tmp_path):
    run_tool(tmp_path, "--node", "fake-node", "--time", "5", "--log", "--export", "none", env_extra={"FAKE_RUN": "3"})
    res = run_tool(tmp_path, "--export-log", "fake-node")
    assert res.returncode == 0, res.stdout + res.stderr
    assert list((tmp_path / "logs").glob("*.csv")) and list((tmp_path / "logs").glob("*.json"))
    bad = run_tool(tmp_path, "--export-log", "nonexistent-node")
    assert bad.returncode != 0
