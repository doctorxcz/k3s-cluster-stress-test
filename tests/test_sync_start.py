"""Synchronised start: the gate in the stress pod, the fake kubectl gate flow and the parallel barrier."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from stress_test import cli, series
from stress_test.manifests import FAILED_MARKER, GATE_FILE, READY_MARKER, STARTED_MARKER, stress_pod
from stress_test.models import PodNames, StressConfig
from stress_test import parallel
from stress_test.parallel import LiveInfo, ParallelSeriesRunner, child_args, read_started
from test_integration import FAKE, run_series

THREE_W = '[{"name":"w1"},{"name":"w2"},{"name":"w3"}]'
PAR_ENV = {"STRESS_TEST_TABLE_INTERVAL": "1", "STRESS_TEST_API_INTERVAL": "1", "FAKE_NODES": THREE_W, "FAKE_RUN": "6"}


def _script(gate_seconds):
    pod = stress_pod("n", PodNames.new("abc123"), 600, "echo LOAD", gate_seconds=gate_seconds)
    return pod["spec"]["containers"][0]["command"][2]


def test_stress_pod_without_gate_has_no_ready_marker():
    script = _script(0)
    assert READY_MARKER not in script and GATE_FILE not in script
    assert STARTED_MARKER in script and "echo LOAD" in script


def test_stress_pod_gate_script_order():
    script = _script(300)
    assert script.index(READY_MARKER) < script.index(GATE_FILE) < script.index(STARTED_MARKER) < script.index("echo LOAD")
    assert "$SECONDS -lt 300" in script and FAILED_MARKER in script


def _run_gate_script(tmp_path, gate_seconds, create_go):
    """Runs the real gate script with a fake apt-get; /tmp/go is replaced by a private path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    apt = bin_dir / "apt-get"
    apt.write_text("#!/bin/sh\nexit 0\n")
    apt.chmod(0o755)
    go = tmp_path / "go"
    script = _script(gate_seconds).replace(GATE_FILE, str(go))
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"))
    if create_go:
        assert proc.stdout.readline().strip() == READY_MARKER       # waits here until the gate opens
        go.write_text("")
        out = proc.communicate(timeout=20)[0]
        return READY_MARKER + "\n" + out
    return proc.communicate(timeout=20)[0]


def test_gate_script_waits_for_go_file(tmp_path):
    text = _run_gate_script(tmp_path, 30, create_go=True)
    assert text.index(READY_MARKER) < text.index(STARTED_MARKER) < text.index("LOAD")


def test_gate_script_fails_when_go_never_comes(tmp_path):
    out = _run_gate_script(tmp_path, 1, create_go=False)
    assert READY_MARKER in out and FAILED_MARKER in out and STARTED_MARKER not in out


def test_fake_kubectl_gate_flow(tmp_path):
    """apply a gated pod -> logs show READY only; `exec touch /tmp/go` -> logs show STARTED."""
    env = dict(os.environ, FAKE_STATE=str(tmp_path))
    names = PodNames.new("a1b2c3")
    manifest = stress_pod("fake-node", names, 600, "stress-ng --cpu 1", gate_seconds=120)

    def kubectl(*args, input=None):
        return subprocess.run([str(FAKE), *args], env=env, input=input, capture_output=True, text=True, timeout=20)
    assert kubectl("apply", "-f", "-", input=json.dumps(manifest)).returncode == 0
    before = kubectl("logs", names.stress).stdout
    assert READY_MARKER in before and STARTED_MARKER not in before
    kubectl("exec", names.stress, "--", "sh", "-c", f"touch {GATE_FILE}")
    after = kubectl("logs", names.stress).stdout
    assert READY_MARKER in after and STARTED_MARKER in after


def test_child_args_gate_only_when_given():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    cfg = StressConfig(node="w", duration=10)
    plain = child_args(cfg, opts, "/l/w.log", 2)
    assert "--start-gate" not in plain
    gated = child_args(cfg, opts, "/l/w.log", 2, gate="/l/w.log.gate", ready_timeout=300)
    assert gated[gated.index("--start-gate") + 1] == "/l/w.log.gate"
    assert gated[gated.index("--ready-timeout") + 1] == "300"
    args = cli.build_parser().parse_args([*gated])
    assert args.start_gate == "/l/w.log.gate" and args.ready_timeout == 300


def test_cli_flags_defaults():
    args = cli.build_parser().parse_args([])
    assert args.no_sync_start is False and args.start_gate == "" and args.ready_timeout >= 30
    assert cli.build_parser().parse_args(["--no-sync-start"]).no_sync_start is True


def test_ready_timeout_too_small_is_rejected(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--time", "5", "--ready-timeout", "5",
                     env_extra=PAR_ENV)
    assert res.returncode != 0
    assert "--ready-timeout must be at least 30" in res.stdout + res.stderr


def test_parallel_barrier_ready_lines_then_go(tmp_path):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--time", "5", "--start-mode", "sync",
                     env_extra=PAR_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    for node in ("w1", "w2", "w3"):
        assert f"{node} READY (" in out
    assert "/3)" in out and "GO - load started on 3 nodes at the same moment" in out
    assert out.index("(3/3)") < out.index("GO - load started")
    (cluster,) = (tmp_path / "logs").rglob("cluster-*.log")
    assert "GO for 3 nodes" in cluster.read_text(encoding="utf-8")
    assert not list((tmp_path / "logs").rglob("*.gate.*"))              # barrier files are cleaned up
    # the load really began together
    starts = [float(l.split()[0]) for l in (tmp_path / "events.txt").read_text().splitlines() if " start " in l]
    assert len(starts) == 3 and max(starts) - min(starts) < 3


@pytest.mark.parametrize("extra", [[], ["--start-mode", "rolling"], ["--no-sync-start"]])
def test_parallel_rolling_is_the_default(tmp_path, extra):
    res = run_series(tmp_path, "--workers", "--parallel", "--yes", "--time", "5", *extra, env_extra=PAR_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "READY (" not in out and "GO - load started" not in out
    for node in ("w1", "w2", "w3"):
        assert f"{node} started its load (" in out
    assert "(3/3)" in out and "CLUSTER SUMMARY" in out
    assert not list((tmp_path / "logs").rglob("*.gate.*"))            # status files of the parent are cleaned up
    starts = [l for l in (tmp_path / "events.txt").read_text().splitlines() if " start " in l]
    assert len(starts) == 3


def test_start_mode_flags():
    parser = cli.build_parser()
    assert parser.parse_args([]).start_mode == "rolling"
    assert parser.parse_args(["--start-mode", "sync"]).start_mode == "sync"
    assert parser.parse_args(["--no-sync-start"]).no_sync_start is True
    assert parser.parse_args(["--no-gate-wait"]).no_gate_wait is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--start-mode", "nope"])


def test_child_args_rolling_adds_no_gate_wait():
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    cfg = StressConfig(node="w", duration=10)
    rolling = child_args(cfg, opts, "/l/w.log", 2, gate="/l/w.log.gate", ready_timeout=300, rolling=True)
    assert "--no-gate-wait" in rolling and rolling[rolling.index("--start-gate") + 1] == "/l/w.log.gate"
    sync = child_args(cfg, opts, "/l/w.log", 2, gate="/l/w.log.gate", ready_timeout=300)
    assert "--no-gate-wait" not in sync
    assert "--no-gate-wait" not in child_args(cfg, opts, "/l/w.log", 2)
    assert cli.build_parser().parse_args(rolling).no_gate_wait is True


# ---------------- rolling start: every node keeps its own time -----------------------------------------------

def test_read_started(tmp_path):
    log = str(tmp_path / "w.log")
    assert read_started(log) is None
    Path(log + ".gate.started").write_text("1700000000.5\n")
    assert read_started(log) == 1700000000.5
    Path(log + ".gate.started").write_text("garbage")
    assert read_started(log) is None
    Path(log + ".gate.started").write_text("")
    assert read_started(log) is None


def bar(elapsed, *, code=None, info=None, cfg=None, waiting=False, ready=False, waited=0.0, prep=None):
    runner = object.__new__(ParallelSeriesRunner)
    runner.ready_timeout = 300
    cfg = cfg or StressConfig(node="w", duration=100, cooldown=20)
    return runner._node_bar(cfg, info or LiveInfo(), code, elapsed, waiting, ready, waited, prep)


def test_node_bar_while_preparing():
    assert bar(None) == ("wait", 0.0, "")
    assert bar(None, prep=(0.4, "installing")) == ("wait", 0.4, "")
    assert bar(None, waiting=True, waited=150)[:2] == ("wait", 0.5)
    assert bar(None, waiting=True, ready=True) == ("wait", 1.0, "READY")


def test_node_bar_counts_from_the_nodes_own_start():
    assert bar(0) == ("test", 0.0, "1:40")
    phase, fraction, text = bar(25)
    assert (phase, fraction, text) == ("test", 0.25, "1:15")
    phase, fraction, text = bar(110)                      # past the load: cooldown, 10 s of 20 s done
    assert phase == "cool" and fraction == 0.5 and text == "0:10"
    assert bar(150)[0] == "cool"
    assert bar(30, code=0) == ("done", 1.0, "done")


def test_node_bar_cooling_reported_by_the_log_before_the_load_time_is_over():
    phase, fraction, _ = bar(90, info=LiveInfo(cooling=True))
    assert phase == "cool" and fraction == 0.0


def test_late_node_gets_its_own_full_time():
    """Two nodes of one test, one began 60 s later: same duration, different position of the bar."""
    early, late = bar(80), bar(20)
    assert early[0] == late[0] == "test"
    assert early[1] == 0.8 and late[1] == 0.2
    assert early[2] == "0:20" and late[2] == "1:20"          # the late node still has 80 s of its 100 s


def test_rows_and_frame_use_each_nodes_own_start(tmp_path, monkeypatch):
    """Hand-made .gate.started files: the late node has its full remaining time, the early one is almost done."""
    import time as _time
    monkeypatch.setenv("NO_COLOR", "1")
    now = _time.time()
    plans, procs = {}, {}

    class Proc:
        def __init__(self): self.pid = 1
        def poll(self): return None
    for name, began in (("early", now - 80), ("late", now - 10), ("prep", None)):
        log = str(tmp_path / f"{name}.log")
        if began is not None:
            Path(log + ".gate.started").write_text(f"{began:.1f}\n")
        plans[name] = (None, StressConfig(node=name, duration=100, cooldown=0), log, log + ".console")
        procs[name] = Proc()
    runner = object.__new__(ParallelSeriesRunner)
    runner.ready_timeout = 300
    rows = runner._rows(plans, procs, 0.0, False)
    assert len(rows) == 3
    text = "\n".join(rows)
    assert "early" in text and "late" in text and "prep" in text
    frame = runner._frame(plans, procs, 0.0, 0.0, False, "")
    body = "\n".join(frame)
    assert "rolling start · 2/3 nodes started" in body
    for name in plans:
        assert name in body
    assert len({parallel.ui.visible_len(x) for x in frame}) == 1
