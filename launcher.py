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
    """Launch pythonw.exe main.py fully detached. Returns the spawned
    PID, or None on failure.

    Why the explicit DEVNULL fds + DETACHED + NEW_PROCESS_GROUP combo:

    When the user double-clicks the .lnk via Explorer, conhost.exe
    attaches a real console window to launcher.py. If we spawn
    pythonw.exe with the default fd inheritance, it picks up handles to
    that console -- and when launcher.py exits a few seconds later,
    Windows tears the console down, taking those inherited handles with
    it. The daemon's first attempt to write to stdout/stderr (or any
    code path that touches a stdio handle) then throws and the daemon
    dies silently. The log shows a startup line, then nothing.

    Passing DEVNULL for all three standard streams means pythonw inherits
    no handles from us, so console teardown doesn't matter.

    DETACHED_PROCESS keeps pythonw out of the parent's console. NEW
    PROCESS_GROUP isolates it from any Ctrl+C signals delivered to the
    launcher's group.
    """
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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid
    except OSError as e:
        print(f"  ERROR: failed to spawn daemon: {e}")
        return None


def _wait_for_daemon_log(after_ts: float, timeout: float = 14.0) -> bool:
    """Poll the log file for a "Vigil starting" line written after `after_ts`.

    Returns True the moment we see one, False if `timeout` seconds elapse
    with nothing matching. We use this to confirm the spawned pythonw.exe
    actually got far enough to set up logging, NOT just that
    subprocess.Popen returned a PID (a process can die between exec and
    the first log line, which is exactly the failure mode the previous
    launcher version had on user-clicks).
    """
    log_path = config.LOG_FILE
    deadline = time.time() + timeout
    needle_marker = "Vigil starting"
    last_size = 0
    while time.time() < deadline:
        try:
            if log_path.exists():
                # Only re-read tail if the file grew; cheap stat call.
                size = log_path.stat().st_size
                if size > last_size:
                    last_size = size
                    # Read just the tail -- the daemon writes maybe 1-2KB
                    # of startup banner before the InsightFace load, so
                    # 8KB is more than enough.
                    with open(log_path, "rb") as f:
                        f.seek(max(0, size - 8192))
                        tail = f.read().decode("utf-8", errors="replace")
                    for line in tail.splitlines():
                        if needle_marker not in line:
                            continue
                        # Log lines start with "YYYY-MM-DD HH:MM:SS  ".
                        try:
                            ts_str = line[:19]
                            ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
                        except (ValueError, IndexError):
                            continue
                        if ts >= after_ts - 1:  # 1s slop for clock granularity
                            return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


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
    spawn_started_at = time.time()
    pid = _spawn_daemon()
    if pid is None:
        print()
        print(f"  Could not start {config.APP_NAME}. Check the log at:")
        print(f"    {config.LOG_FILE}")
        print()
        _countdown(15)  # longer so user has time to read the error
        return 1

    print(f"  Daemon spawned (PID {pid}).")
    print(f"  Waiting for it to come up (~10s for the face model to load)...")
    print()

    # Crucial: don't just trust subprocess.Popen returning a PID -- the
    # spawned process can die between exec and its first log line if any
    # inherited handle / env var was bad. Poll the log for a fresh
    # "Vigil starting" line so we know the daemon actually got past
    # logging setup. If it doesn't appear within the timeout we tell
    # the user clearly so they know to look at the log.
    came_up = _wait_for_daemon_log(after_ts=spawn_started_at, timeout=14.0)

    if not came_up:
        print("  WARNING: didn't see a startup line in the log within 14s.")
        print(f"  The daemon may have died on launch. Check:")
        print(f"    {config.LOG_FILE}")
        print()
        print("  This is unusual. If you keep seeing this, run from a")
        print("  console for full error output:")
        print()
        print(f"    cd {Path(__file__).resolve().parent}")
        print("    .venv\\Scripts\\python.exe main.py --foreground --verbose")
        print()
        _countdown(15)
        return 2

    print("  OK -- daemon is up and running.")
    print()
    print("  Look for the green eye icon in your system tray.")
    print("  (Click the ^ up-arrow in the notification area if hidden.)")
    print()
    print(f"  Logs:           {config.LOG_FILE}")
    print(f"  Data folder:    {config.DATA_DIR}")
    print(f"  Pause hotkey:   {config.PAUSE_HOTKEY}")
    print()
    _countdown(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
