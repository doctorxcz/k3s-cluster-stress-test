"""Running a test in the background (detached process) and managing running tests.

How it works: after all options are entered the process forks. The parent prints a
message and exits, the child detaches from the terminal (new session, output to a file) and
runs the whole test. Every running test has a record in .logs/running/<id>.json,
which --status and --stop use. Requires a system with fork (Linux, macOS).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .parsing import format_duration
from .paths import make_private_dir, open_private, running_dir

log = logging.getLogger(__name__)


def supported() -> bool:
    return hasattr(os, "fork") and hasattr(os, "setsid")


def detach(console_path: str) -> int:
    """Forks the process. The parent gets the child's PID (>0), the child gets 0
    and carries on detached from the terminal (output goes to console_path)."""
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid > 0:
        return pid
    os.setsid()                                     # without a controlling terminal
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    fd = os.open(console_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    for stream in (sys.stdout, sys.stderr):        # so that `tail -f` can follow it live
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:
            pass
    return 0


# --- registry of running tests ------------------------------------------------------

def _record_path(run_id: str) -> Path:
    return running_dir() / f"{run_id}.json"


def register(run_id: str, node: str, duration: int, log_path: Optional[str],
             console_path: str) -> None:
    """Writes a record about a running test (called from the detached process)."""
    try:
        make_private_dir(running_dir())
        with open_private(_record_path(run_id), "w") as fh:
            fh.write(json.dumps({
                "run_id": run_id, "pid": os.getpid(), "node": node,
                "duration": duration, "started": time.time(),
                "log": log_path, "console": console_path,
            }))
    except OSError:
        log.warning("Could not write the record of the running test", exc_info=True)


def unregister(run_id: str) -> None:
    try:
        _record_path(run_id).unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_running() -> list[dict]:
    """Tests running in the background; records of dead processes are cleaned up."""
    directory = running_dir()
    if not directory.exists():
        return []
    running = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _alive(int(data.get("pid", 0))):
            running.append(data)
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return running


def format_running(records: list[dict], now: Optional[float] = None) -> str:
    """Overview of tests running in the background (for --status)."""
    if not records:
        return "No background test is running."
    now = time.time() if now is None else now
    lines = ["Background tests:"]
    for r in records:
        elapsed = max(int(now - r["started"]), 0)
        lines.append(
            f"  {r['run_id']}  node {r['node']}  PID {r['pid']}  "
            f"running {format_duration(elapsed)} of {format_duration(r['duration'])} "
            f"(+ preparation)")
        if r.get("log"):
            lines.append(f"      results: {r['log']}")
        lines.append(f"      output:  {r['console']}")
    return "\n".join(lines)


def stop(target: str, wait_s: float = 30.0) -> tuple[bool, str]:
    """Stops a background test (SIGTERM = graceful shutdown with pod cleanup).

    target = run id, node name or PID.
    """
    matches = [r for r in list_running()
               if target in (r["run_id"], r["node"], str(r["pid"]))]
    if not matches:
        return False, f"No running background test matches '{target}'."
    if len(matches) > 1:
        ids = ", ".join(r["run_id"] for r in matches)
        return False, f"'{target}' matches more than one test ({ids}), specify the run id."
    record = matches[0]
    pid = int(record["pid"])
    log.info("Stopping test %s (PID %s)", record["run_id"], pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "The test was no longer running."
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not _alive(pid):
            return True, (f"Test {record['run_id']} (node {record['node']}) stopped, "
                          f"pods are deleted.")
        time.sleep(0.3)
    return False, (f"Test {record['run_id']} is still waiting to finish (deleting pods). "
                   f"Check again in a moment: --status")
