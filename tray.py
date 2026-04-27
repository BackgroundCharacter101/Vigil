"""System tray icon.

Under pythonw.exe there's no console, so the tray icon is the ONLY
indication to the user that the daemon is alive. Three menu items
(Pause/Resume, Re-enroll, Quit) and a colored icon reflecting state:

    green  = WATCHING
    yellow = PAUSED or CAMERA_UNAVAILABLE
    red    = error / stopped
    gray   = STARTING or LOCKED_SCREEN

pystray runs its own event loop — we run it in a background (daemon) thread
started from main.py, so the watcher's main loop isn't blocked.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw
import pystray

import config
from watcher import State

log = logging.getLogger(__name__)


_COLOR_BY_STATE: dict[State, tuple[int, int, int]] = {
    State.STARTING: (128, 128, 128),
    State.WATCHING: (0, 180, 0),
    State.PAUSED: (230, 180, 0),
    State.CAMERA_UNAVAILABLE: (230, 180, 0),
    State.LOCKED_SCREEN: (80, 80, 80),
    State.STOPPED: (200, 0, 0),
}


def _make_icon_image(rgb: tuple[int, int, int]) -> Image.Image:
    """Generate a 64x64 filled circle icon at runtime so we don't ship assets."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=rgb + (255,), outline=(0, 0, 0, 255))
    return img


class Tray:
    def __init__(
        self,
        watcher,
        on_quit: Callable[[], None],
    ) -> None:
        self._watcher = watcher
        self._on_quit = on_quit
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None
        # Track a re-enroll subprocess so a double-click doesn't spawn two
        # enrollers fighting for the camera.
        self._enroll_proc: subprocess.Popen | None = None
        # Pop a "now active" tray balloon exactly ONCE -- on the first
        # transition into WATCHING. Critical for the Desktop-shortcut UX:
        # under pythonw there is no console window, so without a balloon
        # the user double-clicks the icon and sees absolutely nothing
        # happen for the ~10 seconds it takes InsightFace to load. The
        # balloon also surfaces the tray icon location (Win11 buries
        # icons in the overflow flyout by default).
        self._announced_ready: bool = False

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._icon = pystray.Icon(
            "vigil",
            icon=_make_icon_image(_COLOR_BY_STATE[State.STARTING]),
            title=f"{config.APP_NAME} — starting",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._run_icon, daemon=True)
        self._thread.start()
        log.info("Tray icon started")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.exception("Error stopping tray icon")
            self._icon = None

    def _run_icon(self) -> None:
        try:
            assert self._icon is not None
            self._icon.run()
        except Exception:
            log.exception("Tray icon loop crashed")

    # ---- updates ----------------------------------------------------------

    def on_state_change(self, state: State) -> None:
        if self._icon is None:
            return
        color = _COLOR_BY_STATE.get(state, (128, 128, 128))
        try:
            self._icon.icon = _make_icon_image(color)
            self._icon.title = f"{config.APP_NAME} — {state.value.lower()}"
            # Rebuild the menu so Pause/Resume reflects the current state.
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception:
            log.exception("Error updating tray icon")

        # First-time WATCHING -> pop a balloon so the user knows the
        # daemon actually launched and where the tray icon lives.
        # Suppressed on subsequent WATCHING transitions (e.g. recovering
        # from CAMERA_UNAVAILABLE) so we don't spam the user.
        if state == State.WATCHING and not self._announced_ready:
            self._announced_ready = True
            self._notify(
                f"{config.APP_NAME} is active",
                "Watching for your face. The PC will lock when you "
                "leave the frame. Right-click the tray icon for "
                "Pause / Re-enroll / Quit.",
            )

    def notify(self, title: str, message: str) -> None:
        """Show a tray balloon notification. Best-effort: pystray's
        notify() implementation varies by backend, so swallow failures.

        Public API: called from main.on_external_event() when a duplicate
        Vigil launch needs to surface "I'm already running" feedback to
        the user. Safe to call from any thread; pystray's notify dispatches
        to its own message-pump thread internally.
        """
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            log.exception("Tray notify failed")

    # Pre-rename alias kept so the on_state_change first-launch balloon
    # can keep calling the old name without churn. New callers should use
    # the public notify() above.
    _notify = notify

    # ---- menu -------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        """Build the right-click context menu.

        Intentionally NO `default=True` on any item. pystray treats the
        default item as the action invoked on LEFT-click of the icon,
        and having Pause/Resume be that action meant every time the user
        left-clicked the icon to open the menu they silently toggled the
        watcher. Better to require a right-click every time — it's only
        one extra click and it eliminates a huge footgun.
        """
        paused = self._watcher.is_paused
        return pystray.Menu(
            pystray.MenuItem(
                "Resume" if paused else "Pause",
                self._handle_toggle_pause,
            ),
            pystray.MenuItem("Re-enroll face", self._handle_reenroll),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._handle_quit),
        )

    # ---- menu handlers ----------------------------------------------------

    def _handle_toggle_pause(self, *_args) -> None:
        try:
            self._watcher.toggle_pause()
        except Exception:
            log.exception("Tray toggle-pause raised")

    def _handle_reenroll(self, *_args) -> None:
        """Spawn enroll.py in a new console window. The watcher keeps
        running; the new encoding is picked up only after a daemon
        restart, so the log reminds the user.

        IMPORTANT: when the daemon is running under pythonw.exe (the
        normal autostart case), sys.executable is pythonw.exe -- a
        GUI-subsystem binary that does NOT attach to a console even if
        we pass CREATE_NEW_CONSOLE. Result: enroll's prompts vanish and
        the preview window shows up with no instructions. Swap to
        python.exe for the subprocess so the prompts are visible.

        Also guards against double-click spamming: if a previous enroll
        subprocess is still alive, skip and log. (Two simultaneous
        enrollers would fight for the camera.)
        """
        CREATE_NEW_CONSOLE = 0x00000010
        if self._enroll_proc is not None and self._enroll_proc.poll() is None:
            log.info("Re-enroll already in progress; ignoring extra click")
            return
        try:
            enroll_script = Path(__file__).with_name("enroll.py")
            py_console = self._python_console_exe()
            self._enroll_proc = subprocess.Popen(
                [str(py_console), str(enroll_script)],
                creationflags=CREATE_NEW_CONSOLE,
                close_fds=True,
            )
            log.info(
                "Launched enroll.py in a new console using %s. "
                "Restart the daemon after enrollment to pick up the new "
                "encoding.",
                py_console,
            )
        except Exception:
            log.exception("Failed to launch enroll.py from tray")

    @staticmethod
    def _python_console_exe() -> Path:
        """Return the console-subsystem Python binary (python.exe) that
        sits next to sys.executable. Falls back to sys.executable if
        python.exe isn't there (very unusual custom layouts)."""
        here = Path(sys.executable)
        candidate = here.with_name("python.exe")
        if candidate.exists():
            return candidate
        return here

    def _handle_quit(self, *_args) -> None:
        log.info("Quit requested from tray")
        try:
            self._on_quit()
        except Exception:
            log.exception("Tray quit handler raised")
