"""Fatal crash diagnostics: faulthandler dump file for native/Qt crashes."""
from __future__ import annotations

import faulthandler
import logging
import os
import signal
import subprocess
from pathlib import Path

from pa_agent.config.paths import CRASH_LOG_PATH, LOG_FILE_PATH, PROJECT_ROOT

logger = logging.getLogger(__name__)

_crash_file = None
_enabled = False


def enable_crash_diagnostics() -> None:
    """Write Python/native stack traces to logs/crash.log on fatal errors."""
    global _crash_file, _enabled  # noqa: PLW0603

    if _enabled:
        return

    CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _crash_file = open(CRASH_LOG_PATH, "a", encoding="utf-8")  # noqa: SIM115
    faulthandler.enable(file=_crash_file, all_threads=True)
    if hasattr(faulthandler, "register"):
        try:
            faulthandler.register(signal.SIGTERM, file=_crash_file, all_threads=True)
        except (AttributeError, OSError, ValueError):
            pass
    _enabled = True
    logger.info("faulthandler enabled → %s", CRASH_LOG_PATH)


def _log_file_handler_attached(root: logging.Logger) -> bool:
    """True when our RotatingFileHandler for pa_agent.log is on the root logger."""
    from logging.handlers import RotatingFileHandler

    expected = str(Path(LOG_FILE_PATH).resolve())
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler):
            base = getattr(handler, "baseFilename", "")
            if str(Path(base).resolve()) == expected:
                return True
    return False


def _build_identity() -> str:
    """Deployed revision: PA_BUILD_COMMIT (set by installer/CI) else local git HEAD."""
    env = os.environ.get("PA_BUILD_COMMIT", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def log_startup_diagnostics() -> None:
    """Emit one INFO line confirming logging + crash dump paths (call after configure_logging)."""
    root = logging.getLogger()
    file_ok = _log_file_handler_attached(root)
    logger.info(
        "startup diagnostics: build=%s log_file=%s handler_attached=%s crash_file=%s faulthandler=%s",
        _build_identity(),
        LOG_FILE_PATH,
        file_ok,
        CRASH_LOG_PATH,
        _enabled,
    )
    if not file_ok:
        logger.warning(
            "RotatingFileHandler for %s missing from root logger — file logging may be broken",
            LOG_FILE_PATH,
        )
