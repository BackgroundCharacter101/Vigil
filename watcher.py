"""State machine + webcam capture + recognition loop.

This is the heart of the program. It owns the camera, runs detection,
decides when to lock, and handles the awkward edge cases that naive
implementations get wrong:

  * CAMERA_UNAVAILABLE — another app (Zoom, Teams, OBS) has the camera.
    The naive design would see every frame as "no face detected" and lock
    after 2 seconds. We detect this state and pause locking instead.

  * LOCKED_SCREEN — Windows is already on the Winlogon secure desktop.
    Don't hold the camera, don't re-lock, just wait until we're back on
    the Default desktop.

  * Sliding-window detection — two thresholds, one for "stranger in frame"
    (lock fast), one for "no face at all" (more lenient).

Detection + recognition is delegated to face_engine.py (InsightFace), which
handles profile/angled views that the older dlib pipeline could not.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections import deque
from typing import Deque, Optional

import cv2
import numpy as np

import config
import face_engine
import lock as lock_module

log = logging.getLogger(__name__)


class State(enum.Enum):
    STARTING = "STARTING"
    WATCHING = "WATCHING"
    PAUSED = "PAUSED"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
    LOCKED_SCREEN = "LOCKED_SCREEN"
    STOPPED = "STOPPED"


class Observation(enum.Enum):
    OWNER = "owner"        # Owner's face matched.
    STRANGER = "stranger"  # A face was detected but it didn't match.
    EMPTY = "empty"        # No face detected at all.


class Watcher:
    """Owns the webcam, runs the detection loop, drives the state machine."""

    def __init__(self, on_state_change=None, on_external_event=None) -> None:
        self._on_state_change = on_state_change
        # Cross-process events surfaced from main.py's mutex-collision
        # path (and potentially other future signals). Currently invoked
        # with the literal string "already_running" when a duplicate
        # launch tries to run; the callback is expected to pop a tray
        # balloon. Keeping it generic so we can add more event types
        # (e.g. "encoding_updated") without changing the wiring.
        self._on_external_event = on_external_event
        self._state = State.STARTING
        self._state_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused

        self._cap: Optional[cv2.VideoCapture] = None
        self._known_encoding: Optional[np.ndarray] = None

        self._window: Deque[Observation] = deque(maxlen=config.WINDOW_SIZE)
        self._grace_until: float = 0.0
        self._camera_fail_since: Optional[float] = None

        # Wall-clock timestamp of the most recent OWNER observation. Drives
        # the NO_FACE_LOCK_SECONDS trigger. 0.0 means "never seen since the
        # current watching session began" -- in which case _maybe_lock falls
        # back to the end-of-grace time as the baseline.
        self._last_owner_seen: float = 0.0

        # FPS instrumentation. Every ~5s we log the achieved detection rate
        # so we can tell whether we're hitting FPS_TARGET or bottlenecked on
        # InsightFace. Only counts ticks that actually ran detection.
        self._fps_window_start: float = 0.0
        self._fps_frames: int = 0

    # ---- public API -------------------------------------------------------

    def run(self) -> None:
        """Main loop. Returns when stop() is called."""
        self._load_encoding()
        # Preload the InsightFace model before entering STARTING so the
        # first tick doesn't hit a multi-second init delay (which would
        # extend grace period in practice but also delay the tray icon
        # going green).
        face_engine.preload()
        self._set_state(State.STARTING)
        self._start_grace()

        period = 1.0 / max(1, config.FPS_TARGET)

        try:
            while not self._stop_event.is_set():
                loop_start = time.time()
                self._tick()
                elapsed = time.time() - loop_start
                # Sleep the remainder of the frame budget, but wake early on stop.
                sleep_for = max(0.0, period - elapsed)
                if self._stop_event.wait(sleep_for):
                    break
        finally:
            self._release_camera()
            self._set_state(State.STOPPED)
            log.info("Watcher stopped")

    def stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        if self._pause_event.is_set():
            return
        log.info("Watcher PAUSED (hotkey/tray)")
        self._pause_event.set()
        # Pause stops detection entirely, so the FPS counter shouldn't
        # include the pause duration in its next sample.
        self._reset_fps_sample()

    def resume(self) -> None:
        if not self._pause_event.is_set():
            return
        log.info("Watcher RESUMED (hotkey/tray)")
        self._pause_event.clear()
        # Returning from pause — give a fresh grace period so we don't
        # instantly lock on a stale window, and reset the FPS counter so
        # the first post-resume sample reflects actual detection speed
        # (not the pause duration).
        self._window.clear()
        self._start_grace()
        self._reset_fps_sample()

    def toggle_pause(self) -> None:
        if self._pause_event.is_set():
            self.resume()
        else:
            self.pause()

    @property
    def state(self) -> State:
        with self._state_lock:
            return self._state

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    # ---- state machine ----------------------------------------------------

    def _set_state(self, new_state: State) -> None:
        with self._state_lock:
            if new_state == self._state:
                return
            old = self._state
            self._state = new_state
        log.info("State: %s -> %s", old.value, new_state.value)
        if self._on_state_change is not None:
            try:
                self._on_state_change(new_state)
            except Exception:
                log.exception("on_state_change callback raised")

    def _start_grace(self) -> None:
        self._grace_until = time.time() + config.STARTUP_GRACE_SECONDS
        # Any time we enter a grace period we also reset the last-seen
        # timestamp, so the NO_FACE_LOCK_SECONDS baseline starts from
        # end-of-grace rather than from some stale pre-outage value.
        # Without this, returning from LOCKED_SCREEN / CAMERA_UNAVAILABLE
        # could insta-lock the moment grace expires.
        self._last_owner_seen = 0.0

    def _in_grace(self) -> bool:
        return time.time() < self._grace_until

    def _tick(self) -> None:
        """Run a single frame's worth of work. Cheap paths first."""

        # --- external IPC: did a duplicate launch ask us to ping the user? --
        # Cheap stat() check; the file is rare so this is essentially free.
        # Done OUTSIDE the locked-screen / paused early-returns so that a
        # double-click of the launcher while we're paused/locked still
        # surfaces the "already running" balloon when we wake.
        self._check_external_signals()

        # --- screen locked? -------------------------------------------------
        if lock_module.is_screen_locked():
            if self.state != State.LOCKED_SCREEN:
                self._release_camera()
                self._window.clear()
                self._set_state(State.LOCKED_SCREEN)
            # While locked, poll slowly. Return and let run()'s sleep handle
            # the rate limiting — no need for an extra sleep here.
            time.sleep(max(0.0, config.LOCKED_SCREEN_POLL_SECONDS - 1.0 / config.FPS_TARGET))
            return

        # Coming back from LOCKED_SCREEN — restart grace.
        if self.state == State.LOCKED_SCREEN:
            self._set_state(State.STARTING)
            self._start_grace()
            return

        # --- paused? --------------------------------------------------------
        if self._pause_event.is_set():
            if self.state != State.PAUSED:
                self._release_camera()
                self._window.clear()
                self._set_state(State.PAUSED)
            return

        # --- camera available? ---------------------------------------------
        if not self._ensure_camera_open():
            # Still can't open the camera — sit in CAMERA_UNAVAILABLE.
            if self.state != State.CAMERA_UNAVAILABLE:
                self._set_state(State.CAMERA_UNAVAILABLE)
            time.sleep(config.CAMERA_RETRY_SECONDS)
            return

        ok, frame = self._cap.read()
        if not ok or frame is None:
            now = time.time()
            if self._camera_fail_since is None:
                self._camera_fail_since = now
            elif now - self._camera_fail_since > 1.0:
                # Persistent failure — treat as CAMERA_UNAVAILABLE.
                log.warning("Camera read failing for >1s; releasing camera")
                self._release_camera()
                if self.state != State.CAMERA_UNAVAILABLE:
                    self._set_state(State.CAMERA_UNAVAILABLE)
            return
        self._camera_fail_since = None

        # We have a frame and the camera is working. Any state that isn't
        # already WATCHING needs to transition to it now -- without this,
        # resume-from-PAUSE leaves the tray icon yellow even though
        # detection is running (and, worse, lock triggers fire from the
        # stale PAUSED state).
        if self.state != State.WATCHING:
            # Only re-arm grace for a genuine startup/outage transition;
            # during resume the grace period has already been re-armed
            # by resume(), and during a normal frame-by-frame recovery
            # we don't want to keep pushing grace forward.
            if self.state in (State.STARTING, State.CAMERA_UNAVAILABLE) and not self._in_grace():
                self._start_grace()
            self._set_state(State.WATCHING)

        observation = self._observe(frame)
        self._window.append(observation)
        self._record_fps_sample()

        if self._in_grace():
            return

        self._maybe_lock()

    def _check_external_signals(self) -> None:
        """Poll for cross-process IPC markers from a duplicate Vigil launch.

        Currently the only signal is the "already running" flag dropped by
        main._show_already_running_notification(). When seen, we delete it
        and fire `on_external_event("already_running")` so main.py can pop
        a tray balloon. Deletion is what makes the signal one-shot --
        without it, every tick after a duplicate launch would re-fire.

        OSError on either exists()/unlink() is swallowed: a flag we can't
        delete (permission, race) just means we'll see it again next tick
        and try again. Worst case the balloon shows twice -- which is
        still strictly better than the previous behavior of showing zero
        times.
        """
        try:
            flag = config.NOTIFY_ALREADY_RUNNING_FLAG
            if flag.exists():
                try:
                    flag.unlink()
                except OSError:
                    log.exception("Could not delete notify flag %s", flag)
                if self._on_external_event is not None:
                    try:
                        self._on_external_event("already_running")
                    except Exception:
                        log.exception("on_external_event callback raised")
        except OSError:
            # exists() can raise on a transient FS error; ignore and retry.
            pass

    def _record_fps_sample(self) -> None:
        now = time.time()
        if self._fps_window_start == 0.0:
            self._fps_window_start = now
            self._fps_frames = 1
            return
        self._fps_frames += 1
        elapsed = now - self._fps_window_start
        if elapsed >= 5.0:
            fps = self._fps_frames / elapsed
            log.info("Detection FPS: %.1f (%d frames in %.1fs)",
                     fps, self._fps_frames, elapsed)
            self._fps_window_start = now
            self._fps_frames = 0

    def _reset_fps_sample(self) -> None:
        self._fps_window_start = 0.0
        self._fps_frames = 0

    # ---- camera helpers ---------------------------------------------------

    def _ensure_camera_open(self) -> bool:
        if self._cap is not None and self._cap.isOpened():
            return True
        # (Re)open.
        self._release_camera()
        log.info("Opening camera index %d", config.CAMERA_INDEX)
        cap = cv2.VideoCapture(config.CAMERA_INDEX, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(config.CAMERA_INDEX)
        if not cap.isOpened():
            log.warning("Camera index %d could not be opened", config.CAMERA_INDEX)
            return False
        self._cap = cap
        self._camera_fail_since = None
        return True

    def _release_camera(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                log.exception("Error releasing camera")
            self._cap = None

    # ---- detection --------------------------------------------------------

    def _load_encoding(self) -> None:
        path = config.ENCODING_FILE
        if not path.exists():
            raise FileNotFoundError(
                f"No reference face encoding at {path}. "
                f"Run `python enroll.py` first."
            )
        enc = np.load(path).astype(np.float32)
        # Defensive re-normalize: ensures the stored embedding is unit-norm
        # so cosine similarity (= dot product) is correct even if something
        # upstream forgot to normalize.
        norm = float(np.linalg.norm(enc))
        if norm > 0:
            enc = enc / norm
        self._known_encoding = enc
        log.info("Loaded reference encoding from %s (shape=%s)",
                 path, self._known_encoding.shape)

    def _observe(self, frame_bgr: np.ndarray) -> Observation:
        faces = face_engine.detect_faces(frame_bgr)
        if not faces:
            return Observation.EMPTY

        best_sim = face_engine.best_similarity(self._known_encoding, faces)

        # Three-way classification, NOT two-way. The naive version was:
        #   sim >= MATCH_THRESHOLD -> OWNER
        #   sim <  MATCH_THRESHOLD -> STRANGER
        # That misclassified "owner with hand near face / phone occluding
        # jaw / glasses / hair across eye" as STRANGER, which then triggered
        # the fast (~2s at FPS=1) STRANGER_LOCK_FRAMES path -- locking the
        # PC every time the user glanced at their phone.
        #
        # Now there's a middle "uncertain" zone between STRANGER_HARD_THRESHOLD
        # and MATCH_THRESHOLD. Faces in that zone are treated as EMPTY so
        # they fall under the lenient time-based NO_FACE_LOCK_SECONDS path
        # (6s), not the fast frame-count STRANGER path. A real stranger
        # scores well below 0.2 and still trips the fast lock.
        if best_sim >= config.MATCH_THRESHOLD:
            self._last_owner_seen = time.time()
            return Observation.OWNER
        if best_sim >= config.STRANGER_HARD_THRESHOLD:
            log.debug(
                "Uncertain face (sim=%.2f, owner>=%.2f stranger<%.2f); treating as EMPTY",
                best_sim, config.MATCH_THRESHOLD, config.STRANGER_HARD_THRESHOLD,
            )
            return Observation.EMPTY
        return Observation.STRANGER

    def _maybe_lock(self) -> None:
        if len(self._window) == 0:
            return

        # STRANGER threshold: look at the last STRANGER_LOCK_FRAMES entries
        # and lock if they are ALL strangers (nobody else seen in between).
        n_stranger_tail = min(config.STRANGER_LOCK_FRAMES, len(self._window))
        tail = list(self._window)[-n_stranger_tail:]
        if n_stranger_tail >= config.STRANGER_LOCK_FRAMES and all(
            o == Observation.STRANGER for o in tail
        ):
            log.warning(
                "Lock trigger: STRANGER in last %d frames (fast threshold)",
                config.STRANGER_LOCK_FRAMES,
            )
            self._do_lock()
            return

        # NO-FACE threshold: time-based. Lock if the owner has not been
        # seen in the last NO_FACE_LOCK_SECONDS. The clock RESETS every
        # time a frame matches -- so "glance down at the keyboard for 3s
        # then look back up" doesn't accumulate toward the threshold.
        #
        # If the owner has never been seen since this WATCHING session
        # began (_last_owner_seen == 0), we measure from when the grace
        # period ended. This is the "sat down, camera on, no face yet"
        # case after startup / unpause / unlock.
        baseline = self._last_owner_seen
        if baseline == 0.0:
            baseline = self._grace_until
        gap = time.time() - baseline
        if gap >= config.NO_FACE_LOCK_SECONDS:
            log.warning(
                "Lock trigger: owner not seen for %.1fs (threshold %.1fs)",
                gap, config.NO_FACE_LOCK_SECONDS,
            )
            self._do_lock()

    def _do_lock(self) -> None:
        # Release camera BEFORE locking so the logon/lock screen can use it
        # for Windows Hello (if configured) without contention.
        self._release_camera()
        ok = lock_module.lock_workstation()
        if not ok:
            log.error("lock_workstation() failed")
        # Clear window so we don't immediately re-trigger on resume.
        self._window.clear()
        # Windows takes ~100-300ms to actually switch to the Winlogon secure
        # desktop after LockWorkStation returns. Without a brief wait, the
        # very next _tick's is_screen_locked() check sometimes sees the
        # Default desktop still active, transitions us LOCKED_SCREEN ->
        # STARTING, then flips back on the tick after that. That's harmless
        # but produces a noisy 4-line state-transition burst in the log.
        # 500ms is well inside any reasonable lock latency.
        time.sleep(0.5)
        self._set_state(State.LOCKED_SCREEN)
        self._reset_fps_sample()
