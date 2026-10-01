"""History of pytest runs into the hidden folder .logs/tests/.

Every pytest run gets its own subdirectory inside the folder of the day (.logs/tests/YYYY-MM-DD/date_time_pid):
  pytest.log             results of individual tests, errors with a traceback, summary
  inprocess-debug.log    debug log of the library from tests that call the code directly
  tool-runs/             debug logs of the tool started from integration tests
                         (one file per run)

Nothing is deleted, the history stays complete. The folder is hidden but always available
for debugging (what happened during the tests and why something failed).
"""
import logging
import os
import platform
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_state = {}


def pytest_configure(config):
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = ROOT / ".logs" / "tests" / time.strftime("%Y-%m-%d") / f"{stamp}_{os.getpid()}"
    (session_dir / "tool-runs").mkdir(parents=True, exist_ok=True)

    # debug logs of the tool started from the tests go here, not to .logs/debug
    os.environ["STRESS_TEST_DEBUG_DIR"] = str(session_dir / "tool-runs")

    report = open(session_dir / "pytest.log", "w", encoding="utf-8")
    report.write(f"pytest run {stamp}\n")
    report.write(f"Python {sys.version.split()[0]} | {platform.platform()}\n")
    report.write(f"args: {list(config.invocation_params.args)}\n")
    report.write("=" * 70 + "\n")
    report.flush()

    handler = logging.FileHandler(session_dir / "inprocess-debug.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] "
        "%(name)s:%(funcName)s:%(lineno)d  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger("stress_test")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    _state.update(dir=session_dir, report=report, handler=handler,
                  counts=Counter(), started=time.monotonic())


def pytest_runtest_logreport(report):
    st = _state
    if "report" not in st:
        return
    relevant = (report.when == "call"
                or (report.when == "setup" and report.outcome != "passed")
                or (report.when == "teardown" and report.failed))
    if not relevant:
        return
    outcome = report.outcome.upper()
    st["counts"][outcome] += 1
    st["report"].write(f"{outcome:8} {report.nodeid}  ({report.duration:.2f}s)\n")
    if report.failed:
        st["report"].write("-" * 70 + "\n" + report.longreprtext + "\n" + "-" * 70 + "\n")
    st["report"].flush()


def pytest_sessionfinish(session, exitstatus):
    st = _state
    if "report" not in st:
        return
    counts = ", ".join(f"{k.lower()}: {v}" for k, v in sorted(st["counts"].items()))
    st["report"].write("=" * 70 + "\n")
    st["report"].write(f"SUMMARY: {counts or 'no tests'} | exit status {exitstatus} "
                       f"| {time.monotonic() - st['started']:.1f} s\n")
    st["report"].close()
    logging.getLogger("stress_test").removeHandler(st["handler"])
    st["handler"].close()
