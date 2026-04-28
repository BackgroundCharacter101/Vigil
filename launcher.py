"""Visible launcher for the Vigil daemon.

WHY THIS EXISTS:

The Desktop and Start-Menu shortcuts target THIS script (run via
`python.exe`, the console-subsystem interpreter) instead of going
straight to `pythonw.exe main.py` (the GUI-subsystem one). The reason:

  On at least one tested Windows 11 install, all of:
    * the pystray "Vigil is active" tray balloon
    * the previous PowerShell-spawned UWP toast
    * `user32!MessageBoxW`
  are silently swallowed by an installed security/automation tool.
  Combined with Win11's default of hiding new tray icons in the
  overflow flyout, the user double-clicked the shortcut and observed
  ABSOLUTELY NOTHING -- the daemon was running fine, the tray icon
  was just hidden, and every notification path we tried was eaten.

A console window is the one UI affordance Windows guarantees to render
visibly: it's not a dialog, it's not a toast, it's a process attached
to a real conhost.exe. Nothing intercepts it. So this launcher pops a
console with a clear "Vigil is starting" message, spawns the real
daemon detached in the background, then exits after a short countdown.

The autostart paths (Startup folder shortcut, HKCU\\Run) still point
straight at `pythonw.exe main.py` -- THIS launcher is only used for
*user-initiated* clicks (Desktop, Start Menu), where seeing feedback
matters. Login should stay silent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import config


# Windows process-creation flags. Imported here rather than depending on
# win32con so the launcher has zero pywin32 dependency.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _pythonw_exe() -> Path:
    """Resolve pythonw.exe next to the current python.exe interpreter.

    The launcher itself runs under python.exe (the console one), but we
    spawn the real daemon under pythonw.exe (the GUI one) so it has no
    console attached. Both binaries normally sit in the same venv
    Scripts/ folder.
    """
    here = Path(sys.executable)
    candidate = here.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    # Last-ditch fallback: run under whatever launched us. The daemon
    # will then have an attached console, but it'll still work.
    return here


def _is_daemon_already_running() -> bool:
    """Cheap heuristic for "is the single-instance mutex held".

    We DON'T try to open the mutex from here -- that would race with
    the spawned daemon, and CreateMutex with the same name would just
    add another handle without telling us anything useful. Instead we
    look at process names: if any pythonw.exe with our main.py path is
    already running, we treat that as the daemon.

    Returns True only on a high-confidence match. False positives here
    are harmless (we'd just spawn a duplicate which then bounces off
    the real mutex inside main.py and writes the marker file the
    running daemon picks up). False negatives are also harmless for
    the same reason.
    """
    main_py = str(Path(__file__).with_name("main.py").resolve()).lower()
    try:
        # `wmic` is deprecated but ships on every Windows 10/11 still;
        # PowerShell would also work but is slower to spawn.
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='pythonw.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=4,
            creationflags=_CREATE_NO_WINDOW,
        )
        return main_py in out.stdout.lower()
    except Exception:
        return False


def _spawn_daemon() -> int | None:
    """Launch pythonw.exe main.py detached. Returns the spawned PID, or
    None on failure."""
    here = Path(__file__).resolve().parent
    pythonw = _pythonw_exe()
    main_py = here / "main.py"
    if not main_py.exists():
        print(f"  ERROR: main.py not found at {main_py}")
        return None
    try:
        proc = subprocess.Popen(
            [str(pythonw), str(main_py)],
            cwd=str(here),
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return proc.pid
    except OSError as e:
        print(f"  ERROR: failed to spawn daemon: {e}")
        return None


def _countdown(seconds: int) -> None:
    """Print a countdown so the user knows the window is closing on its
    own (and isn't frozen)."""
    for i in range(seconds, 0, -1):
        # \r returns to start-of-line so the countdown overwrites itself
        # rather than spamming N lines.
        print(f"  This window will close in {i}s... ", end="\r", flush=True)
        time.sleep(1)
    print(" " * 50, end="\r")  # clear the countdown line


def main() -> int:
    # Bigger, friendlier output -- the whole point of this launcher is
    # for the user to immediately SEE that their click was received.
    print()
    print("==============================================")
    print(f"  {config.APP_NAME}")
    print("==============================================")
    print()

    if _is_daemon_already_running():
        print(f"  {config.APP_NAME} is ALREADY RUNNING.")
        print()
        print("  Look for the green eye icon in your system tray.")
        print("  If you can't see it, click the ^ (up-arrow) in the")
        print("  notification area to open the overflow flyout.")
        print()
        # Still drop the marker file so the running daemon pops a tray
        # balloon -- belt and suspenders, in case the balloon DOES work
        # and just got missed by the user previously.
        try:
            config.ensure_data_dir()
            config.NOTIFY_ALREADY_RUNNING_FLAG.touch(exist_ok=True)
        except Exception:
            pass
        _countdown(5)
        return 0

    print("  Starting the daemon...")
    pid = _spawn_daemon()
    if pid is None:
        print()
        print(f"  Could not start {config.APP_NAME}. Check the log at:")
        print(f"    {config.LOG_FILE}")
        print()
        _countdown(15)  # longer so user has time to read the error
        return 1

    print(f"  Daemon spawned (PID {pid}).")
    print()
    print("  It takes ~10 seconds for the face-recognition model to load.")
    print("  Once it's ready, the green eye icon will appear in your tray.")
    print("  Click the ^ (up-arrow) in the notification area if hidden.")
    print()
    print(f"  Logs:           {config.LOG_FILE}")
    print(f"  Data folder:    {config.DATA_DIR}")
    print(f"  Pause hotkey:   {config.PAUSE_HOTKEY}")
    print()
    _countdown(8)
    return 0


if __name__ == "__main__":
    sys.exit(main())
