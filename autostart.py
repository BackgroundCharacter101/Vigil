"""Autostart + Start Menu shortcut management for Vigil.

Creates THREE shortcuts plus ONE registry entry for the daemon:

1. Startup folder   (%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\)
   -> Windows launches this automatically at login. Shows up in Task
      Manager's Startup tab so the user can disable without tooling.

2. Start Menu folder (%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\)
   -> Appears in Start search and the Start Menu list, so the user can
      launch it manually (e.g. after a tray Quit).

3. Desktop folder (or OneDrive Desktop)
   -> One-click launch + right-click "Pin to taskbar / Pin to Start".

4. HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Vigil
   -> REDUNDANT autostart channel. The Startup folder is supposed to fire
      every login, but in practice on Windows 11 it sometimes silently
      fails (the .lnk is present, the shell enumerates it, but it's never
      launched -- usually because of an interaction with the StartupApproved
      registry cache or other shell-init hiccups). The HKCU\\Run key is the
      mechanism Steam, Notion, Teams, Spotify etc. use, and it fires
      reliably on every login. Having BOTH means we still launch even if
      one path is sabotaged. The mutex prevents the duplicate run.

All three shortcuts target `pythonw.exe` with `main.py` as the argument and
the repo folder as the working directory. The Run key value is the same
command line. None of them need admin rights.

The shortcut icon is an .ico generated once at `%LOCALAPPDATA%\\Vigil\\icon.ico`,
so the Start Menu / Startup entries don't inherit pythonw.exe's generic
Python icon. The .ico contains multiple sizes (16..256 px) so Windows
picks the right one for the search results, taskbar, etc.

`ensure_installed()` is the entry point for the daemon startup path: it's
idempotent and only creates shortcuts/entries that don't already exist.
On every call it ALSO removes any stale "WebcamAutoLock.lnk" / Run-key
entries from the pre-rename era so an upgrading user doesn't end up with
two identical entries in Task Manager's Startup tab.

If the user explicitly ran `--uninstall-autostart`, the next `main.py`
launch will re-install everything -- by design, matching the "always run
at startup" expectation. The off switch is Task Manager -> Startup ->
Disable (which disables BOTH the .lnk and the Run-key entry, since both
appear in that tab).
"""

from __future__ import annotations

import logging
import os
import sys
import winreg
from pathlib import Path

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HKCU\Run registry key (redundant autostart channel)
# ---------------------------------------------------------------------------

# Subkey under HKCU. No admin needed.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Value name. Picked to match the .lnk basename so it's obvious which
# program owns the entry when the user inspects Task Manager / regedit.
_RUN_VALUE_NAME = "Vigil"
# Pre-rename Run-key value name. Cleaned up on every ensure_installed()
# so an upgrading user doesn't end up with two startup entries.
_LEGACY_RUN_VALUE_NAME = "WebcamAutoLock"
# Pre-rename .lnk basename. Same cleanup story.
_LEGACY_AUTOSTART_LINK_NAME = "WebcamAutoLock.lnk"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _start_menu_programs_folder() -> Path:
    """Base `.../Start Menu/Programs/` folder. The Startup folder is a
    child of this; the Start Menu list items live directly in it."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("%APPDATA% not set -- can't locate Start Menu folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _startup_folder() -> Path:
    return _start_menu_programs_folder() / "Startup"


def _startup_shortcut_path() -> Path:
    return _startup_folder() / config.AUTOSTART_LINK_NAME


def _start_menu_shortcut_path() -> Path:
    return _start_menu_programs_folder() / config.AUTOSTART_LINK_NAME


def _desktop_folder() -> Path:
    """Resolve the Desktop folder, handling OneDrive redirection.

    Modern Windows setups sometimes relocate Desktop into OneDrive; the
    WScript.Shell SpecialFolders API returns whichever location is
    active. Falling back to `~/Desktop` keeps us working if the COM
    call fails (very rare).
    """
    try:
        from win32com.client import Dispatch  # type: ignore
        shell = Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")
        if desktop:
            return Path(str(desktop))
    except Exception:
        log.exception("Could not resolve Desktop via WScript.Shell; falling back")
    return Path.home() / "Desktop"


def _desktop_shortcut_path() -> Path:
    return _desktop_folder() / config.AUTOSTART_LINK_NAME


def _pythonw_exe() -> Path:
    """Find pythonw.exe next to the current interpreter.

    On a normal venv install, sys.executable is either python.exe or
    pythonw.exe depending on how main.py was launched. We always want
    the windowed variant for the shortcut target so login doesn't flash
    a console window at the user.
    """
    py = Path(sys.executable)
    candidate = py.with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    log.warning("pythonw.exe not found next to %s; falling back to python.exe", py)
    return py


# ---------------------------------------------------------------------------
# Icon (.ico) generation
# ---------------------------------------------------------------------------


def _icon_path() -> Path:
    return config.DATA_DIR / "icon.ico"


def ensure_icon_file() -> Path:
    """Generate `%LOCALAPPDATA%\\Vigil\\icon.ico` if it doesn't exist.

    Embeds multiple resolutions (16, 32, 48, 64, 128, 256) so Windows
    renders cleanly in Start search results, taskbar, and shortcut
    thumbnails. Idempotent: returns the path without touching the file
    if it already exists.
    """
    path = _icon_path()
    if path.exists():
        return path

    config.ensure_data_dir()

    # Lazy import so autostart.py can be imported in environments without
    # Pillow (e.g. during --autostart-status checks in CI).
    from PIL import Image, ImageDraw

    # Render the largest size; Pillow downsamples for the others when
    # saving the multi-resolution ICO.
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Green outer ring matching the WATCHING tray color.
    ring_outer = 8
    draw.ellipse(
        (ring_outer, ring_outer, size - ring_outer, size - ring_outer),
        fill=(0, 180, 0, 255),
        outline=(0, 60, 0, 255),
        width=8,
    )
    # Inner "pupil" so the icon reads as an eye at small sizes.
    pupil_r = size // 5
    cx, cy = size // 2, size // 2
    draw.ellipse(
        (cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r),
        fill=(240, 255, 240, 255),
        outline=(0, 60, 0, 255),
        width=4,
    )

    img.save(
        path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    log.info("Generated app icon at %s", path)
    return path


# ---------------------------------------------------------------------------
# Shortcut creation helpers
# ---------------------------------------------------------------------------


def _python_console_exe() -> Path:
    """Resolve python.exe (the console-subsystem interpreter) for the
    visible-launcher shortcuts. Sits next to pythonw.exe in the venv."""
    py = Path(sys.executable)
    candidate = py.with_name("python.exe")
    if candidate.exists():
        return candidate
    return py


def _create_shortcut(
    target_lnk: Path,
    icon_file: Path,
    *,
    visible: bool,
) -> None:
    """Create (or overwrite) the .lnk at `target_lnk` pointing at the
    daemon entry point with `icon_file` as the icon.

    `visible=True` shortcuts target `python.exe launcher.py` so the user
    sees a console window confirming their click was received (used for
    Desktop + Start-Menu, where invisible feedback is the killer UX
    bug). `visible=False` targets `pythonw.exe main.py` directly with
    no window (used for the Startup-folder shortcut, which fires
    silently on login -- nobody wants a console window opening every
    time they log in).

    Overwrites unconditionally -- if called with a fresh .ico or a moved
    repo, the shortcut is corrected on the next ensure_installed() pass.
    """
    # Import pywin32 lazily so CI / lock.py smoke tests don't need it.
    from win32com.client import Dispatch  # type: ignore

    work_dir = Path(__file__).resolve().parent
    if visible:
        # User-clicked path: python.exe launcher.py -> shows a console
        # countdown, spawns pythonw main.py detached, then closes itself.
        py = _python_console_exe()
        script = work_dir / "launcher.py"
        # WindowStyle=1 (normal) -- we WANT the console visible.
        window_style = 1
    else:
        # Background-autostart path: pythonw.exe main.py -> totally silent.
        py = _pythonw_exe()
        script = work_dir / "main.py"
        # WindowStyle=7 (minimized) -- pythonw has no window anyway, but
        # this keeps the .lnk's preferred-state metadata sane.
        window_style = 7

    target_lnk.parent.mkdir(parents=True, exist_ok=True)

    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(target_lnk))
    sc.TargetPath = str(py)
    sc.Arguments = f'"{script}"'
    sc.WorkingDirectory = str(work_dir)
    sc.IconLocation = f"{icon_file},0"
    sc.Description = f"{config.APP_NAME} -- locks the PC when you leave the frame"
    sc.WindowStyle = window_style
    sc.save()


def install_startup_shortcut() -> Path:
    """Install the Startup-folder shortcut (runs SILENTLY on login)."""
    icon = ensure_icon_file()
    target = _startup_shortcut_path()
    _create_shortcut(target, icon, visible=False)
    log.info("Installed Startup shortcut (silent) at %s", target)
    return target


def install_start_menu_shortcut() -> Path:
    """Install the Start Menu shortcut (visible launcher with console)."""
    icon = ensure_icon_file()
    target = _start_menu_shortcut_path()
    _create_shortcut(target, icon, visible=True)
    log.info("Installed Start Menu shortcut (visible) at %s", target)
    return target


def install_desktop_shortcut() -> Path:
    """Install a shortcut on the user's Desktop (visible launcher).

    Why Desktop rather than programmatic Pin-to-Start/Pin-to-Taskbar?
    Microsoft has progressively locked down those APIs since Windows 10
    1607 (to stop bloatware from auto-pinning itself), so programmatic
    pinning either silently fails or works only on specific builds. A
    Desktop shortcut is reliable on every Windows version and the user
    can right-click -> Pin to Start / Pin to Taskbar in one gesture,
    using the exact mechanism Microsoft supports.

    This shortcut targets the visible launcher (python.exe launcher.py)
    not pythonw.exe main.py directly -- a Desktop-icon click that
    produces zero visible feedback is the most common complaint when
    the tray balloon path is intercepted on the user's machine.
    """
    icon = ensure_icon_file()
    target = _desktop_shortcut_path()
    _create_shortcut(target, icon, visible=True)
    log.info("Installed Desktop shortcut (visible) at %s", target)
    return target


def _run_key_command() -> str:
    """The exact command line stored in the HKCU\\Run value.

    Format: `"<pythonw.exe>" "<main.py>"`. Both paths quoted so spaces in
    either path survive the shell's argv splitting.
    """
    main_py = Path(__file__).with_name("main.py").resolve()
    pythonw = _pythonw_exe()
    return f'"{pythonw}" "{main_py}"'


def install_run_key() -> str:
    """Create/overwrite HKCU\\...\\Run\\Vigil with the daemon command line.
    Returns the value written.

    Idempotent — overwrites unconditionally so a moved repo or rebuilt
    venv gets the corrected path on the next ensure_installed() call.
    """
    cmd = _run_key_command()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
    log.info("Installed HKCU\\Run\\%s = %s", _RUN_VALUE_NAME, cmd)
    return cmd


def _run_key_value() -> str | None:
    """Return the current HKCU\\Run\\Vigil value, or None if the value
    isn't set."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _type = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def uninstall_run_key() -> bool:
    """Remove HKCU\\Run\\Vigil. Returns True if it existed."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _RUN_VALUE_NAME)
        log.info("Removed HKCU\\Run\\%s", _RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("Failed to remove HKCU\\Run\\%s", _RUN_VALUE_NAME)
        return False


def _cleanup_legacy_entries() -> None:
    """Remove pre-rename autostart shortcuts and Run-key entries.

    Called from ensure_installed() so an upgrading user doesn't end up
    with both "WebcamAutoLock" AND "Vigil" entries running side-by-side
    (the mutex would let only one win, but Task Manager would show two
    rows in Startup which is just confusing).

    Best-effort: anything that fails (file in use, permission denied) is
    logged and swallowed.
    """
    legacy_paths = [
        _startup_folder() / _LEGACY_AUTOSTART_LINK_NAME,
        _start_menu_programs_folder() / _LEGACY_AUTOSTART_LINK_NAME,
        _desktop_folder() / _LEGACY_AUTOSTART_LINK_NAME,
    ]
    for p in legacy_paths:
        try:
            if p.exists():
                p.unlink()
                log.info("Removed legacy shortcut %s", p)
        except OSError:
            log.exception("Could not remove legacy shortcut %s", p)
    # Legacy Run-key value.
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, _LEGACY_RUN_VALUE_NAME)
                log.info("Removed legacy HKCU\\Run\\%s", _LEGACY_RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    except OSError:
        log.exception("Could not open HKCU\\Run for legacy cleanup")


def install() -> tuple[Path, Path, Path, str]:
    """Install all three shortcuts and the Run-key entry.

    Returns (startup, start_menu, desktop, run_key_command).
    """
    return (
        install_startup_shortcut(),
        install_start_menu_shortcut(),
        install_desktop_shortcut(),
        install_run_key(),
    )


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def _remove(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        log.exception("Failed to remove %s", path)
        return False
    log.info("Removed shortcut at %s", path)
    return True


def uninstall() -> bool:
    """Remove all shortcuts and the Run-key entry. Returns True if ANY existed."""
    a = _remove(_startup_shortcut_path())
    b = _remove(_start_menu_shortcut_path())
    c = _remove(_desktop_shortcut_path())
    d = uninstall_run_key()
    return a or b or c or d


def is_installed() -> bool:
    """True if ALL shortcuts and the Run-key entry exist. Used by
    ensure_installed() to decide whether there's anything to do."""
    return (
        _startup_shortcut_path().exists()
        and _start_menu_shortcut_path().exists()
        and _desktop_shortcut_path().exists()
        and _run_key_value() is not None
    )


# ---------------------------------------------------------------------------
# Idempotent ensure (called from main.py on every startup)
# ---------------------------------------------------------------------------


def ensure_installed() -> None:
    """Install any missing shortcuts. Safe to call every startup.

    Each shortcut is installed independently so a partial state (e.g. the
    user deleted just the Start Menu entry) gets healed correctly. The
    Desktop shortcut is intentionally part of this auto-heal -- if the
    user deletes it from their Desktop on purpose, the next daemon boot
    will put it back. This matches the overall autostart model: the
    daemon always wants its shortcuts present. If that's not desired,
    pass `--no-autoinstall`.

    Failures are logged and swallowed -- shortcut setup is a
    nice-to-have, not a reason to block the daemon.
    """
    try:
        # Startup shortcut: install only if missing (idempotent path).
        # We don't re-write it every boot because the user MAY have
        # disabled it via Task Manager -> Startup -> Disable, which
        # writes a "user disabled this" marker into the StartupApproved
        # registry key. Re-creating the .lnk would not undo that, but
        # creating-on-missing avoids touching a deliberately-disabled
        # entry.
        if not _startup_shortcut_path().exists():
            install_startup_shortcut()
        # Desktop + Start-Menu shortcuts: ALWAYS re-install. These point
        # at launcher.py (visible console window) rather than pythonw
        # main.py directly, and existing pre-launcher installs need to
        # be upgraded. Idempotent: WScript.Shell overwrites in-place
        # with the corrected target. Cost is one COM call per boot --
        # negligible.
        install_desktop_shortcut()
        install_start_menu_shortcut()
        # Always re-write the Run-key value: cheap, and self-heals if the
        # path got stale (e.g. venv rebuilt elsewhere). Reading-then-writing
        # only-on-change would save microseconds and add a branch.
        install_run_key()
        # Remove any pre-rename WebcamAutoLock.lnk + Run-key entry so
        # upgrading users don't see two startup rows in Task Manager.
        _cleanup_legacy_entries()
    except Exception:
        log.exception("ensure_installed: failed to install shortcut(s)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("install", "uninstall", "status", "icon"),
    )
    args = parser.parse_args()
    if args.action == "install":
        sp, mp, dp, rk = install()
        print("Installed:")
        print(f"  Startup:    {sp}")
        print(f"  Start Menu: {mp}")
        print(f"  Desktop:    {dp}")
        print(f"  Run key:    HKCU\\{_RUN_KEY}\\{_RUN_VALUE_NAME}")
        print(f"              = {rk}")
        print()
        print("To put the icon on your taskbar or Start tiles: right-click the")
        print("Desktop shortcut and choose 'Pin to taskbar' or 'Pin to Start'.")
    elif args.action == "uninstall":
        print("Removed." if uninstall() else "Nothing to remove.")
    elif args.action == "icon":
        p = ensure_icon_file()
        print(f"Icon: {p}")
    else:
        rkv = _run_key_value()
        print(f"Startup shortcut:    {'yes' if _startup_shortcut_path().exists() else 'NO'}  {_startup_shortcut_path()}")
        print(f"Start Menu shortcut: {'yes' if _start_menu_shortcut_path().exists() else 'NO'}  {_start_menu_shortcut_path()}")
        print(f"Desktop shortcut:    {'yes' if _desktop_shortcut_path().exists() else 'NO'}  {_desktop_shortcut_path()}")
        print(f"Run key:             {'yes' if rkv else 'NO'}  HKCU\\{_RUN_KEY}\\{_RUN_VALUE_NAME}")
        if rkv:
            print(f"                     = {rkv}")
        print(f"Icon:                {'yes' if _icon_path().exists() else 'NO'}  {_icon_path()}")
