"""Scheduling: start times, the waiting process, systemd units / timers, the planner and the T key of the menu."""
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from stress_test import background, menu, schedule
from test_integration import FAKE, ROOT


# ---------------- start times -------------------------------------------------------------------------------------

NOW = time.mktime((2026, 10, 1, 14, 0, 0, 0, 0, -1))


@pytest.mark.parametrize("text, expected", [
    ("30m", NOW + 1800), ("2h", NOW + 7200), ("1h30m", NOW + 5400),
    ("22:30", time.mktime((2026, 10, 1, 22, 30, 0, 0, 0, -1))),
    ("09:15", time.mktime((2026, 10, 2, 9, 15, 0, 0, 0, -1))),                      # passed today -> tomorrow
    ("2026-10-03 02:00", time.mktime((2026, 10, 3, 2, 0, 0, 0, 0, -1))),
    ("2026-10-03 02:00:30", time.mktime((2026, 10, 3, 2, 0, 30, 0, 0, -1))),
    ("3.10.2026 02:00", time.mktime((2026, 10, 3, 2, 0, 0, 0, 0, -1))),
    ("3.10. 02:00", time.mktime((2026, 10, 3, 2, 0, 0, 0, 0, -1))),
    ("1.10. 10:00", time.mktime((2027, 10, 1, 10, 0, 0, 0, 0, -1))),                # passed this year -> next year
])
def test_parse_start_forms(text, expected):
    assert schedule.parse_start(text, NOW) == expected


@pytest.mark.parametrize("bad", ["", "abc", "25:00", "12:60", "2026-10-01 13:00", "31.2. 10:00", "2026-13-40 10:00"])
def test_parse_start_rejects_bad_or_past(bad):
    with pytest.raises(ValueError):
        schedule.parse_start(bad, NOW)


def test_describe_when():
    text = schedule.describe_when(NOW + 8 * 3600 + 12 * 60, NOW)
    assert "(in 8 h 12 min)" in text and "22:12" in text
    assert "due" in schedule.describe_when(NOW - 5, NOW)


def test_cli_schedule_arg_accepts_dates_and_reports_errors():
    import argparse
    from stress_test.cli import schedule_arg
    assert schedule_arg("2099-01-01 00:00") > time.time()
    with pytest.raises(argparse.ArgumentTypeError):
        schedule_arg("never")


# ---------------- waiting process (A) -----------------------------------------------------------------------------------

@pytest.fixture
def running_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path / "running"))
    monkeypatch.setenv("STRESS_TEST_SCHEDULED_DIR", str(tmp_path / "scheduled"))
    monkeypatch.setenv("STRESS_TEST_SYSTEMD_DIR", str(tmp_path / "units"))
    return tmp_path


def test_wait_registers_waiting_then_running(running_dir):
    seen = []

    def fake_sleep(seconds):
        seen.append((seconds, background.list_running()))

    start = time.time() + 600
    schedule.wait_registered(start, "ab12cd", "node1", 120, "/tmp/x.log", "/tmp/x.console", "GPU 2 min · node1", sleep=fake_sleep)
    (seconds, during), = seen
    assert 590 < seconds <= 600 and during[0]["scheduled_at"] == start and during[0]["title"] == "GPU 2 min · node1"
    after = background.list_running()
    assert after[0]["scheduled_at"] is None and after[0]["run_id"] == "ab12cd"


def test_planned_process_is_listed_and_cancellable(running_dir):
    start = time.time() + 3600
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "stress_test"])      # looks like ours for _is_ours
    try:
        background.register("zz99", "node1", 60, None, "/tmp/c")        # a normal running test: not a plan
        import json as _j
        (running_dir / "running").mkdir(exist_ok=True)
        (running_dir / "running" / "pl01.json").write_text(_j.dumps({
            "run_id": "pl01", "pid": os.getpid(), "node": "node1", "duration": 60, "started": time.time(),
            "log": None, "console": "/tmp/c", "scheduled_at": start, "title": "CPU 1 min · node1"}))
        plans = schedule.list_plans()
        assert [p["id"] for p in plans] == ["pl01"] and plans[0]["state"] == "waiting" and plans[0]["kind"] == "process"
        text = "\n".join(schedule.format_plans(plans))
        assert "pl01" in text and "waiting process" in text and "CPU 1 min" in text
        assert "PLANNED for" in background.format_running(background.list_running())
    finally:
        proc.kill()


def test_empty_plan_list(running_dir):
    assert schedule.list_plans() == [] and schedule.format_plans([]) == ["Nothing is scheduled."]


# ---------------- systemd timers (C) ------------------------------------------------------------------------------------

def test_unit_files_quote_arguments_and_clean_up_after_the_run():
    argv = ["--node", "n1", "--notes", 'a "quoted" 100% $HOME', "--yes"]
    service, timer = schedule.unit_texts("a1b2c3", argv, NOW + 3600, "GPU 2 min · n1", ROOT, "/usr/bin/python3")
    assert "ExecStart=/usr/bin/python3 -m stress_test --node n1 --notes \"a \\\"quoted\\\" 100%% $$HOME\" --yes" in service
    assert "ExecStopPost=-/usr/bin/python3 -m stress_test --cancel-plan a1b2c3" in service and "Type=oneshot" in service
    assert f"PYTHONPATH={ROOT}" in service and f"WorkingDirectory={ROOT}" in service
    assert "OnCalendar=2026-10-01 15:00:00" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer


def _fake_systemctl(tmp_path):
    """A systemctl that records its calls and answers show-environment / show."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "systemctl"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/systemctl.calls
case "$*" in
  *show-environment*) echo HOME=/x ;;
  *"show"*".service"*"ActiveState"*) echo inactive ;;
  *"show"*"ActiveState"*) cat {tmp_path}/state 2>/dev/null || echo active ;;
esac
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_install_list_and_cancel_a_systemd_plan(running_dir, monkeypatch):
    bin_dir = _fake_systemctl(running_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    assert schedule.systemd_available()
    start = time.time() + 7200
    record = schedule.install_timer("ff00aa", ["--node", "n1", "--yes"], start, "GPU 2 min · n1", "n1")
    units = running_dir / "units"
    assert (units / "stress-test-ff00aa.service").exists() and (units / "stress-test-ff00aa.timer").exists()
    calls = (running_dir / "systemctl.calls").read_text()
    assert "--user daemon-reload" in calls and "--user enable --now stress-test-ff00aa.timer" in calls
    plans = schedule.list_plans()
    assert plans[0]["id"] == "ff00aa" and plans[0]["kind"] == "systemd" and plans[0]["state"] == "waiting"
    assert "systemd timer (survives a restart)" in "\n".join(schedule.format_plans(plans))
    ok, message = schedule.cancel("ff00aa")
    assert ok and "cancelled" in message
    assert not (units / "stress-test-ff00aa.timer").exists() and not (running_dir / "scheduled" / "ff00aa.json").exists()
    assert "--user disable --now stress-test-ff00aa.timer" in (running_dir / "systemctl.calls").read_text()
    assert schedule.list_plans() == []


def test_finished_systemd_plans_are_cleaned_up_by_the_list(running_dir, monkeypatch):
    bin_dir = _fake_systemctl(running_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    schedule.install_timer("dd11ee", ["--yes"], time.time() - 300, "old", "n1")     # passed more than the 2 min grace ago
    (running_dir / "state").write_text("inactive\n")
    assert schedule.list_plans() == [] and not (running_dir / "units" / "stress-test-dd11ee.timer").exists()


def test_systemd_not_available_without_systemctl(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert not schedule.systemd_available()


# ---------------- the CLI: --scheduled, --persistent, --stop ----------------------------------------------------------------

def _cli(tmp_path, *args, extra_env=None):
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), FAKE_RUN="3",
               STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"), STRESS_TEST_SCHEDULED_DIR=str(tmp_path / "scheduled"),
               STRESS_TEST_SYSTEMD_DIR=str(tmp_path / "units"), PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "stress_test", *args], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)


def test_scheduled_and_persistent_plan_end_to_end(running_dir):
    _fake_systemctl(running_dir)
    res = _cli(running_dir, "--node", "fake-node", "--time", "30", "--cooldown", "0", "--yes", "--non-interactive",
               "--schedule", "2h", "--persistent", "--log-dir", str(running_dir / "logs"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "TEST PLANNED as a systemd timer" in res.stdout and "Plan id" in res.stdout
    service = next((running_dir / "units").glob("*.service")).read_text()
    assert "--node fake-node" in service and "--profile classic" in service and "--non-interactive" in service
    assert "--schedule" not in service and "--persistent" not in service            # the timer is the schedule
    plan_id = next((running_dir / "scheduled").glob("*.json")).stem
    listing = _cli(running_dir, "--scheduled")
    assert plan_id in listing.stdout and "CPU" in listing.stdout and "systemd timer" in listing.stdout
    stopped = _cli(running_dir, "--stop", plan_id)
    assert stopped.returncode == 0 and "cancelled" in stopped.stdout
    assert "Nothing is scheduled." in _cli(running_dir, "--scheduled").stdout


def test_persistent_series_replay_has_the_nodes_and_no_questions(running_dir):
    _fake_systemctl(running_dir)
    res = _cli(running_dir, "--nodes", "n1,n2", "--time", "30", "--cooldown", "0", "--yes", "--non-interactive",
               "--schedule", "3h", "--persistent", "--no-parallel", "--log-dir", str(running_dir / "logs"),
               extra_env={"FAKE_NODES": '[{"name":"n1"},{"name":"n2"}]'})
    assert res.returncode == 0, res.stdout + res.stderr
    service = next((running_dir / "units").glob("*.service")).read_text()
    assert "--nodes n1,n2" in service and "--no-parallel" in service and "--node n1" not in service.replace("--nodes n1", "")
    assert "--log-file" not in service and "--concurrent" not in service


def test_persistent_without_systemd_is_refused(running_dir):
    res = _cli(running_dir, "--node", "fake-node", "--time", "30", "--yes", "--non-interactive", "--schedule", "2h",
               "--persistent", "--log-dir", str(running_dir / "logs"), extra_env={"PATH": "/nonexistent"})
    # the python interpreter is found by its absolute path, systemctl is not on PATH
    assert res.returncode != 0 or "not available" in res.stdout


def test_stop_of_an_unknown_plan_is_an_error(running_dir):
    res = _cli(running_dir, "--stop", "nope")
    assert res.returncode != 0 and "No running background test matches" in res.stdout


def test_selftest_can_be_planned_as_a_timer(running_dir):
    _fake_systemctl(running_dir)
    res = _cli(running_dir, "--self-test", "--self-test-level", "quick", "--self-test-ack", "--yes", "--non-interactive",
               "--schedule", "5h", "--persistent", "--log-dir", str(running_dir / "logs"))
    assert res.returncode == 0, res.stdout + res.stderr
    service = next((running_dir / "units").glob("*.service")).read_text()
    assert "--self-test --self-test-level quick --self-test-ack --yes --non-interactive" in service
    assert "FULL self-test (quick)" in service


# ---------------- the menu: T key and 9 SCHEDULE ------------------------------------------------------------------------

def scripted(*answers):
    it = iter(answers)
    return lambda prompt, default="": next(it)


def test_start_time_dialog_in_at_date_and_now(running_dir, monkeypatch):
    monkeypatch.setattr(schedule, "systemd_available", lambda: False)
    got = menu.start_time_dialog(scripted("1", "2h"), NOW)
    assert got == {"epoch": NOW + 7200, "persistent": False}
    assert menu.start_time_dialog(scripted("2", "bad", "22:30"), NOW)["epoch"] == schedule.parse_start("22:30", NOW)
    assert menu.start_time_dialog(scripted("3", "2026-10-03 02:00"), NOW)["epoch"] == schedule.parse_start("2026-10-03 02:00", NOW)
    assert menu.start_time_dialog(scripted("4"), NOW) == {}
    assert menu.start_time_dialog(scripted("0"), NOW) is None


def test_start_time_dialog_asks_for_persistence_only_with_systemd(running_dir, monkeypatch):
    monkeypatch.setattr(schedule, "systemd_available", lambda: True)
    assert menu.start_time_dialog(scripted("1", "1h", "y"), NOW)["persistent"] is True
    assert menu.start_time_dialog(scripted("1", "1h", ""), NOW)["persistent"] is False


def test_schedule_options_waiting_process_or_timer():
    a = menu.schedule_options({"epoch": NOW, "persistent": False})
    c = menu.schedule_options({"epoch": NOW, "persistent": True})
    assert a[0] == "--schedule" and a[2] == "--background" and c[2] == "--persistent"
    assert schedule.parse_start(a[1], NOW - 5) == NOW


def test_t_then_a_test_plans_it_once(running_dir, monkeypatch, capsys):
    monkeypatch.setattr(schedule, "systemd_available", lambda: False)
    ran = []
    answers = ["t", "1", "2h", "5", "", "", "5", "", "", "0"]       # T: in 2h; QUICK (extras: Enter); then QUICK again: now plain
    ask = scripted(*answers)
    menu.run_menu(ask=ask, run=lambda o: ran.append(o) or 0, cluster=lambda: "c")
    assert "--quick" in ran[0] and "--schedule" in ran[0] and "--background" in ran[0]
    assert "--schedule" not in ran[1]                                  # the plan applies to ONE test
    assert "next test starts" in capsys.readouterr().out


def test_planner_screen_empty_plan_and_cancel(running_dir, monkeypatch, capsys):
    monkeypatch.setattr(schedule, "systemd_available", lambda: True)
    _fake_systemctl(running_dir)
    monkeypatch.setenv("PATH", f"{running_dir / 'bin'}:{os.environ['PATH']}")
    ask = scripted("9", "0", "q")
    menu.run_menu(ask=ask, run=lambda o: 0, cluster=lambda: "c")
    assert "Nothing is scheduled." in capsys.readouterr().out
    schedule.install_timer("ab12cd", ["--yes"], time.time() + 3600, "GPU 2 min · n1", "n1")
    ask = scripted("9", "2", "ab12cd", "0", "q")                       # list, cancel the plan, back, quit
    menu.run_menu(ask=ask, run=lambda o: 0, cluster=lambda: "c")
    out = capsys.readouterr().out
    assert "ab12cd" in out and "GPU 2 min · n1" in out and "cancelled" in out and schedule.list_plans() == []


def test_planner_plans_a_test_through_the_dialogs(running_dir, monkeypatch):
    monkeypatch.setattr(schedule, "systemd_available", lambda: False)
    ran = []
    ask = scripted("9", "1", "1", "30m", "5", "", "0", "q")             # plan a test, in 30m, QUICK, extras none, back
    menu.run_menu(ask=ask, run=lambda o: ran.append(o) or 0, cluster=lambda: "c")
    assert ran and "--quick" in ran[0] and "--schedule" in ran[0] and "--background" in ran[0]


@pytest.mark.parametrize("cols", [50, 80, 120])
def test_menu_shows_9_schedule_and_the_t_key(cols):
    text = "\n".join(menu.menu_lines("c", [], False, cols=cols))
    assert "SCHEDULE" in text and "9 ▸" in text and "⏰ T" in text
