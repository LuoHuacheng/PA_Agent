"""Headless installs skip Qt: shared modules must import and work without PyQt6.

Runs a fresh subprocess with PyQt6/pyqtgraph/sip imports blocked, then imports
the modules the headless (pa-monitor) path touches and exercises the QtCore
fallback (qt_compat) on EventBus and SessionTokenLedger.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
import sys

class _BlockQt:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"PyQt6", "pyqtgraph", "sip"}:
            raise ImportError("Qt blocked for headless test")
        return None

sys.meta_path.insert(0, _BlockQt())

# 1) qt_compat must fall back to the pure-Python stub.
from pa_agent.util.qt_compat import HAVE_QT, pyqtSignal

assert HAVE_QT is False, "expected stub fallback without PyQt6"

# 2) pa_agent.util root eagerly imports event_bus -> must not need Qt.
import pa_agent.util  # noqa: F401
from pa_agent.util.event_bus import EventBus

bus = EventBus()
received = []
bus.status.connect(lambda text: received.append(text))
bus.emit_status("hello")
assert received == ["hello"], received

# 3) session ledger (created by AppContext.bootstrap on the headless path).
from pa_agent.ai.session_ledger import SessionTokenLedger

ledger = SessionTokenLedger(context_window=1000, warn_pct=50)
thresholds = []
ledger.threshold_crossed.connect(lambda kind, _totals: thresholds.append(kind))


class _Usage:
    prompt_tokens = 400
    cached_prompt_tokens = 0
    completion_tokens = 100


ledger.add(_Usage())  # 50% -> yellow threshold
ledger.add(_Usage())  # 100% -> red threshold
assert thresholds == ["yellow", "red"], thresholds
assert ledger.breakdown()["context_pct"] == 100.0

# 4) Headless graph entry points import cleanly without any Qt package.
import importlib

for mod in ("pa_agent.monitoring.cli", "pa_agent.app_context", "pa_agent.main"):
    importlib.import_module(mod)

print("HEADLESS-NO-QT-OK")
"""


def test_headless_imports_without_qt() -> None:
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (
        result.returncode == 0
    ), f"headless no-Qt import failed:\n{result.stdout}\n{result.stderr}"
    assert "HEADLESS-NO-QT-OK" in result.stdout
