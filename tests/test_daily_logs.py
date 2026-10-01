"""Day folders: results, debug logs and pytest runs go into <folder>/YYYY-MM-DD/; older flat files can be migrated."""
import os
import re
import time

from stress_test import compare, paths
from stress_test.paths import DAY_RE, log_root_of, migrate_to_daily, result_dirs, today, user_log_dir, user_log_root

from test_integration import run_tool


def _touch(path, day="2026-09-28"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M"))
    os.utime(path, (stamp, stamp))
    return path


def test_today_is_iso_and_user_log_dir_adds_it(tmp_path):
    assert DAY_RE.match(today())
    assert user_log_dir(str(tmp_path)) == tmp_path / today()
    assert user_log_dir(str(tmp_path), flat=True) == tmp_path
    assert user_log_root(str(tmp_path)) == tmp_path


def test_log_root_of_climbs_out_of_day_and_selftest_folders(tmp_path):
    assert log_root_of(tmp_path / "2026-09-30" / "a.log") == tmp_path
    assert log_root_of(tmp_path / "2026-09-30" / "selftest-2026-09-30_10-00-00" / "a.log") == tmp_path
    assert log_root_of(tmp_path / "2026-09-30") == tmp_path
    assert log_root_of(tmp_path / "a.log") == tmp_path
    assert log_root_of(tmp_path) == tmp_path


def test_result_dirs_lists_root_days_newest_first_and_selftests(tmp_path):
    (tmp_path / "2026-09-28").mkdir()
    (tmp_path / "2026-09-30" / "selftest-x").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    dirs = result_dirs(tmp_path)
    assert dirs[0] == tmp_path
    assert dirs.index(tmp_path / "2026-09-30") < dirs.index(tmp_path / "2026-09-28")
    assert tmp_path / "2026-09-30" / "selftest-x" in dirs and tmp_path / "other" not in dirs


def test_migrate_moves_flat_files_by_their_date(tmp_path):
    logs, hidden = tmp_path / "logs", tmp_path / ".logs"
    _touch(logs / "n1-60s-2026-09-28_12-00-00.log", "2026-09-28")
    _touch(logs / "n1-60s-2026-09-29_12-00-00.json", "2026-09-29")
    _touch(logs / "selftest-2026-09-29_10-00-00" / "a.log", "2026-09-29")
    stamp = time.mktime(time.strptime("2026-09-29 12:00", "%Y-%m-%d %H:%M"))
    os.utime(logs / "selftest-2026-09-29_10-00-00", (stamp, stamp))         # a folder is dated by its own last change
    _touch(logs / "baselines" / "n1.json", "2026-09-28")                 # baselines stay where they are
    _touch(logs / ".gitkeep", "2026-09-28")
    _touch(hidden / "debug" / "2026-09-28_10-00-00_1.log", "2026-09-28")
    _touch(hidden / "tests" / "2026-09-28_10-00-00_7" / "pytest.log", "2026-09-28")
    stamp28 = time.mktime(time.strptime("2026-09-28 12:00", "%Y-%m-%d %H:%M"))
    os.utime(hidden / "tests" / "2026-09-28_10-00-00_7", (stamp28, stamp28))
    counts = migrate_to_daily(logs, hidden)
    assert counts == {"logs": 3, "debug": 1, "tests": 1}
    assert (logs / "2026-09-28" / "n1-60s-2026-09-28_12-00-00.log").is_file()
    assert (logs / "2026-09-29" / "n1-60s-2026-09-29_12-00-00.json").is_file()
    assert (logs / "2026-09-29" / "selftest-2026-09-29_10-00-00" / "a.log").is_file()
    assert (logs / "baselines" / "n1.json").is_file() and (logs / ".gitkeep").is_file()
    assert (hidden / "debug" / "2026-09-28" / "2026-09-28_10-00-00_1.log").is_file()
    assert (hidden / "tests" / "2026-09-28" / "2026-09-28_10-00-00_7" / "pytest.log").is_file()
    assert migrate_to_daily(logs, hidden) == {"logs": 0, "debug": 0, "tests": 0}      # second run: nothing to do


def test_migrate_never_overwrites(tmp_path):
    logs = tmp_path / "logs"
    _touch(logs / "a.log", "2026-09-28")
    _touch(logs / "2026-09-28" / "a.log", "2026-09-28")
    (logs / "2026-09-28" / "a.log").write_text("keep", encoding="utf-8")
    migrate_to_daily(logs, tmp_path / ".logs")
    assert (logs / "2026-09-28" / "a.log").read_text(encoding="utf-8") == "keep"
    assert (logs / "a.log").exists()


def test_compare_finds_the_newest_logs_across_day_folders(tmp_path):
    a = _touch(tmp_path / "2026-09-28" / "n1-60s-2026-09-28_10-00-00.log")
    b = _touch(tmp_path / "2026-09-29" / "n1-60s-2026-09-29_10-00-00.log")
    c = _touch(tmp_path / "2026-09-30" / "selftest-2026-09-30_09-00-00" / "n1-60s-2026-09-30_09-00-00.log")
    flat = _touch(tmp_path / "n1-60s-2026-09-27_10-00-00.log")
    assert compare.latest_logs_for_node("n1", tmp_path, 2) == [b, c]
    assert compare.latest_logs_for_node("n1", tmp_path, 4) == [flat, a, b, c]
    assert compare._find_log("n1-60s-2026-09-28_10-00-00", tmp_path) == a
    assert compare._find_log("n1-60s-2026-09-29_10-00-00.log", tmp_path) == b


def test_run_writes_into_todays_folder_inside_log_dir(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    day = tmp_path / "logs" / today()
    assert list(day.glob("fake-node-5s-*.log"))
    assert not list((tmp_path / "logs").glob("*.log"))                     # nothing flat


def test_second_run_the_same_day_uses_the_same_folder(tmp_path):
    for _ in range(2):
        assert run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "2"}).returncode == 0
    days = [d for d in (tmp_path / "logs").iterdir() if d.is_dir()]
    assert [d.name for d in days] == [today()]
    assert len(list(days[0].glob("fake-node-5s-*.log"))) == 2


def test_log_dir_option_gets_the_day_folder_inside(tmp_path):
    target = tmp_path / "mine"
    res = run_tool(tmp_path, "--time", "5", "--log", "--log-dir", str(target), env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert list((target / today()).glob("fake-node-5s-*.log"))


def test_baseline_lives_in_the_logs_root_not_in_a_day_folder(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "2"})
    assert res.returncode == 0, res.stdout + res.stderr
    log = next((tmp_path / "logs" / today()).glob("fake-node-5s-*.log"))
    res = run_tool(tmp_path, "--set-baseline", log.name, "--cooldown", "0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "logs" / "baselines" / "fake-node.json").is_file()
    assert not (tmp_path / "logs" / today() / "baselines").exists()


def test_migrate_logs_option_runs_without_a_cluster(tmp_path):
    res = run_tool(tmp_path, "--migrate-logs")
    assert res.returncode == 0 and "Moved into day folders" in res.stdout
