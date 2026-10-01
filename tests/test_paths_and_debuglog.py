"""Log paths and unexpected errors (calling the code directly, without a subprocess)."""
from pathlib import Path

from stress_test import cli, debuglog
from stress_test.kube import Kubectl
from stress_test.paths import HIDDEN_LOGS, PROJECT_ROOT, debug_log_dir, today, user_log_dir, user_log_root

FAKE = Path(__file__).parent / "fake_kubectl.py"


def test_user_log_dir_defaults_to_project_logs_regardless_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert user_log_root() == PROJECT_ROOT / "logs"
    assert user_log_dir() == PROJECT_ROOT / "logs" / today()          # results go into the folder of the day
    assert user_log_dir("logs") == PROJECT_ROOT / "logs" / today()


def test_user_log_dir_relative_and_absolute(tmp_path):
    assert user_log_root("results/g6") == PROJECT_ROOT / "results" / "g6"
    assert user_log_dir("results/g6") == PROJECT_ROOT / "results" / "g6" / today()
    assert user_log_dir(str(tmp_path / "x")) == tmp_path / "x" / today()
    assert user_log_dir(str(tmp_path / "x"), flat=True) == tmp_path / "x"


def test_debug_dir_default_is_hidden_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("STRESS_TEST_DEBUG_DIR", raising=False)
    assert debug_log_dir() == HIDDEN_LOGS / "debug" / today()
    assert HIDDEN_LOGS.name == ".logs"                 # hidden folder (dot)
    monkeypatch.setenv("STRESS_TEST_DEBUG_DIR", str(tmp_path))
    assert debug_log_dir() == tmp_path


def test_setup_creates_new_file_each_time(monkeypatch, tmp_path):
    monkeypatch.setenv("STRESS_TEST_DEBUG_DIR", str(tmp_path))
    first = debuglog.setup(["--a"])
    debuglog.shutdown()
    second = debuglog.setup(["--b"])
    debuglog.shutdown()
    assert first.parent == tmp_path and second.parent == tmp_path
    assert "argv=['--a']" in first.read_text(encoding="utf-8")
    assert "argv=['--b']" in second.read_text(encoding="utf-8")


def test_setup_never_raises_when_dir_is_not_writable(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")                              # the directory cannot be created
    monkeypatch.setenv("STRESS_TEST_DEBUG_DIR", str(blocker / "pod"))
    assert debuglog.setup([]) is None


def test_unexpected_exception_goes_to_debug_log_with_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STRESS_TEST_DEBUG_DIR", str(tmp_path))
    monkeypatch.setenv("KUBECTL", str(FAKE))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path))

    def boom(self, name):
        raise RuntimeError("boom-test")
    monkeypatch.setattr(Kubectl, "get_node", boom)

    code = cli.main(["--node", "fake-node", "--non-interactive", "--time", "5"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Unexpected error: boom-test" in out
    text = next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert "Traceback" in text and "RuntimeError: boom-test" in text
    assert "exit code 1" in text
