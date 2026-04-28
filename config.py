"""Centralized configuration for Vigil (webcam auto-lock).

All tunables live here. Change a value, restart the daemon, done.

Runtime data (encoding file, log) is kept in %LOCALAPPDATA%\\Vigil\\ so the
install folder doesn't need write permissions and re-enrolling doesn't dirty
the git tree.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

# Webcam device index.
#   0 = USB2.0 HD UVC WebCam          ← the actual laptop webcam
#   1 = OBS Virtual Camera
# The enumeration order depends on installed DirectShow drivers and can
# shift when drivers are added/removed. Re-check with
# `python enroll.py --list-cameras` after any webcam/driver change.
CAMERA_INDEX: int = 0

# Target capture frame rate. This is a CAP, not a guarantee: if detection is
# slower than this, we run at whatever rate detection can sustain. With
# the single-thread ORT cap (see main._install_ort_thread_cap), detection
# costs ~400ms per frame at det_size=320; on top of that we want the loop
# to SLEEP for the rest of each tick so CPU stays low.
#
#   FPS=1 -> ~400ms detect + ~600ms sleep = ~3% CPU on a 20-core machine
#   FPS=2 -> ~400ms detect + ~100ms sleep = ~6% CPU
#   FPS=5 -> detection-bound, no sleep, ~12% CPU
#
# NO_FACE_LOCK_SECONDS=6 means even FPS=1 gives us 6 detection chances
# before locking, which is plenty. Bump higher only if you're seeing
# missed-detection-induced false locks (rare in practice).
FPS_TARGET: int = 1

# InsightFace RetinaFace detection input resolution. Frames are resized to
# this square before running the detector. The owner sitting 0.3-1.0m from
# the webcam is large enough that 320x320 finds them fine, and the speedup
# vs 640x640 is ~3x on CPU. Bump back up only if you need to detect small
# or distant faces.
DETECTION_SIZE: int = 320

# Downsample factor applied to each frame before running face detection/
# encoding. Legacy from the dlib/face_recognition path — InsightFace does
# its own internal resizing via DETECTION_SIZE above. Kept only for
# backward compat with any code that still references it.
FRAME_DOWNSCALE: int = 1

# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------

# InsightFace cosine similarity threshold. HIGHER = better match (opposite
# direction from the old face_recognition distance metric).
#
#   >= 0.6  very strict — same person under similar conditions
#   >= 0.5  strict — good default for a security-flavored tool
#   >= 0.4  balanced — tolerant of angles / lighting / minor occlusion
#   >= 0.3  loose — may false-accept a similar-looking person
#
# For a side-angle laptop webcam where the user is rarely frontal, 0.4 is
# a good starting point and is what the existing reference encoding was
# tuned against. Bumping toward 0.5 makes the matcher stricter (less
# likely to false-accept a stranger who happens to look similar) but you
# MUST re-enroll afterwards -- the existing encoding may not score above
# the new threshold even for you, leading to instant self-lockout.
MATCH_THRESHOLD: float = 0.4

# A face below MATCH_THRESHOLD is "not the owner" -- but is it a STRANGER
# (lock fast!) or just a partially-occluded YOU (your hand on your face,
# your phone in front of your jaw, hair across your eye)?
#
# An unrelated person's face typically scores ~0.05-0.15 against your
# encoding. A degraded/occluded YOU typically scores ~0.20-0.38. So we
# treat similarity in the "uncertain zone" [STRANGER_HARD_THRESHOLD,
# MATCH_THRESHOLD) as if no clear face was present -- it falls into the
# lenient NO_FACE_LOCK_SECONDS bucket (6s) rather than the aggressive
# STRANGER_LOCK_FRAMES bucket (~2s at FPS=1).
#
# Without this, glancing at your phone classifies as STRANGER and locks
# the PC after 2 seconds, which is the most common false-lock complaint.
#
# Range guide:
#   0.10  paranoid -- lock on anything that isn't almost-random
#   0.20  balanced (default) -- typical occlusion still uncertain
#   0.30  permissive -- only lock on demonstrably-different faces
#
# Setting this >= MATCH_THRESHOLD disables the uncertain zone entirely
# (every non-match becomes STRANGER), reproducing the pre-fix behavior.
STRANGER_HARD_THRESHOLD: float = 0.2

# ---------------------------------------------------------------------------
# Lock logic
# ---------------------------------------------------------------------------
# Two distinct triggers for two distinct threat models:
#
#   1) STRANGER lock (fast): consecutive frames of a face-that-isn't-you.
#      Someone sat down at your keyboard. Frame-count based because the
#      trigger requires an UNBROKEN tail of stranger frames.
#
#   2) NO-FACE lock (lenient): it's been N seconds since your face was
#      last seen. You walked away. TIME-based, not frame-count based, so
#      that "glance down at keyboard for 3 seconds" doesn't trigger a
#      lock -- as soon as you look back up and your face is detected,
#      the clock resets.
#
# The time-based approach is the important one. An earlier frame-count
# variant ("10 of last 12 frames without owner") was too aggressive for
# realistic use: at ~2.5 fps on CPU, 10 bad frames arrives in ~4 seconds,
# but a user looking at their keyboard for 4 seconds is NOT "away".

# Rolling window of recent observations. Only used for the STRANGER tail
# check below -- size it to cover STRANGER_LOCK_FRAMES comfortably. At
# FPS=1, 8 frames is ~8 seconds of history which is plenty.
WINDOW_SIZE: int = 8

# Consecutive stranger frames needed to fast-lock. At FPS=1 this is ~2s,
# matching the original "lock within ~2 seconds" requirement. Don't set
# this so low that a one-frame misclassification causes a lock -- 2 is
# the floor (1 means a single misdetection locks the PC).
STRANGER_LOCK_FRAMES: int = 2

# Seconds since the owner was last seen (OR since startup grace ended, if
# never seen yet) before we lock. Tolerant of brief look-aways because the
# clock RESETS on every successful owner match.
#
#   ~4s  aggressive -- glance at phone can trigger
#   ~6s  balanced   -- glance at phone safe; longer reads risky
#   ~12s lenient    -- you can read a paper for a while without locking
#   ~20s very lenient -- forgiving of stale face encodings / detection
#                       hiccups; better default if you keep seeing
#                       false-locks while sitting at the computer.
#
# Bumped to 15s after field reports of repeated false-locks: the daemon
# would lock, the user would unlock, the post-unlock grace would expire,
# and the daemon would lock AGAIN within ~10s -- usually because the
# user's appearance had drifted from the enrollment encoding (lighting,
# glasses on/off, monitor position) and the matcher was missing them.
# 15s gives enough headroom that even a multi-second occlusion or a few
# missed-detection frames don't trigger the time-based lock.
NO_FACE_LOCK_SECONDS: float = 15.0

# Grace period on startup — don't enforce for the first N seconds after
# launch. Gives the camera time to warm up and the user time to settle in
# after login. Also used when returning from LOCKED_SCREEN state.
#
# 8s (was 5s) after observing a relock loop: lock fires -> user unlocks
# -> daemon enters STARTING -> 5s grace -> WATCHING -> camera/detector
# is still warming up, fails to find owner -> NO_FACE_LOCK_SECONDS
# expires -> immediate re-lock. 8s of grace is enough for InsightFace to
# get into a steady-state detection cycle even after a cold camera
# reopen.
STARTUP_GRACE_SECONDS: float = 8.0

# How often to retry opening the camera when it's busy (Zoom, Teams, OBS).
CAMERA_RETRY_SECONDS: float = 3.0

# How often to poll the desktop state to see if the screen is still locked.
LOCKED_SCREEN_POLL_SECONDS: float = 1.0

# ---------------------------------------------------------------------------
# Hotkey
# ---------------------------------------------------------------------------

# pynput GlobalHotKeys format. Edit to whatever combo you prefer.
# Examples:  "<ctrl>+<alt>+p"  "<ctrl>+<shift>+l"  "<f12>"
PAUSE_HOTKEY: str = "<ctrl>+<alt>+p"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_LOCALAPPDATA: Path = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
DATA_DIR: Path = _LOCALAPPDATA / "Vigil"
LOG_FILE: Path = DATA_DIR / "vigil.log"
ENCODING_FILE: Path = DATA_DIR / "known_face.npy"
ENCODING_BACKUP_FILE: Path = DATA_DIR / "known_face.npy.bak"

# Cross-process IPC: when a SECOND launch of Vigil hits the
# single-instance mutex and exits, it touches this file. The running
# daemon's watcher tick polls for it and pops a tray balloon ("Vigil is
# already running -- look for it in the tray"). This replaces an earlier
# attempt that shelled out to PowerShell to show a UWP toast directly --
# the PowerShell quote-escaping was fragile and silently broken on at
# least one machine, so the user double-clicked the shortcut and
# observed absolutely no feedback. The marker-file path reuses the
# pystray notification code that we already know works (it's the same
# code that fires the "Vigil is active" balloon at first launch), so
# it's robust to whatever was eating the toast.
NOTIFY_ALREADY_RUNNING_FLAG: Path = DATA_DIR / ".notify_already_running"

# Pre-rename data directory. Used by ensure_data_dir() to migrate user
# state (face encoding, log) from the old "lock" location to the new
# "Vigil" location on first start after the rename. Safe to delete this
# constant once nobody on the planet still has a %LOCALAPPDATA%\lock\
# folder lying around.
_LEGACY_DATA_DIR: Path = _LOCALAPPDATA / "lock"

# ---------------------------------------------------------------------------
# Single-instance / autostart naming
# ---------------------------------------------------------------------------

# Named mutex used to detect a duplicate running daemon.
MUTEX_NAME: str = "Global\\Vigil_SingleInstance_Mutex"

# Display name for the Startup-folder shortcut.
AUTOSTART_LINK_NAME: str = "Vigil.lnk"

# Human-readable app name (used in log lines, tray tooltip, toast titles).
APP_NAME: str = "Vigil"

# One-line tagline for the README and any "about" surface that needs it.
APP_TAGLINE: str = "Webcam-aware auto-lock for Windows"


def ensure_data_dir() -> None:
    """Create %LOCALAPPDATA%\\Vigil\\ if it doesn't exist, and migrate any
    files from the pre-rename %LOCALAPPDATA%\\lock\\ folder.

    Migration is one-shot: we copy the face encoding and the log over so an
    upgrading user keeps their enrollment + history. We DON'T delete the
    old folder -- a paranoid user might have other things in there, and
    leaving it around costs nothing. The next re-enroll will write only to
    the new location.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _LEGACY_DATA_DIR.exists() and _LEGACY_DATA_DIR != DATA_DIR:
        for name in ("known_face.npy", "known_face.npy.bak", "lock.log", "icon.ico"):
            src = _LEGACY_DATA_DIR / name
            dst = DATA_DIR / name
            if src.exists() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    # Migration is best-effort. If a file is locked (e.g.
                    # the legacy log is still being held by an older
                    # daemon process during transition), skip and move on.
                    pass
