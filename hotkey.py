"""Global hotkey listener using pynput.

We use pynput rather than `keyboard` because `keyboard` requires admin
privileges for global hooks on Windows, and we want the daemon to run as a
normal user.

Gotchas we have to work around:
  * The GlobalHotKeys listener runs in a background thread. Under pythonw.exe
    (no console) the thread's exceptions vanish silently — we wrap the
    callback in try/except with logging.
  * The listener must be held by reference (don't let it get garbage
    collected) and should NOT be a daemon thread, or pythonw may tear it down
    at unexpected moments.
"""

from __future__ import annotations

import logging
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)


class HotkeyListener:
    """Wraps pynput.keyboard.GlobalHotKeys with logging + safe lifecycle."""

    def __init__(self, combo: str, on_trigger: Callable[[], None]) -> None:
        self._combo = combo
        self._on_trigger = on_trigger
        self._listener: keyboard.GlobalHotKeys | None = None

    def _safe_callback(self) -> None:
        log.info("Hotkey pressed: %s", self._combo)
        try:
            self._on_trigger()
        except Exception:
            log.exception("Hotkey callback raised")

    def start(self) -> None:
        if self._listener is not None:
            return
        try:
            self._listener = keyboard.GlobalHotKeys(
                {self._combo: self._safe_callback}
            )
            # Not a daemon — we want it to keep the interpreter alive if
            # main.py's main thread somehow exits early. main.py calls
            # stop() during shutdown.
            self._listener.daemon = False
            self._listener.start()
            log.info("Hotkey listener started for %s", self._combo)
        except Exception:
            log.exception("Failed to start hotkey listener for %s", self._combo)

    def stop(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
            log.info("Hotkey listener stopped")
        except Exception:
            log.exception("Error stopping hotkey listener")
        self._listener = None
