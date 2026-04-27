"""Windows lock and locked-screen detection.

Two responsibilities:

1. `lock_workstation()` — actually lock the screen.
2. `is_screen_locked()` — detect whether the screen is ALREADY locked, so the
   watcher can skip the detection pipeline (and release the camera) instead
   of hammering LockWorkStation every iteration when we're already locked.

Why not just use WTSRegisterSessionNotification? Because that requires a
hidden window + message pump. OpenInputDesktop is a single ctypes call per
poll and gives us the same info.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32

# Access mask for OpenInputDesktop — DESKTOP_SWITCHDESKTOP is the minimum
# required. We're only reading metadata so we don't need more than this.
DESKTOP_SWITCHDESKTOP = 0x0100

# UOI_NAME: pass to GetUserObjectInformationW to ask for the desktop's name.
UOI_NAME = 2


def lock_workstation() -> bool:
    """Lock the Windows session. Returns True on success."""
    try:
        result = _user32.LockWorkStation()
        if result == 0:
            err = ctypes.get_last_error()
            log.error("LockWorkStation returned 0 (error code %d)", err)
            return False
        log.info("LockWorkStation call succeeded")
        return True
    except Exception:
        log.exception("LockWorkStation raised")
        return False


def is_screen_locked() -> bool:
    """Return True if the Windows session is currently locked.

    Implementation: query the current input desktop. When the session is
    unlocked and the user is active, the input desktop is "Default". When
    locked, Windows switches to the "Winlogon" secure desktop. If we can't
    even open the input desktop (access denied), we're on a secure desktop —
    treat that as locked too.
    """
    # Try to open a handle to the current input desktop. If this fails,
    # we're almost certainly on the Winlogon secure desktop → locked.
    h_desktop = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not h_desktop:
        return True

    try:
        # Query the desktop name. We pass a 256-byte buffer; desktop names
        # are short ("Default", "Winlogon", "Screen-saver").
        name_buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        ok = _user32.GetUserObjectInformationW(
            h_desktop,
            UOI_NAME,
            name_buf,
            ctypes.sizeof(name_buf),
            ctypes.byref(needed),
        )
        if not ok:
            # Can't read the name — assume worst case.
            return True
        name = name_buf.value
        # "Default" = unlocked active session. Anything else (Winlogon,
        # Screen-saver, etc.) we treat as "don't touch the camera".
        return name.lower() != "default"
    finally:
        _user32.CloseDesktop(h_desktop)


if __name__ == "__main__":
    # Smoke test: print whether the screen is currently locked. Safe to run.
    logging.basicConfig(level=logging.INFO)
    print("Screen locked:", is_screen_locked())
    print("(LockWorkStation NOT invoked -- that would actually lock you out.)")
