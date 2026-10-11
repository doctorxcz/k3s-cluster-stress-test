"""The regular cluster check: dates, the waiting loop, a missed date, the notification text."""
import time
from datetime import datetime

import pytest

from stress_test import background, health_plan as hp
from stress_test.dashboard import ClusterView


def at(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute).timestamp()


def test_parse_at_accepts_normal_times_only():
    assert hp.parse_at("03:00") == (3, 0) and hp.parse_at("3:30") == (3, 30) and hp.parse_at("3.05") == (3, 5)
    assert hp.normalize_at("3:5".replace(":5", ":05")) == "03:05"
    for bad in ("", "25:00", "12:60", "noon", "3"):
        with pytest.raises(ValueError):
            hp.parse_at(bad)


def test_first_run_is_the_next_time_of_day():
    assert hp.first_run(at(11, 10), "03:00") == at(12, 3)                   # 03:00 already passed today
    assert hp.first_run(at(11, 1), "03:00") == at(11, 3)
    assert hp.first_run(at(11, 2, 58), "03:00") == at(11, 3)
    assert hp.first_run(at(11, 3) - 30, "03:00") == at(12, 3)               # less than a minute ahead counts as passed


def test_next_after_keeps_the_wall_clock_time_and_skips_past_dates():
    assert hp.next_after(at(12, 3), 14, at(12, 3, 5)) == at(26, 3)
    assert hp.next_after(at(12, 3), 7, at(30, 0)) == at(2, 3, month=11)     # 19, 26 passed: next is 2 Nov
    assert hp.next_after(at(12, 3), 0, at(12, 4)) == at(13, 3)              # a zero interval is one day


def test_check_command_and_title():
    cmd = hp.check_argv(True, "a,b", "/x")
    assert cmd[1:3] == ["-m", "stress_test"] and "--doctor" in cmd and "--non-interactive" in cmd
    assert "--doctor-load" in cmd and "--doctor-load-ok" in cmd and cmd[cmd.index("--nodes") + 1] == "a,b" and cmd[-1] == "/x"
    plain = hp.check_argv(False)
    assert "--doctor-load" not in plain and "--yes" not in plain                       # no load without the pre-authorisation
    assert "load" in hp.title_of(14, "03:00", True) and "load" not in hp.title_of(14, "03:00", False)


def test_summary_of_a_report():
    assert hp.summary(None)[0] == "Cluster check"
    report = {"verdict": "needs attention", "scores": {"cluster": 62},
              "findings": [{"level": "WARN", "title": "Swap is used"}, {"level": "FAIL", "title": "CPU at 90 °C"}, {"level": "OK", "title": "x"}]}
    title, body = hp.summary(report)
    assert "needs attention" in title and "62/100" in title
    assert "1 failure(s), 1 warning(s)" in body and "CPU at 90 °C" in body
    assert "Nothing to fix" in hp.summary({"verdict": "healthy", "scores": {}, "findings": []})[1]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def registry(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "update", lambda run_id, **f: calls.append((run_id, f)))
    return calls


def test_loop_waits_runs_tells_and_plans_the_next_date(registry):
    clock = Clock(at(11, 10))
    runs, told = [], []
    code = hp.loop("rid", 14, "03:00", False, "", None, clock=clock, sleep=clock.sleep,
                   runner=lambda argv: runs.append((clock.now, argv)) or 0, after=lambda w, root: told.append(w), max_rounds=2)
    assert code == 0 and len(runs) == 2
    assert at(12, 3) <= runs[0][0] < at(12, 3) + 60 and at(26, 3) <= runs[1][0] < at(26, 3) + 60       # 14 days later
    assert len(told) == 2 and "--doctor-last" in told[0]
    assert registry[0][1]["scheduled_at"] == at(12, 3) and registry[0][1]["phase"] == "waiting"
    assert any(f.get("phase") == "checking" and f.get("scheduled_at") is None for _r, f in registry)


def test_a_missed_date_is_skipped_not_caught_up(registry):
    clock = Clock(at(12, 3) + 5 * 3600)                     # the computer was off at 03:00 and is on at 08:00
    runs = []
    hp.loop("rid", 14, "03:00", False, "", None, clock=clock, sleep=clock.sleep, runner=lambda a: runs.append(a) or 0,
            after=lambda w, r: None, first=at(12, 3), max_rounds=1)
    assert runs == []
    assert registry[0][1]["scheduled_at"] == at(12, 3) and all(f.get("phase") != "checking" for _r, f in registry)       # it never ran
    runs.clear()
    clock2 = Clock(at(12, 3) + 600)                          # 10 minutes late is still fine
    hp.loop("rid", 14, "03:00", False, "", None, clock=clock2, sleep=clock2.sleep, runner=lambda a: runs.append(a) or 0,
            after=lambda w, r: None, first=at(12, 3), max_rounds=1)
    assert len(runs) == 1


def test_after_check_notifies_and_opens_the_window(tmp_path):
    sent = []
    out = hp.after_check(["x", "--doctor-last"], tmp_path, notifier=lambda t, b: sent.append((t, b)) or True,
                         window=lambda argv: sent.append(argv) or True,
                         reader=lambda root: {"verdict": "healthy", "scores": {"cluster": 98}, "findings": []})
    assert out["notified"] and out["window"] and "healthy" in out["title"] and sent[1] == ["x", "--doctor-last"]
    out = hp.after_check(["x"], None, notifier=lambda t, b: False, window=lambda a: False, reader=lambda root: None)
    assert out == {"notified": False, "window": False, "title": "Cluster check", "body": "The check ran but no report was found."}


def test_running_plan_and_the_overdue_hint(monkeypatch):
    monkeypatch.setattr(background, "list_running", lambda: [{"run_id": "a", "kind": "x"}, {"run_id": "b", "kind": hp.KIND, "pid": 1}])
    assert hp.running_plan()["run_id"] == "b"
    assert hp.overdue_hint({"enabled": True}) == ""
    monkeypatch.setattr(background, "list_running", lambda: [])
    assert "not running" in hp.overdue_hint({"enabled": True})
    assert hp.overdue_hint({"enabled": False}) == ""


# ---------------- the program: settings dialog, command line, banner ------------------------------------------------------

def scripted(*answers):
    it = iter(answers)
    return lambda prompt, default="": next(it)


def test_settings_dialog_starts_the_plan_with_the_answers(monkeypatch):
    from stress_test import menu
    started = []
    monkeypatch.setattr(hp, "start", lambda *a, **k: started.append(a) or 0)
    plan = menu._doctor_plan_dialog(scripted("y", "7", "x", "4:30", "y"), {})
    assert plan == {"enabled": True, "every_days": 7, "at": "04:30", "load_ok": True}              # "x" was refused, then 4:30
    assert started == [(7, "04:30", True)]
    stopped = []
    monkeypatch.setattr(menu, "_stop_doctor_plan", lambda: stopped.append(1))
    assert menu._doctor_plan_dialog(scripted("n"), {"enabled": True}) == {"enabled": False} and stopped
    assert "every 7 days at 04:30" in menu._doctor_plan_text({"doctor": plan}) and "load test" in menu._doctor_plan_text({"doctor": plan})
    assert "off" in menu._doctor_plan_text({})


def test_command_line(tmp_path, capsys):
    import subprocess
    import sys
    from test_integration import FAKE, ROOT
    import os
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), NO_COLOR="1",
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
    bad = subprocess.run([sys.executable, "-m", "stress_test", "--doctor-every", "14", "--doctor-at", "25:99"], cwd=tmp_path, env=env, text=True,
                         capture_output=True, timeout=60)
    assert bad.returncode == 2 and "not a time of day" in bad.stdout
    none = subprocess.run([sys.executable, "-m", "stress_test", "--doctor-last", "--log-dir", str(tmp_path / "logs")], cwd=tmp_path, env=env,
                          text=True, capture_output=True, timeout=60)
    assert none.returncode == 1 and "No cluster check yet" in none.stdout


def test_banner_new_report_then_seen(tmp_path):
    from stress_test import doctor
    assert doctor.banner(tmp_path) == "" and doctor.unseen_report(tmp_path) is None
    report = doctor.build_report(ClusterView(), version="x", when=time.time() - 3 * 86400)
    doctor.save(report, tmp_path)
    line = doctor.banner(tmp_path)
    assert "3 days" in line.replace("d ", " days ") or "ago" in line
    assert "NEW" in line and doctor.unseen_report(tmp_path) is not None
    doctor.mark_seen(report.when, tmp_path)
    assert "NEW" not in doctor.banner(tmp_path) and doctor.unseen_report(tmp_path) is None


def test_show_last_prints_the_report_and_marks_it_seen(tmp_path, capsys):
    from stress_test import doctor
    assert doctor.show_last(None, tmp_path) == 1
    doctor.save(doctor.build_report(ClusterView(), version="x", when=time.time() - 60), tmp_path)
    assert doctor.show_last(None, tmp_path) == 0
    assert "CLUSTER DOCTOR" in capsys.readouterr().out and doctor.unseen_report(tmp_path) is None
