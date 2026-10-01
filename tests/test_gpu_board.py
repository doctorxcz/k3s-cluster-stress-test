"""Multi-node GPU test: the shared table (GpuBoard), the series run, the image pre-pull and the small GPU option checks."""
import io
import os

import pytest

from stress_test import cli, gpuscan, ui
from stress_test.gpu import GpuSummary
from stress_test.gpuscan import DONE, FAILED, RUNNING, SKIPPED, WAITING, WARN, GpuBoard
from test_integration import run_tool


def summary(max_temp=67, findings=(), throttle=0.0):
    return GpuSummary(10, max_temp, 99.0, None, None, 1200, 1354, throttle, 0.0, [], "warn" if findings else "ok", list(findings))


def test_board_states_and_position():
    b = GpuBoard(["a", "b", "c"])
    assert [b.rows[n].state for n in "abc"] == [WAITING] * 3 and b.position == "0/3"
    b.start("a")
    b.live("a", temp=60, clock=1300, progress=0.4, card="Quadro P620")
    b.live("a", temp=67)
    assert b.rows["a"].max_temp == 67 and b.rows["a"].temp == 67 and b.position == "1/3"
    b.finish("a", 0, summary())
    b.start("b")
    b.finish("b", 3)
    assert b.rows["a"].state == DONE and b.rows["b"].state == FAILED and "overheated" in b.rows["b"].note
    assert b.position == "2/3"
    b.skip_rest("not tested (interrupted)")
    assert b.rows["c"].state == SKIPPED
    b.rows["c"].state = WAITING
    b.start("c")
    b.finish("c", 0, summary(findings=["GPU reached 77 °C"]))
    assert b.rows["c"].state == WARN


def test_board_ignores_updates_for_nodes_that_are_not_running():
    b = GpuBoard(["a", "b"])
    b.live("b", temp=90)
    assert b.rows["b"].temp is None


@pytest.mark.parametrize("width", [50, 80, 120])
def test_board_table_is_aligned_and_marks_every_state(width):
    b = GpuBoard(["worker-1", "node-two", "node-three", "node-four"], {"worker-1": "Quadro P620"})
    b.start("worker-1")
    b.finish("worker-1", 0, summary())
    b.start("node-two")
    b.live("node-two", temp=71, clock=1620, progress=0.54)
    b.rows["node-four"].state, b.rows["node-four"].note = FAILED, "error"
    lines = b.table(False, width)
    text = "\n".join(lines)
    assert len({ui.visible_len(x) for x in lines}) == 1 and max(ui.visible_len(x) for x in lines) <= width
    assert "✅" in text and "▶" in text and "⏳" in text and "❌" in text and "3/4" in text
    assert "54 %" in text and "71 °C" in text and "67 °C max" in text


def test_gpu_frame_has_the_board_on_top(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "100")
    b = GpuBoard(["n1", "n2"])
    b.start("n1")
    g = ui.GpuScreen("n1", total=60, stream=io.StringIO(), board=b)
    g.begin_test()
    g.set_card(["GPU: Quadro P620"])
    g.reading("[1] CPU: 5% | RAM: 1 MiB (1%) | Temp: CPU: 46°C | Clock: 3614 MHz | GPU: 60°C ? 1300MHz 99% 1800/2048MiB thr=0x0")
    lines = [ui.ANSI.sub("", ln) if hasattr(ui, "ANSI") else ln for ln in g._lines()]
    text = "\n".join(lines)
    assert "GPU TEST · 2 nodes, one after another · 1/2" in text and "n2" in text and "waiting" in text
    assert b.rows["n1"].card == "Quadro P620" and b.rows["n1"].temp == 60
    assert len({ui.visible_len(x) for x in lines}) == 1


FAKE2 = '[{"name":"g1"},{"name":"g2"}]'


def test_series_gpu_test_prints_one_table_and_logs_the_gpu_profile(tmp_path):
    from test_integration import FAKE
    import subprocess, sys
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=os.path.dirname(os.path.dirname(__file__)),
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), FAKE_GPU="1", FAKE_RUN="4", FAKE_NODES=FAKE2,
               STRESS_NO_LIVE="1")
    res = subprocess.run([sys.executable, "-m", "stress_test", "--nodes", "g1,g2", "--profile", "gpu", "--time", "30",
                          "--cooldown", "0", "--yes", "--non-interactive", "--interval", "0.5", "--log-dir", str(tmp_path / "logs")],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=200)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.count("GPU TEST SUMMARY") >= 2                      # after the first node and the final table
    final = res.stdout.rsplit("GPU TEST SUMMARY", 1)[1]
    assert final.count("✅ done") == 2 and "g1" in final and "g2" in final and "CLUSTER SUMMARY" not in res.stdout
    series_log = next((tmp_path / "logs").rglob("cluster-*.log")).read_text(encoding="utf-8")
    assert "Profile: gpu" in series_log and "Profile: classic" not in series_log


def test_prepull_warms_every_node_and_leaves_no_pod(tmp_path, monkeypatch):
    here = os.path.dirname(__file__)
    monkeypatch.setenv("FAKE_STATE", str(tmp_path))
    from stress_test.kube import Kubectl
    said = []
    res = gpuscan.prepull(Kubectl(os.path.join(here, "fake_kubectl.py")), ["g1", "g2"], "nvidia/cuda:test", said.append, timeout=30)
    assert res == {"g1": True, "g2": True} and sum("image ready" in s for s in said) == 2
    import json
    pods = [json.loads(p.read_text()) for p in tmp_path.glob("manifest-hw-info-*.json")]
    assert len(pods) == 2 and all(p["spec"]["containers"][0]["image"] == "nvidia/cuda:test" for p in pods)
    assert all("privileged" not in json.dumps(p) and "hostPath" not in json.dumps(p) for p in pods)


def test_gpu_options_without_the_gpu_profile_are_reported(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--gpu-max-temp", "70", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0 and "--gpu-* options only apply to --profile gpu" in res.stdout
    ok = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "3"})
    assert "--gpu-* options" not in ok.stdout


def test_settings_show_both_stop_temperatures_for_the_gpu_test(tmp_path):
    res = run_tool(tmp_path, "--profile", "gpu", "--time", "30", "--yes", "--gpu-max-temp", "78",
                   env_extra={"FAKE_GPU": "1", "FAKE_RUN": "4"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "GPU 78 °C (warning from 73 °C) · CPU 85 °C (2 readings in a row)" in res.stdout
