"""The regular cluster check: the program's own scheduler.

A detached process (like a planned test) waits for the next date, runs `--doctor` as a child, tells the user (a desktop
notification, a terminal window with the report, a banner in the menu next time) and plans the date after that. It is listed in
`--status`, `--scheduled` and the planner (menu 9) and stopped with `--stop ID` like any other waiting plan.

It lives as long as the computer runs: after a restart it is gone, and a date that passed while the computer was off is
skipped (the next one counts); the menu offers to start it again.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from . import background, notify, ui

log = logging.getLogger(__name__)

KIND = "doctor-plan"
DEFAULT_DAYS, DEFAULT_AT = 14, "03:00"
DAY_CHOICES = (7, 14, 30)
MAX_DAYS = 365
GRACE_S = 3600                      # a check that is more than an hour late (the computer slept) is skipped
SLICE_S = 30                        # the wait is cut into slices: a suspend / a clock change is noticed
_AT = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def parse_at(text: str) -> tuple:
    """"03:00" / "3:30" / "3.30" -> (3, 0); ValueError for anything else."""
    m = _AT.match((text or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"'{text}' is not a time of day like 03:00")
    return int(m.group(1)), int(m.group(2))


def normalize_at(text: str) -> str:
    h, m = parse_at(text)
    return f"{h:02d}:{m:02d}"


def first_run(now: float, at: str) -> float:
    """The next time of day `at` that is still ahead (at least a minute from now)."""
    h, m = parse_at(at)
    when = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
    if when.timestamp() < now + 60:
        when += timedelta(days=1)
    return when.timestamp()


def next_after(due: float, every_days: int, now: float) -> float:
    """The date after `due` (same wall-clock time, every_days later) that is still in the future: dates that passed are skipped."""
    when = datetime.fromtimestamp(due)
    step = timedelta(days=max(1, int(every_days)))
    when += step
    while when.timestamp() <= now:
        when += step
    return when.timestamp()


def title_of(every_days: int, at: str, load_ok: bool) -> str:
    return f"Cluster check every {every_days} days at {at}" + (" (with the mini load test)" if load_ok else "")


def check_argv(load_ok: bool, nodes: str = "", log_dir: Optional[str] = None) -> list:
    argv = [sys.executable, "-m", "stress_test", "--doctor", "--non-interactive"]
    if load_ok:
        argv += ["--doctor-load", "--doctor-load-ok"]
    if nodes:
        argv += ["--nodes", nodes]
    if log_dir:
        argv += ["--log-dir", log_dir]
    return argv


def summary(report: Optional[dict]) -> tuple:
    """(title, body) of the notification for a saved report."""
    if not report:
        return "Cluster check", "The check ran but no report was found."
    bad = [f for f in report.get("findings", []) if f["level"] == "FAIL"]
    warn = [f for f in report.get("findings", []) if f["level"] == "WARN"]
    score = report.get("scores", {}).get("cluster", 100)
    title = f"Cluster check: {report.get('verdict', '?')} ({score}/100)"
    first = (bad or warn)[:1]
    body = f"{len(bad)} failure(s), {len(warn)} warning(s)." + (f" First: {first[0]['title']}" if first else " Nothing to fix.")
    return title, body


def running_plan() -> Optional[dict]:
    """The record of the scheduler that runs now (one at a time), None when there is none."""
    for record in background.list_running():
        if record.get("kind") == KIND:
            return record
    return None


def stop_running() -> bool:
    record = running_plan()
    if not record:
        return False
    ok, _message = background.stop(record["run_id"], wait_s=10)
    return ok


def run_check(argv: list, runner: Callable = subprocess.run, timeout: int = 3 * 3600) -> int:
    try:
        return runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode
    except (OSError, subprocess.SubprocessError):
        log.exception("the scheduled cluster check could not run")
        return 2


def after_check(window_argv: list, root: Optional[Path] = None, notifier: Callable = notify.notify_desktop,
                window: Callable = notify.open_window, reader: Optional[Callable] = None) -> dict:
    """Tells the user how the check went: the notification (short verdict) and the window (the whole report)."""
    from . import doctor
    report = (reader or doctor.latest_report)(root)
    title, body = summary(report)
    return {"notified": bool(notifier(title, body)), "window": bool(window(window_argv)), "title": title, "body": body}


def loop(run_id: str, every_days: int, at: str, load_ok: bool, nodes: str, log_dir: Optional[str], clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep, runner: Callable[[list], int] = run_check, after: Callable = after_check,
         first: Optional[float] = None, max_rounds: Optional[int] = None) -> int:
    """The scheduler itself (runs in the detached child): wait, check, tell, plan the next one."""
    due = first if first is not None else first_run(clock(), at)
    window_argv = [sys.executable, "-m", "stress_test", "--doctor-last"] + (["--log-dir", log_dir] if log_dir else [])
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        background.update(run_id, scheduled_at=due, phase="waiting")
        while clock() < due:
            sleep(min(SLICE_S, max(1.0, due - clock())))
        late = clock() - due
        if late > GRACE_S:
            log.warning("the cluster check due %s was missed (%.0f s late) - skipped", time.ctime(due), late)
            due = next_after(due, every_days, clock())
            rounds += 1
            continue
        background.update(run_id, scheduled_at=None, phase="checking")
        code = runner(check_argv(load_ok, nodes, log_dir))
        log.info("the scheduled cluster check finished with code %s", code)
        after(window_argv, Path(log_dir) if log_dir else None) if after else None
        due = next_after(due, every_days, clock())
        rounds += 1
    return 0


def start(every_days: int, at: str, load_ok: bool = False, nodes: str = "", log_dir: Optional[str] = None) -> int:
    """Starts the scheduler as a detached process (replacing a running one). The caller gets 0 back and goes on (the menu does); the
    child never returns to the caller: it ends with the loop (os._exit)."""
    import secrets
    if not background.supported():
        ui.emit("❌ Running in the background is not supported on this system (no fork).")
        return 1
    if not 1 <= every_days <= MAX_DAYS:
        ui.emit(f"❌ The interval must be 1-{MAX_DAYS} days.")
        return 2
    at = normalize_at(at)
    stop_running()
    from .paths import HIDDEN_LOGS, make_private_dir
    run_id = secrets.token_hex(3)
    make_private_dir(HIDDEN_LOGS)
    console = str(HIDDEN_LOGS / f"doctor-plan-{run_id}.console.txt")
    pid = background.detach(console)
    if pid:                                                       # the caller: says what was planned and goes on
        due = first_run(time.time(), at)
        ui.emit(f"🩺 {title_of(every_days, at, load_ok)}")
        ui.emit(f"   first check: {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))} · run id {run_id} · PID {pid}")
        ui.emit(f"   shown in --scheduled and --status; ./stress.sh --stop {run_id} cancels it; it ends when this computer restarts.")
        sys.stdout.flush()
        return 0
    signal.signal(signal.SIGTERM, lambda *_a: (_ for _ in ()).throw(SystemExit(0)))
    background.register(run_id, "cluster", 0, None, console, scheduled_at=first_run(time.time(), at), title=title_of(every_days, at, load_ok),
                        extra={"kind": KIND, "every_days": every_days, "at": at, "load_ok": load_ok, "nodes": nodes, "phase": "waiting"})
    code = 0
    try:
        code = loop(run_id, every_days, at, load_ok, nodes, log_dir)
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        background.unregister(run_id)
    os._exit(code)                                              # the child must not run on into the caller's code (the menu)


def overdue_hint(plan_settings: dict, running: Optional[dict] = None) -> str:
    """For the menu: a line when the check is switched on in the settings but no scheduler runs (e.g. after a restart)."""
    if plan_settings.get("enabled") and not (running or running_plan()):
        return "the regular cluster check is on but not running (the computer restarted?)"
    return ""
