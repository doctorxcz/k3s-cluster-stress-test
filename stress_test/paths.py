"""Where things are stored.

  logs/<YYYY-MM-DD>/         visible test results for the user, one folder per day (always in the project)
  .logs/debug/<YYYY-MM-DD>/  hidden history of technical (debug) logs of every run, one folder per day
  .logs/tests/<YYYY-MM-DD>/  hidden history of pytest runs (one subdirectory per run), one folder per day

A run looks whether today's folder exists (created when it does not) and writes only into it, so a day of
testing stays in one place. `python3 -m stress_test --migrate-logs` moves older flat files into day folders.

Paths are derived from the project location, not from the folder you run the tool from.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HIDDEN_LOGS = PROJECT_ROOT / ".logs"

_HIDDEN_README = """\
Technical logs for debugging (hidden folder, you normally do not need it).

debug/   one folder per day (YYYY-MM-DD), in it one file per run of the tool: <date>_<time>_<pid>.log
         contains all called kubectl commands (with the return code and
         time), measurements, decisions and any errors with a traceback.
running/ registry of tests started in the background (one .json per running test,
         removed when it finishes)
tests/   one folder per day (YYYY-MM-DD), in it one subdirectory per pytest run: pytest.log (results and
         errors), tool-runs/ (debug logs of the tool started from the tests).

Nothing is deleted automatically - the history stays complete.
Test results for the user are in the visible folder ../logs/.
"""


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAY_FORMAT = "%Y-%m-%d"


def today() -> str:
    return time.strftime(DAY_FORMAT)


def user_log_root(override: Optional[str] = None) -> Path:
    """The folder that holds the day folders (default <project>/logs) - where results are SEARCHED.

    A relative path is taken from the project folder, an absolute one is used unchanged.
    """
    path = Path(override or "logs").expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def user_log_dir(override: Optional[str] = None, flat: bool = False) -> Path:
    """The folder a run WRITES its results into: <logs>/<today>. `flat` = exactly the given folder (no day folder)."""
    root = user_log_root(override)
    return root if flat else root / today()


def debug_log_dir() -> Path:
    """Hidden folder with debug logs: .logs/debug/<today>. STRESS_TEST_DEBUG_DIR overrides it (used as given)."""
    env = os.environ.get("STRESS_TEST_DEBUG_DIR")
    return Path(env) if env else HIDDEN_LOGS / "debug" / today()


def tests_log_dir() -> Path:
    """Hidden folder of today's pytest runs: .logs/tests/<today>."""
    return HIDDEN_LOGS / "tests" / today()


def log_root_of(path) -> Path:
    """The logs folder a result file or folder belongs to: climbs out of selftest-* and <YYYY-MM-DD> folders."""
    folder = Path(path)
    if folder.is_file() or folder.suffix:
        folder = folder.parent
    while folder.name.startswith("selftest-") or DAY_RE.match(folder.name):
        folder = folder.parent
    return folder


def result_dirs(root) -> list:
    """Folders of a logs root that can hold result files: the root itself (old flat files), every day folder
    (newest first) and the selftest-* folders inside them."""
    root = Path(root)
    found = [root]
    if root.is_dir():
        for day in sorted((d for d in root.iterdir() if d.is_dir() and DAY_RE.match(d.name)), reverse=True):
            found.append(day)
            found += sorted((d for d in day.iterdir() if d.is_dir() and d.name.startswith("selftest-")), reverse=True)
        found += sorted((d for d in root.iterdir() if d.is_dir() and d.name.startswith("selftest-")), reverse=True)
    return found


def migrate_to_daily(logs_root=None, hidden_root=None) -> dict:
    """One-off: moves old flat files into <YYYY-MM-DD> folders by the date of their last change.

    logs/<files>            -> logs/<day>/<files>        (selftest-* folders too; baselines/ stays)
    .logs/debug/<files>     -> .logs/debug/<day>/<files>
    .logs/tests/<run dirs>  -> .logs/tests/<day>/<run dirs>
    Returns counts per area. Existing targets are never overwritten."""
    logs_root = Path(logs_root) if logs_root else user_log_root()
    hidden = Path(hidden_root) if hidden_root else HIDDEN_LOGS
    counts = {"logs": 0, "debug": 0, "tests": 0}

    def move_all(folder: Path, key: str, only_dirs: bool = False) -> None:
        if not folder.is_dir():
            return
        for item in sorted(folder.iterdir()):
            if item.name.startswith(".") or item.name in ("README.txt", "README.md", "baselines"):
                continue
            if DAY_RE.match(item.name) or item.is_symlink():
                continue
            if only_dirs and not item.is_dir():
                continue
            if not only_dirs and key == "logs" and item.is_dir() and not item.name.startswith("selftest-"):
                continue
            target_dir = make_private_dir(folder / time.strftime(DAY_FORMAT, time.localtime(item.stat().st_mtime)))
            target = target_dir / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
            counts[key] += 1

    move_all(logs_root, "logs")
    move_all(hidden / "debug", "debug")
    move_all(hidden / "tests", "tests", only_dirs=True)
    return counts


def running_dir() -> Path:
    """Registry of tests running in the background (STRESS_TEST_RUNNING_DIR overrides)."""
    env = os.environ.get("STRESS_TEST_RUNNING_DIR")
    return Path(env) if env else HIDDEN_LOGS / "running"


def open_private(path, mode: str = "a", encoding: str = "utf-8"):
    """Opens a file with permissions 0600 (only the owner can read it; an existing file is not changed).

    A symlink in place of the file is refused (O_NOFOLLOW): a planted link must not redirect the write elsewhere.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if "a" in mode else os.O_TRUNC), 0o600)
    return os.fdopen(fd, mode, encoding=encoding)


def make_private_dir(path) -> Path:
    """Creates a folder with permissions 0700 (only newly created ones, existing ones are not changed)."""
    path = Path(path)
    for folder in reversed([p for p in (path, *path.parents) if not p.exists()]):    # every NEW level is private
        folder.mkdir(mode=0o700, exist_ok=True)
    # folders of the project itself (logs/, .logs/, day folders) that exist with wider rights are tightened too;
    # a folder outside the project (an own --log-dir) is never touched
    for folder in (path, *path.parents):
        if folder == PROJECT_ROOT or PROJECT_ROOT not in folder.parents:
            break
        try:
            if folder.stat().st_mode & 0o077:
                folder.chmod(0o700)
        except OSError:
            pass
    return path


def ensure_hidden_readme() -> None:
    """Creates .logs/README.txt if missing (errors are ignored)."""
    try:
        make_private_dir(HIDDEN_LOGS)
        readme = HIDDEN_LOGS / "README.txt"
        if not readme.exists():
            readme.write_text(_HIDDEN_README, encoding="utf-8")
    except OSError:
        pass
