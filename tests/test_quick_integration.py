"""--quick against the fake kubectl: only the node, no further questions."""
from test_integration import run_tool


def test_quick_single_node_runs_without_questions(tmp_path):
    res = run_tool(tmp_path, "--quick", "--time", "5", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "⚡ Quick test: fake-node · 5 s" in res.stdout
    assert list((tmp_path / "logs").glob("fake-node-5s-*.log"))     # --quick logs
