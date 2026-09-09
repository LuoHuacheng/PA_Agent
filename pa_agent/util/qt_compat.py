"""Optional QtCore compatibility layer.

GUI installs ship PyQt6 and take the real QObject/pyqtSignal. Headless
installs deliberately skip the Qt dependency; this module then falls back to a
tiny pure-Python stand-in with the same no-listener semantics, so shared
modules (EventBus, SessionTokenLedger) keep working without a single Qt import.

Only signal plumbing (connect/disconnect/emit) is emulated. GUI code paths are
never executed against the stub: they import QtWidgets and therefore require
PyQt6, which takes the real branch above.
"""

from __future__ import annotations

try:  # pragma: no cover - exercised by import machinery
    from PyQt6.QtCore import QObject, pyqtSignal

    HAVE_QT = True
except ImportError:  # Headless install without the gui extra.
    HAVE_QT = False

    class _Signal:
        """Minimal Qt signal stand-in: emit dispatches to connected slots.

        Qt signals with no connected slot are no-ops, so a stub with an empty
        slot list behaves identically for headless callers.
        """

        __slots__ = ("_slots",)

        def __init__(self, *_types: object) -> None:
            self._slots = []

        def connect(self, slot) -> None:
            if slot not in self._slots:
                self._slots.append(slot)

        def disconnect(self, slot=None) -> None:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args, **kwargs) -> None:
            for slot in list(self._slots):
                slot(*args, **kwargs)

    class QObject:
        """Qt QObject stand-in: parent tracking only (headless usage)."""

        def __init__(self, parent=None) -> None:
            self.parent = parent

    def pyqtSignal(*_types):
        """Return a signal stand-in usable as a class attribute."""
        return _Signal(*_types)


__all__ = ["HAVE_QT", "QObject", "pyqtSignal"]
