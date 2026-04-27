"""First-run enrollment: capture reference photos of the owner and save a
single averaged 512-d face embedding to disk.

Usage:
    python enroll.py                 # use default camera from config.py
    python enroll.py --camera 1      # override camera index
    python enroll.py --test          # also run a 5-second live match test
    python enroll.py --list-cameras  # list DirectShow cameras with their indices

This runs in the FOREGROUND (with a console and a preview window). Do not
run this under pythonw.

Uses InsightFace (RetinaFace + ArcFace). Unlike the older dlib path, this
handles profile / side-angle views — important for users whose laptop
webcam doesn't face them head-on.

Controls in the preview window:
    SPACE   capture a snapshot (requires a face to be detected)
    ESC     abort without saving
    Q       same as ESC
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import cv2
import numpy as np

import config
import face_engine

# Number of snapshots to capture. Multiple shots at slightly different angles,
# averaged together, are MUCH more robust against lighting/pose variations
# than a single photo.
NUM_SNAPSHOTS = 5

# Minimum gap between accepted captures (seconds) so a held SPACE doesn't
# burn through all 5 shots on essentially the same frame.
MIN_CAPTURE_GAP_SECONDS = 0.6

# Window title.
WINDOW_TITLE = f"{config.APP_NAME} — Enrollment"

log = logging.getLogger("enroll")


def _open_camera(index: int) -> cv2.VideoCapture:
    # DSHOW is usually the fastest backend to open on Windows.
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        # Fallback to the default backend.
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(
            f"Could not open camera index {index}. Is another app using it "
            f"(Zoom/Teams/OBS)? Try --camera 1 if you have multiple cameras."
        )
    return cap


def _draw_hud(
    frame: np.ndarray,
    face_box: tuple[int, int, int, int] | None,
    captured: int,
    total: int,
    message: str,
) -> np.ndarray:
    """Overlay the face box, progress counter, and an instruction line.

    face_box is (x1, y1, x2, y2) in frame coordinates, matching InsightFace.
    """
    out = frame.copy()
    h, w = out.shape[:2]

    if face_box is not None:
        x1, y1, x2, y2 = face_box
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(
            out,
            "FACE DETECTED",
            (x1, max(0, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
        )
    else:
        cv2.putText(
            out,
            "No face detected",
            (12, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 255),
            2,
        )

    # Progress bar bottom-left.
    cv2.putText(
        out,
        f"Captured: {captured}/{total}",
        (12, h - 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        out,
        message,
        (12, h - 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        2,
    )
    return out


def _detect_single_face(frame_bgr: np.ndarray):
    """Run InsightFace on a frame. Returns (box, embedding) or (None, None).

    box is (x1, y1, x2, y2) in ORIGINAL frame coordinates.
    embedding is a 512-d L2-normalized vector.
    """
    faces = face_engine.detect_faces(frame_bgr)
    best = face_engine.best_face(faces)
    if best is None:
        return None, None
    return best.bbox, best.embedding


def enroll(camera_index: int, run_test: bool) -> int:
    config.ensure_data_dir()

    # Preload the InsightFace model BEFORE opening the camera, so the first
    # captured frame doesn't hit a multi-second initialization delay (and
    # so any download happens before the user is staring at a blank preview).
    print("Loading face recognition model (first run downloads ~280MB)...")
    face_engine.preload()
    print("Model ready.")

    cap = _open_camera(camera_index)
    cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_NORMAL)

    captured_encodings: list[np.ndarray] = []
    last_capture_time = 0.0
    status_message = "Press SPACE to capture, ESC to cancel"

    try:
        while len(captured_encodings) < NUM_SNAPSHOTS:
            ok, frame = cap.read()
            if not ok or frame is None:
                status_message = "Camera read failed -- retrying..."
                time.sleep(0.1)
                continue

            box, encoding = _detect_single_face(frame)
            hud = _draw_hud(
                frame, box, len(captured_encodings), NUM_SNAPSHOTS, status_message
            )
            cv2.imshow(WINDOW_TITLE, hud)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q"), ord("Q")):  # ESC / Q
                print("Enrollment cancelled. No file written.")
                return 1

            if key == 32:  # SPACE
                now = time.time()
                if now - last_capture_time < MIN_CAPTURE_GAP_SECONDS:
                    status_message = "Slow down -- wait a moment between captures"
                    continue
                if encoding is None:
                    status_message = "No face detected -- reposition and try again"
                    # Windows console beep.
                    try:
                        import winsound  # noqa: WPS433
                        winsound.Beep(440, 120)
                    except Exception:
                        pass
                    continue
                captured_encodings.append(encoding)
                last_capture_time = now
                status_message = (
                    f"Captured {len(captured_encodings)}/{NUM_SNAPSHOTS}. "
                    f"Shift angle slightly and press SPACE."
                )
                print(status_message)
    finally:
        cap.release()
        cv2.destroyAllWindows()

    # Average the embeddings. InsightFace embeddings are L2-normalized, so
    # the mean is NOT unit-norm — re-normalize after averaging so cosine
    # similarity comparisons stay well-behaved.
    stacked = np.stack(captured_encodings, axis=0).astype(np.float32)
    averaged = np.mean(stacked, axis=0)
    norm = float(np.linalg.norm(averaged))
    if norm > 0:
        averaged = averaged / norm

    # Back up the previous encoding file before overwriting.
    if config.ENCODING_FILE.exists():
        try:
            config.ENCODING_FILE.replace(config.ENCODING_BACKUP_FILE)
            print(f"Backed up previous encoding -> {config.ENCODING_BACKUP_FILE}")
        except OSError as exc:
            print(f"Warning: couldn't back up previous encoding: {exc}")

    np.save(config.ENCODING_FILE, averaged)
    print(f"Saved averaged encoding -> {config.ENCODING_FILE}")

    if run_test:
        _live_test(camera_index, averaged)

    return 0


def _live_test(camera_index: int, known_encoding: np.ndarray) -> None:
    """Run a ~10-second live loop showing the cosine similarity so the
    user can sanity-check the threshold.

    Longer than the old 5s -- gives the user time to actually turn their
    head to their working angle and see that recognition still fires.

    Prints a similarity sample ~4x/second to stdout so the values end up
    in the log even if the preview window is not being watched. Also
    prints a min/max/mean summary at the end, and warns loudly if the
    minimum observed similarity would cause a false lock.
    """
    print()
    print(f"Running 10-second match test -- similarity should be >= {config.MATCH_THRESHOLD}")
    print("Turn your head naturally (including your working profile angle).")
    print("Press ESC in the preview window to end early.")
    cap = _open_camera(camera_index)
    cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_NORMAL)
    end_at = time.time() + 10.0
    sims: list[float] = []
    face_frames = 0
    empty_frames = 0
    last_print = 0.0
    try:
        while time.time() < end_at:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            box, enc = _detect_single_face(frame)
            now = time.time()
            if enc is not None:
                face_frames += 1
                sim = face_engine.cosine_similarity(known_encoding, enc)
                sims.append(sim)
                is_match = sim >= config.MATCH_THRESHOLD
                color = (0, 255, 0) if is_match else (0, 0, 255)
                label = f"sim={sim:+.3f}  {'MATCH' if is_match else 'NO MATCH'}"
                if box is not None:
                    x1, y1, x2, y2 = box
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, label, (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                # Sample-print to stdout ~4 times per second.
                if now - last_print >= 0.25:
                    print(f"  sim={sim:+.3f}  {'MATCH' if is_match else 'NO MATCH'}")
                    last_print = now
            else:
                empty_frames += 1
                cv2.putText(frame, "no face detected", (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                if now - last_print >= 0.25:
                    print("  (no face detected)")
                    last_print = now
            cv2.imshow(WINDOW_TITLE, frame)
            if (cv2.waitKey(1) & 0xFF) in (27, ord("q"), ord("Q")):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    # Summary.
    print()
    print("=" * 52)
    print("Live test summary:")
    print(f"  frames with face:   {face_frames}")
    print(f"  frames empty:       {empty_frames}")
    if sims:
        arr = np.asarray(sims, dtype=np.float32)
        sim_min = float(arr.min())
        sim_max = float(arr.max())
        sim_mean = float(arr.mean())
        match_rate = float((arr >= config.MATCH_THRESHOLD).mean())
        print(f"  similarity min:     {sim_min:+.3f}")
        print(f"  similarity max:     {sim_max:+.3f}")
        print(f"  similarity mean:    {sim_mean:+.3f}")
        print(f"  match rate:         {match_rate * 100:.1f}%  (threshold {config.MATCH_THRESHOLD})")
        if sim_min < config.MATCH_THRESHOLD:
            gap = config.MATCH_THRESHOLD - sim_min
            print()
            print(f"  WARNING: minimum similarity ({sim_min:+.3f}) is BELOW threshold")
            print(f"           ({config.MATCH_THRESHOLD}) by {gap:.3f}. The watcher would see a few")
            print("           'stranger' frames at this angle, which could contribute to")
            print("           a false lock. Consider lowering MATCH_THRESHOLD in config.py")
            print("           or re-enrolling with more of this angle in the snapshots.")
        else:
            print()
            print("  OK: minimum similarity is at or above threshold. Good to go.")
    else:
        print("  (no face was detected in any frame of the test)")
    print("=" * 52)


def _list_cameras() -> int:
    """Print DirectShow camera devices with their indices. Requires pygrabber."""
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        print("pygrabber not installed. Install it with:")
        print("    pip install pygrabber")
        return 1
    devices = FilterGraph().get_input_devices()
    if not devices:
        print("No DirectShow cameras found.")
        return 1
    print("DirectShow cameras (use the index with --camera N or CAMERA_INDEX in config.py):")
    for i, name in enumerate(devices):
        marker = "  <-- current default" if i == config.CAMERA_INDEX else ""
        print(f"  {i}: {name}{marker}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll your face for webcam auto-lock.")
    parser.add_argument(
        "--camera", type=int, default=config.CAMERA_INDEX,
        help=f"Camera device index (default: {config.CAMERA_INDEX})",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run a 10-second live match test after saving.",
    )
    parser.add_argument(
        "--test-only", action="store_true",
        help="Skip capture; load the existing encoding and run the live match test. "
             "Useful for verifying threshold at your working angle without re-enrolling.",
    )
    parser.add_argument(
        "--list-cameras", action="store_true",
        help="List DirectShow cameras with their indices, then exit.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.list_cameras:
        return _list_cameras()
    if args.test_only:
        if not config.ENCODING_FILE.exists():
            print(f"No encoding at {config.ENCODING_FILE}. Run enroll.py first.")
            return 1
        print("Loading face recognition model...")
        face_engine.preload()
        print("Model ready.")
        enc = np.load(config.ENCODING_FILE).astype(np.float32)
        # Defensive re-normalize in case the file was written pre-normalize.
        norm = float(np.linalg.norm(enc))
        if norm > 0:
            enc = enc / norm
        _live_test(args.camera, enc)
        return 0
    return enroll(args.camera, args.test)


if __name__ == "__main__":
    sys.exit(main())
