"""Regression tests for central logging configuration."""
from __future__ import annotations

import logging

from pa_agent.util.logging import _silence_noisy_libraries


def test_silencing_tvdatafeed_does_not_change_root_logger_level() -> None:
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        _silence_noisy_libraries()
        assert root.level == logging.INFO
    finally:
        root.setLevel(previous_level)
