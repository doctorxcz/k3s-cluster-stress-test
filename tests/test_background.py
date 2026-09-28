"""Unit tests of the registry of background tests (without fork)."""
import json
import os
import time

from stress_test import background


def _write(dirpath, run_id, pid, node="node", duration=3600, started=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "pid": pid, "node": node, "duration": duration,
        "started": started if started is not None else time.time(),
        "log": "/x/a.log", "console": "/x/a.console.txt"}))


def test_list_running_keeps_live_and_drops_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path))
    _write(tmp_path, "live1", os.getpid())                 # this process is alive
    _write(tmp_path, "dead1", 2 ** 22 + 12345)             # such a PID does not exist
    running = background.list_running()
    assert [r["run_id"] for r in running] == ["live1"]
    assert not (tmp_path / "dead1.json").exists()          # the stale record was cleaned up


def test_list_running_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "does not exist"))
    assert background.list_running() == []


def test_format_running():
    assert background.format_running([]) == "No background test is running."
    now = 10_000.0
    text = background.format_running(
        [{"run_id": "ab12cd", "pid": 42, "node": "dell", "duration": 3600,
          "started": now - 125, "log": "/l/a.log", "console": "/l/a.console.txt"}], now=now)
    assert "ab12cd" in text and "node dell" in text and "PID 42" in text
    assert "running 2 min 5 s of 1 h" in text
    assert "/l/a.log" in text and "/l/a.console.txt" in text


def test_stop_no_match_and_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path))
    ok, msg = background.stop("nothing")
    assert not ok and "matches" in msg
    _write(tmp_path, "a1", os.getpid(), node="dell")
    _write(tmp_path, "b2", os.getpid(), node="dell")
    ok, msg = background.stop("dell")
    assert not ok and "more than one test" in msg and "a1" in msg and "b2" in msg


def test_register_and_unregister_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path))
    background.register("zz9", "g2", 60, "/x.log", "/x.console.txt")
    data = json.loads((tmp_path / "zz9.json").read_text())
    assert data["pid"] == os.getpid() and data["node"] == "g2" and data["duration"] == 60
    background.unregister("zz9")
    assert not (tmp_path / "zz9.json").exists()
    background.unregister("zz9")                            # must not crash the second time
