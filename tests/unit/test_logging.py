"""Regression tests for central logging configuration."""
from __future__ import annotations

import logging

from pa_agent.util.logging import ConsoleFormatter, _silence_noisy_libraries


def test_silencing_tvdatafeed_does_not_change_root_logger_level() -> None:
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        _silence_noisy_libraries()
        assert root.level == logging.INFO
    finally:
        root.setLevel(previous_level)


def test_console_formatter_is_compact_and_colorized_for_terminal() -> None:
    formatter = ConsoleFormatter(use_color=True)
    record = logging.LogRecord(
        "pa_agent.monitoring.service", logging.WARNING, __file__, 1, "fetch failed", (), None
    )

    rendered = formatter.format(record)

    assert "▲" in rendered
    assert "monitoring.service" in rendered
    assert "fetch failed" in rendered
    assert "\033[33m" in rendered


def test_console_formatter_omits_ansi_color_when_redirected() -> None:
    formatter = ConsoleFormatter(use_color=False)
    record = logging.LogRecord("pa_agent.monitoring.service", logging.INFO, __file__, 1, "ready", (), None)

    rendered = formatter.format(record)

    assert "●" in rendered
    assert "ready" in rendered
    assert "\033[" not in rendered


def test_rotating_log_retention_covers_the_cancel_review_window() -> None:
    """Cancel-reason reviews look back 12 h; one log file is worth about an hour.

    The previous 5 MB x 10 budget covered roughly ten hours, so late-evening
    events had already rotated away by morning. Keep at least 15 files.
    """
    from pa_agent.util import logging as logging_util

    retained_bytes = logging_util._MAX_LOG_BYTES * logging_util._LOG_BACKUP_COUNT
    assert logging_util._LOG_BACKUP_COUNT >= 15
    assert retained_bytes >= 75 * 1024 * 1024

