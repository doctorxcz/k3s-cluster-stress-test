"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _never_the_real_menu_state(tmp_path, monkeypatch):
    """The menu remembers settings (probe pods kept for N hours, default node ...) in .logs/menu-state.json; a test must never read the
    user's real file (it changed the commands the menu starts) nor write to it. A test that needs a state sets its own path."""
    monkeypatch.setenv("STRESS_TEST_MENU_STATE", str(tmp_path / "menu-state.json"))


@pytest.fixture(autouse=True)
def _never_the_real_doctor_reports(tmp_path, monkeypatch):
    """The menu banner and the settings read the saved cluster-check reports of <project>/logs/doctor; a test must neither see the
    user's real ones nor write there (a test that passes a root explicitly is not affected)."""
    from stress_test import doctor
    real = doctor.report_dir
    monkeypatch.setattr(doctor, "report_dir", lambda root=None: real(root) if root else tmp_path / "doctor-default")
