"""Where things are stored.

  logs/          visible test results for the user (always in the project)
  .logs/debug/   hidden history of technical (debug) logs of every run
  .logs/tests/   hidden history of pytest runs (one subdirectory per run)

Paths are derived from the project location, not from the folder you run the tool from.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HIDDEN_LOGS = PROJECT_ROOT / ".logs"

_HIDDEN_README = """\
Technical logs for debugging (hidden folder, you normally do not need it).

debug/   one file per run of the tool: <date>_<time>_<pid>.log
         contains all called kubectl commands (with the return code and
         time), measurements, decisions and any errors with a traceback.
running/ registry of tests started in the background (one .json per running test,
         removed when it finishes)
tests/   one subdirectory per pytest run: pytest.log (results and
         errors), tool-runs/ (debug logs of the tool started from the tests).

Nothing is deleted automatically - the history stays complete.
Test results for the user are in the visible folder ../logs/.
"""


def user_log_dir(override: Optional[str] = None) -> Path:
    """Visible folder with results. Default <project>/logs.

    A relative path is taken from the project folder, an absolute one is used unchanged.
    """
    path = Path(override or "logs").expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def debug_log_dir() -> Path:
    """Hidden folder with debug logs (can be overridden with the STRESS_TEST_DEBUG_DIR variable)."""
    env = os.environ.get("STRESS_TEST_DEBUG_DIR")
    return Path(env) if env else HIDDEN_LOGS / "debug"


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
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
