"""Scheduling of tests: start times, plans, and the two ways a plan is kept.

A) default: a detached process that waits for the start time (it is registered in the background registry as
   "waiting"; closing the terminal does not matter, a restart of the computer does).
C) optional (`--persistent`): a systemd USER timer with `Persistent=true` (survives a restart; a missed start is caught up at
   the next boot; running while you are logged out needs `loginctl enable-linger`).
Both kinds are listed and cancelled in the same way (`--scheduled`, `--stop ID`, menu 9 SCHEDULE).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import background
from .parsing import format_duration, parse_duration
from .paths import HIDDEN_LOGS, PROJECT_ROOT, make_private_dir, open_private

UNIT_PREFIX = "stress-test-"
_DATE_FORMS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S")
_SHORT_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?\s+(\d{1,2}):(\d{2})$")      # 3.10. 02:00 (this year, next if passed)


def scheduled_dir() -> Path:
    """Plans kept as systemd timers (STRESS_TEST_SCHEDULED_DIR overrides)."""
    env = os.environ.get("STRESS_TEST_SCHEDULED_DIR")
    return Path(env) if env else HIDDEN_LOGS / "scheduled"


def unit_dir() -> Path:
    env = os.environ.get("STRESS_TEST_SYSTEMD_DIR")
    return Path(env) if env else Path.home() / ".config" / "systemd" / "user"


# --- start times --------------------------------------------------------------------------------
def parse_start(text: str, now: Optional[float] = None) -> float:
    """Epoch of the start: 'HH:MM' (the nearest, today or tomorrow), a duration from now (30m, 2h), 'YYYY-MM-DD HH:MM',
    'DD.MM.YYYY HH:MM' or 'DD.MM. HH:MM' (a date must be in the future). ValueError with a readable message otherwise."""
    text = (text or "").strip()
    now = time.time() if now is None else now
    for form in _DATE_FORMS:
        try:
            when = datetime.strptime(text, form).timestamp()
        except ValueError:
            continue
        if when <= now:
            raise ValueError("That date and time is already in the past.")
        return when
    short = _SHORT_DATE.match(text)
    if short:
        day, month, hour, minute = (int(g) for g in short.groups())
        year = time.localtime(now).tm_year
        for y in (year, year + 1):
            try:
                when = datetime(y, month, day, hour, minute).timestamp()
            except ValueError:
                raise ValueError("Invalid date.")
            if when > now:
                return when
        raise ValueError("Invalid date.")
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hh, mm = (int(p) for p in text.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("Invalid time, use HH:MM (0-23:0-59).")
        t = time.localtime(now)
        target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
        return target + 86400 if target <= now else target
    try:
        return now + parse_duration(text)
    except ValueError:
        raise ValueError("Enter a time as HH:MM (22:30), a duration from now (30m, 2h) or a date and time "
                         "(2026-10-03 02:00, 3.10. 02:00).")


def describe_when(epoch: float, now: Optional[float] = None) -> str:
    """'Sat 3.10. 02:00 (in 8 h 12 min)'."""
    now = time.time() if now is None else now
    t = time.localtime(epoch)
    left = int(epoch - now)
    span = format_duration(left) if left > 0 else "now"
    return f"{time.strftime('%a', t)} {t.tm_mday}.{t.tm_mon}. {time.strftime('%H:%M', t)} (in {span})" if left > 0 \
        else f"{time.strftime('%a %d.%m. %H:%M', t)} (due)"


# --- the waiting process (A) ------------------------------------------------------------------------
def wait_registered(start_at: Optional[float], run_id: str, node: str, duration: int, log_path: Optional[str],
                    console_path: str, title: str = "", sleep=time.sleep, extra: Optional[dict] = None) -> None:
    """Child of a background test: registers a 'waiting' record (visible in --status, the planner and stoppable with
    --stop), waits for the start time, then registers the running test as usual."""
    if start_at and start_at > time.time():
        background.register(run_id, node, duration, log_path, console_path, scheduled_at=start_at, title=title, extra=extra)
        sleep(max(0.0, start_at - time.time()))
    background.register(run_id, node, duration, log_path, console_path, title=title, extra=extra)


# --- systemd timers (C) -----------------------------------------------------------------------------
def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def linger_enabled() -> Optional[bool]:
    """Does the user session keep running while logged out (needed for a timer to fire then)? None = unknown."""
    if not shutil.which("loginctl"):
        return None
    try:
        out = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def _quote(arg: str) -> str:
    """One word of a systemd ExecStart= line."""
    if re.fullmatch(r"[A-Za-z0-9_@%:./=+,-]+", arg) and "%" not in arg:
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_texts(plan_id: str, argv: list, start_at: float, title: str, project: Path, python: str) -> tuple[str, str]:
    """(service, timer) unit files of a plan. The service runs the test without questions; ExecStopPost removes the plan."""
    env = [f"PYTHONPATH={project}"] + [f"{k}={os.environ[k]}" for k in ("KUBECONFIG", "KUBECTL") if os.environ.get(k)]
    base = f"{python} -m stress_test"
    service = ("[Unit]\n"
               f"Description=Planned cluster stress test: {title.replace(chr(10), ' ')}\n\n"
               "[Service]\nType=oneshot\n"
               f"WorkingDirectory={project}\n"
               + "".join(f"Environment={_quote(e)}\n" for e in env)
               + f"ExecStart={base} {' '.join(_quote(a) for a in argv)}\n"
               f"ExecStopPost=-{base} --cancel-plan {plan_id}\n")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_at))
    timer = ("[Unit]\n"
             f"Description=Start of the planned stress test {plan_id}\n\n"
             "[Timer]\n"
             f"OnCalendar={when}\nPersistent=true\nAccuracySec=1s\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def install_timer(plan_id: str, argv: list, start_at: float, title: str, nodes: str = "") -> dict:
    """Writes the units, enables the timer and a plan record; returns the record. OSError / RuntimeError on failure."""
    service, timer = unit_texts(plan_id, argv, start_at, title, PROJECT_ROOT, sys.executable)
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{UNIT_PREFIX}{plan_id}.service").write_text(service, encoding="utf-8")
    (directory / f"{UNIT_PREFIX}{plan_id}.timer").write_text(timer, encoding="utf-8")
    for step in (("daemon-reload",), ("enable", "--now", f"{UNIT_PREFIX}{plan_id}.timer")):
        done = _systemctl(*step)
        if done.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(step)} failed: {done.stderr.strip() or done.stdout.strip()}")
    record = {"id": plan_id, "kind": "systemd", "start_at": start_at, "title": title, "nodes": nodes,
              "unit": f"{UNIT_PREFIX}{plan_id}", "created": time.time()}
    make_private_dir(scheduled_dir())
    with open_private(scheduled_dir() / f"{plan_id}.json", "w") as fh:
        fh.write(json.dumps(record))
    return record


def _timer_state(plan: dict) -> str:
    """'waiting' | 'running' | 'finished' of a systemd plan."""
    unit = plan["unit"]
    try:
        service = _systemctl("show", f"{unit}.service", "-p", "ActiveState", "--value").stdout.strip()
        timer = _systemctl("show", f"{unit}.timer", "-p", "ActiveState", "--value").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "waiting"
    if service in ("active", "activating"):
        return "running"
    # a calendar timer with one fixed date stays "active (elapsed)" after it fired: judge by the time instead (2 min of grace
    # for a missed start that systemd catches up at the next boot)
    if time.time() >= plan.get("start_at", 0) + 120:
        return "finished"
    return "waiting" if timer == "active" or time.time() < plan.get("start_at", 0) else "waiting"


def _remove_systemd(plan: dict) -> None:
    unit = plan["unit"]
    for suffix in ("timer", "service"):
        try:
            _systemctl("disable", "--now", f"{unit}.{suffix}")
        except (OSError, subprocess.SubprocessError):
            pass
    for suffix in ("timer", "service"):
        try:
            (unit_dir() / f"{unit}.{suffix}").unlink()
        except OSError:
            pass
    try:
        _systemctl("daemon-reload")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        (scheduled_dir() / f"{plan['id']}.json").unlink()
    except OSError:
        pass


# --- listing and cancelling (both kinds) ----------------------------------------------------------
def _systemd_plans() -> list[dict]:
    directory = scheduled_dir()
    plans = []
    if not directory.exists():
        return plans
    for path in sorted(directory.glob("*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plans.append(plan)
    return plans


def list_plans(now: Optional[float] = None) -> list[dict]:
    """Planned tests, soonest first: {id, kind: process|systemd, title, nodes, start_at, state: waiting|running}.
    Finished systemd plans are cleaned up on the way."""
    now = time.time() if now is None else now
    plans = []
    for r in background.list_running():
        if r.get("scheduled_at") and r["scheduled_at"] > now:
            plans.append({"id": r["run_id"], "kind": "process", "title": r.get("title") or r.get("node", "?"),
                          "nodes": r.get("node", ""), "start_at": r["scheduled_at"], "state": "waiting"})
    for plan in _systemd_plans():
        state = _timer_state(plan)
        if state == "finished":
            _remove_systemd(plan)
            continue
        plans.append({"id": plan["id"], "kind": "systemd", "title": plan.get("title", ""), "nodes": plan.get("nodes", ""),
                      "start_at": plan["start_at"], "state": state})
    return sorted(plans, key=lambda p: p["start_at"])


def cancel(plan_id: str) -> tuple[bool, str]:
    """Cancels a plan of either kind (a running systemd one is stopped, its pods are deleted by the test itself)."""
    for plan in _systemd_plans():
        if plan.get("id") == plan_id:
            _remove_systemd(plan)
            return True, f"Plan {plan_id} cancelled (timer removed)."
    return background.stop(plan_id)


def format_plans(plans: list, now: Optional[float] = None) -> list[str]:
    """Plain lines for --scheduled and the log."""
    now = time.time() if now is None else now
    if not plans:
        return ["Nothing is scheduled."]
    out = ["Scheduled tests:"]
    for p in plans:
        how = "systemd timer (survives a restart)" if p["kind"] == "systemd" else "waiting process"
        out.append(f"  {p['id']}  {describe_when(p['start_at'], now)}  {p['title']}  [{how}"
                   + (", running now" if p["state"] == "running" else "") + "]")
    return out
