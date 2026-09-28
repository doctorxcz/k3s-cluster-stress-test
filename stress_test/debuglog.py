"""Hidden technical (debug) log of every run -> .logs/debug/.

It must never crash the tool: if the log cannot be created, the run continues without it.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .paths import debug_log_dir, ensure_hidden_readme, make_private_dir

LOGGER_NAME = "stress_test"
FORMAT = ("%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] "
          "%(name)s:%(funcName)s:%(lineno)d  %(message)s")

_handler: Optional[logging.Handler] = None
_path: Optional[Path] = None


def setup(argv: Sequence[str]) -> Optional[Path]:
    """Opens a new debug log (one file per run). Returns its path."""
    global _handler, _path
    shutdown()
    try:
        directory = make_private_dir(debug_log_dir())
        if "STRESS_TEST_DEBUG_DIR" not in os.environ:
            ensure_hidden_readme()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = directory / f"{stamp}_{os.getpid()}.log"
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))   # file for the owner only
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    _handler, _path = handler, path

    log = logging.getLogger(__name__)
    log.info("===== START stress_test %s =====", __version__)
    log.info("Python %s | %s", sys.version.split()[0], platform.platform())
    log.info("cwd=%s", os.getcwd())
    log.info("argv=%s", list(argv))
    log.info("env KUBECTL=%r FORCE=%r", os.environ.get("KUBECTL"),
             os.environ.get("FORCE"))
    log.info("debug log: %s", path)
    return path


def current_path() -> Optional[Path]:
    return _path


def shutdown() -> None:
    """Closes the current debug log (safe to call repeatedly)."""
    global _handler, _path
    if _handler is not None:
        logging.getLogger(LOGGER_NAME).removeHandler(_handler)
        _handler.close()
    _handler = None
    _path = None
