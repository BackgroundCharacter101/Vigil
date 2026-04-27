"""Entry point for the Vigil daemon.

Vigil is a webcam-aware auto-lock for Windows: it watches the laptop
camera for the enrolled owner's face and locks the workstation when the
owner leaves the frame (or when an unrecognized face appears).

Responsibilities (in order of setup):
  1. Cap the BLAS / ORT thread pools BEFORE numpy / onnxruntime import
     (otherwise a single inference burns every logical core).
  2. Configure logging to a rotating file (pythonw has no stdout/stderr).
  3. Install excepthooks so thread/main crashes don't vanish silently.
  4. Acquire a named mutex so only one daemon runs per user session.
  5. Parse CLI flags (--install-autostart / --uninstall-autostart / --foreground).
  6. Start the hotkey listener, the tray icon, and the watcher loop.
  7. Handle clean shutdown on Ctrl+C, SIGTERM, or tray Quit.
"""

from __future__ import annotations

import os

# CPU throttle: cap the BLAS/OpenMP thread pools BEFORE numpy /
# onnxruntime / opencv import. By default ORT grabs every logical core
# for each inference, which on a 24-core CPU means a single ~400ms
# RetinaFace call burns 24×400ms = 9.6 core-seconds per second of
# work. Capping at 2 threads cuts that by ~10x with no measurable
# accuracy hit on a single-face frame at det_size=320. These env vars
# MUST be set before the first numpy import or they're ignored.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")
# OpenMP idle workers default to ACTIVE (busy-spin) -- on a 20-core CPU
# that's 20 cores burning at 100% even when no inference is running.
# PASSIVE makes them sleep on a condition variable instead.
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")


def _install_ort_thread_cap() -> None:
    """Monkey-patch onnxruntime.InferenceSession to cap threads at create-time.

    InsightFace's FaceAnalysis.prepare() creates each ONNX session passing
    only `providers=` -- there is NO public hook to pass SessionOptions.
    Without this patch, ORT uses its default thread pool sized to the
    logical core count, so a single 400ms RetinaFace inference on a 24-core
    machine burns ~17 cores worth of CPU (1700% in Task Manager).

    Editing SessionOptions AFTER the session is constructed is a no-op in
    recent ORT (the session has already allocated its thread pools), which
    is why the post-hoc edit in face_engine._get_app() silently does
    nothing. Monkey-patching __init__ is the only way to inject options
    BEFORE the pools are sized.

    Idempotent: calling twice is harmless because we check for an existing
    sentinel attribute. Best-effort: any failure is swallowed so a missing
    onnxruntime install (e.g. dev machine without the model) doesn't
    break startup.
    """
    try:
        import onnxruntime as ort  # type: ignore
    except Exception:
        return
    if getattr(ort.InferenceSession, "_vigil_thread_cap_installed", False):
        return
    original_init = ort.InferenceSession.__init__

    def patched_init(self, path_or_bytes, sess_options=None, *args, **kwargs):
        if sess_options is None:
            sess_options = ort.SessionOptions()
            # intra_op=1 is single-threaded inference. At det_size=320 a
            # single ~400ms RetinaFace call costs ONE core-second instead of
            # 17. With FPS_TARGET=1 the loop sleeps ~600ms between frames,
            # so total CPU is ~(0.4s busy / 1s wall) = 2-4% of one CPU.
            # Set higher (e.g. 2) only if detection FPS drops below the
            # target and the loop becomes detection-bound.
            sess_options.intra_op_num_threads = 1
            sess_options.inter_op_num_threads = 1
            sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return original_init(self, path_or_bytes, sess_options=sess_options,
                             *args, **kwargs)

    ort.InferenceSession.__init__ = patched_init
    ort.InferenceSession._vigil_thread_cap_installed = True


# Apply the patch at module import time, BEFORE any code path can pull
# in InsightFace (which happens lazily inside watcher.py / face_engine.py).
_install_ort_thread_cap()

import argparse
import ctypes
import logging
import logging.handlers
import signal
import sys
import threading
import traceback
from ctypes import wintypes

import config

# ---------------------------------------------------------------------------
# Logging — must be set up before importing anything that might log at import.
# ---------------------------------------------------------------------------

log = logging.getLogger()  # root logger


def _setup_logging(verbose: bool) -> None:
    config.ensure_data_dir()
    log.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    # Under pythonw, sys.stdout/stderr are None. Guard before adding a
    # console handler, else writing to a None stream raises OSError.
    if sys.stdout is not None and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        log.addHandler(console)
    else:
        # Redirect stdout/stderr so any stray print() (from third-party libs)
        # doesn't crash the process.
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")


def _install_excepthooks() -> None:
    def _sys_hook(exc_type, exc, tb):
        log.error(
            "Uncaught exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    sys.excepthook = _sys_hook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        log.error(
            "Uncaught thread exception in %s:\n%s",
            args.thread.name if args.thread else "<unknown>",
            "".join(
                traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback
                )
            ),
        )

    threading.excepthook = _thread_hook


# ---------------------------------------------------------------------------
# Single-instance mutex
# ---------------------------------------------------------------------------

# Module-level handle so the mutex lives as long as the process.
_mutex_handle: int | None = None


def _acquire_single_instance() -> bool:
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]

    _mutex_handle = kernel32.CreateMutexW(None, False, config.MUTEX_NAME)
    if not _mutex_handle:
        log.error("CreateMutexW failed: %d", ctypes.get_last_error())
        return False

    ERROR_ALREADY_EXISTS = 183
    last_err = kernel32.GetLastError()
    if last_err == ERROR_ALREADY_EXISTS:
        log.warning("Another instance already running (mutex exists); exiting")
        return False

    log.info("Acquired single-instance mutex")
    return True


def _show_already_running_notification() -> None:
    """Tell the already-running daemon to show a tray balloon.

    Without this, the second instance hits the single-instance mutex
    and exits silently -- the user sees nothing happen and concludes the
    Desktop / Start Menu shortcut is broken.

    HISTORY of attempts (each broken in its own way):

      v1: user32!MessageBoxW. On the developer's machine, MessageBoxW
          returned IDOK immediately without ever creating a window --
          some installed software (security/automation tool) was silently
          intercepting modal dialogs.

      v2: shell out to PowerShell with an inline UWP toast XML literal.
          The XML's `template="ToastText02"` quotes got eaten somewhere
          in the Python f-string -> subprocess argv -> powershell.exe
          -Command pipeline, producing a malformed XML payload and an
          0xC00CE502 parse error. Failed silently inside the except.

      v3 (this one): write a marker file. The running daemon's watcher
          tick polls for it and pops a `pystray` balloon. That balloon
          path is the SAME code that fires the "Vigil is active" balloon
          at first launch -- so we know it works on this machine. No
          PowerShell, no WinRT, no quoting hell.

    Best-effort: a failure (disk full, permission denied) is logged and
    swallowed -- the user can still spot the daemon via Task Manager.
    """
    try:
        config.ensure_data_dir()
        # touch() is sufficient -- the watcher only checks for existence,
        # not contents. Use os.utime so an existing flag (race with the
        # watcher's poll) gets a fresh mtime that the user-event handler
        # uses to dedupe rapid-fire double-clicks.
        config.NOTIFY_ALREADY_RUNNING_FLAG.touch(exist_ok=True)
        log.info(
            "Wrote already-running flag at %s (running daemon will pop a balloon)",
            config.NOTIFY_ALREADY_RUNNING_FLAG,
        )
    except Exception:
        log.exception("Failed to write already-running notify flag")


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


def run_daemon() -> int:
    from watcher import Watcher
    from hotkey import HotkeyListener
    from tray import Tray

    shutdown_event = threading.Event()

    # Forward-declare tray so on_state_change can reference it.
    tray_ref: dict[str, Tray | None] = {"tray": None}

    def on_state_change(state) -> None:
        tr = tray_ref["tray"]
        if tr is not None:
            tr.on_state_change(state)

    def on_external_event(event: str) -> None:
        """Cross-process IPC handler. Currently only fires for
        "already_running" -- when a duplicate Vigil launch tried to start
        and bounced off the single-instance mutex, this is how the user
        gets visible feedback that their click WAS received."""
        tr = tray_ref["tray"]
        if tr is None:
            return
        if event == "already_running":
            log.info("Duplicate launch detected; popping 'already running' tray balloon")
            tr.notify(
                f"{config.APP_NAME} is already running",
                "It's watching from the system tray. Click the up-arrow "
                "in the notification area if you can't see the green eye icon.",
            )

    watcher = Watcher(
        on_state_change=on_state_change,
        on_external_event=on_external_event,
    )

    def request_shutdown() -> None:
        if shutdown_event.is_set():
            return
        log.info("Shutdown requested")
        shutdown_event.set()
        watcher.stop()

    hotkey = HotkeyListener(
        combo=config.PAUSE_HOTKEY,
        on_trigger=watcher.toggle_pause,
    )
    tray = Tray(watcher, on_quit=request_shutdown)
    tray_ref["tray"] = tray

    # Signals — Ctrl+C in foreground mode.
    def _sig_handler(signum, frame):
        log.info("Signal %s received", signum)
        request_shutdown()

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except (ValueError, AttributeError):
        # Not on the main thread or not supported — tray Quit still works.
        pass

    hotkey.start()
    tray.start()

    try:
        watcher.run()  # blocks until stop()
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2
    except Exception:
        log.exception("Watcher crashed")
        return 3
    finally:
        hotkey.stop()
        tray.stop()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"{config.APP_NAME} -- locks Windows when you leave the webcam frame.",
    )
    parser.add_argument(
        "--install-autostart", action="store_true",
        help="Create Startup-folder, Start-Menu, and Desktop shortcuts, then "
             "exit. Normally not needed -- the daemon self-installs on startup.",
    )
    parser.add_argument(
        "--uninstall-autostart", action="store_true",
        help="Remove the Startup, Start-Menu, and Desktop shortcuts, then "
             "exit. Note: the next `main.py` launch will re-install them "
             "unless you also pass --no-autoinstall.",
    )
    parser.add_argument(
        "--autostart-status", action="store_true",
        help="Print whether each shortcut is installed, then exit.",
    )
    parser.add_argument(
        "--foreground", action="store_true",
        help="Run the daemon in the current console (for development/debugging).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log at DEBUG level.",
    )
    parser.add_argument(
        "--no-autoinstall", action="store_true",
        help="Skip the automatic 'ensure autostart shortcut exists' step on "
             "startup. For development / running from a transient checkout.",
    )
    args = parser.parse_args(argv)

    _setup_logging(verbose=args.verbose)
    _install_excepthooks()
    log.info("=" * 60)
    log.info("%s starting (pid=%d, python=%s)",
             config.APP_NAME, os.getpid(), sys.version.split()[0])

    # One-shot admin commands.
    if args.install_autostart:
        import autostart
        startup_path, menu_path, desktop_path, run_key_cmd = autostart.install()
        print("Installed:")
        print(f"  Startup:    {startup_path}")
        print(f"  Start Menu: {menu_path}")
        print(f"  Desktop:    {desktop_path}")
        print("  Run key:    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Vigil")
        print(f"              = {run_key_cmd}")
        print()
        print("To pin to the taskbar or Start tiles: right-click the Desktop")
        print("shortcut and choose 'Pin to taskbar' or 'Pin to Start'.")
        return 0
    if args.uninstall_autostart:
        import autostart
        removed = autostart.uninstall()
        print("Removed." if removed else "Not installed -- nothing to do.")
        return 0
    if args.autostart_status:
        import autostart
        # Report each shortcut individually; is_installed() only tells us
        # whether ALL are present, which hides partial-install states.
        startup_ok = autostart._startup_shortcut_path().exists()
        menu_ok = autostart._start_menu_shortcut_path().exists()
        desktop_ok = autostart._desktop_shortcut_path().exists()
        run_key_value = autostart._run_key_value()
        print(f"Startup shortcut:    {'yes' if startup_ok else 'NO'}")
        print(f"Start Menu shortcut: {'yes' if menu_ok else 'NO'}")
        print(f"Desktop shortcut:    {'yes' if desktop_ok else 'NO'}")
        print(f"Run key:             {'yes' if run_key_value else 'NO'}")
        if run_key_value:
            print(f"  = {run_key_value}")
        return 0

    # Daemon mode (default).
    if not _acquire_single_instance():
        # Most common reason a second invocation lands here: the user
        # double-clicked the Desktop / Start Menu shortcut while the
        # daemon was already running. Show a toast so they can see the
        # click was received but the daemon is already alive in tray.
        _show_already_running_notification()
        return 1

    # Self-install the autostart + Start Menu shortcuts on first launch
    # (and after any accidental removal). Fully idempotent: does nothing
    # if both are already present. Failures are logged and swallowed so
    # a flaky COM call can't prevent the daemon from running.
    if not args.no_autoinstall:
        import autostart
        autostart.ensure_installed()

    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
