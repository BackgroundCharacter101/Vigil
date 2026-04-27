"""InsightFace wrapper for face detection + recognition.

Uses RetinaFace (detection, pose-robust — works on profiles and angled
views that frontal-only detectors like dlib HOG miss) and ArcFace
(512-d recognition embedding).

Matching uses COSINE SIMILARITY of L2-normalized embeddings:
  +1.0 = identical face
   0.0 = unrelated
  -1.0 = opposite (unusual)

A similarity of ~0.5 is a solid match; ~0.3 is marginal; below 0.2 is
almost certainly a different person.

This is the opposite direction from dlib's face_distance, where LOWER
was better. Watcher code uses `similarity >= MATCH_THRESHOLD` accordingly.

The underlying FaceAnalysis object is heavy (loads ONNX models into RAM)
so it's lazily constructed on first use and then reused. First-ever run
downloads the `buffalo_l` model set (~280MB) to `~/.insightface/models/`.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

import config

log = logging.getLogger(__name__)

# Module-level singleton so all callers share one FaceAnalysis instance.
_app = None


class DetectedFace:
    """A single face detection + its 512-d L2-normalized embedding."""

    __slots__ = ("bbox", "embedding", "det_score")

    def __init__(
        self,
        bbox: Tuple[int, int, int, int],
        embedding: np.ndarray,
        det_score: float,
    ) -> None:
        self.bbox = bbox  # (x1, y1, x2, y2) in ORIGINAL frame pixel coordinates
        self.embedding = embedding  # shape (512,), L2-normalized
        self.det_score = det_score

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bbox
        return max(0, x2 - x1) * max(0, y2 - y1)


def _get_app():
    """Lazy-load the FaceAnalysis pipeline. First call is slow.

    Thread-pool sizing is handled in `main._install_ort_thread_cap()` via
    a monkey-patch on `onnxruntime.InferenceSession.__init__` -- we cannot
    do it here because InsightFace constructs the sessions inside
    `app.prepare()` and ORT's thread pools are immutable once allocated.
    See main.py for the full rationale.
    """
    global _app
    if _app is not None:
        return _app
    try:
        import onnxruntime as ort  # type: ignore
        ort.set_default_logger_severity(3)  # quiet
    except Exception:
        pass

    from insightface.app import FaceAnalysis  # type: ignore

    log.info("Loading InsightFace model (first run downloads ~280MB)...")
    # allowed_modules restricts the pipeline to just what we need — skip
    # gender/age and the 68-landmark model to save memory and startup time.
    app = FaceAnalysis(
        name="buffalo_l",
        providers=["CPUExecutionProvider"],
        allowed_modules=["detection", "recognition"],
    )
    # det_size balances accuracy vs speed. The user sitting 0.3-1.0m from
    # a laptop webcam is a very large face, so 320x320 is plenty and is
    # ~3x faster than the 640x640 default. Configurable via config.py.
    size = int(config.DETECTION_SIZE)
    app.prepare(ctx_id=0, det_size=(size, size))

    _app = app
    log.info("InsightFace model loaded (det_size=%dx%d)", size, size)
    return _app


def preload() -> None:
    """Trigger model load eagerly. Useful at startup so the first frame
    doesn't pay a multi-second initialization cost."""
    _get_app()


def detect_faces(frame_bgr: np.ndarray) -> List[DetectedFace]:
    """Detect all faces in a BGR frame. Returns an empty list if none."""
    app = _get_app()
    raw = app.get(frame_bgr)
    result: List[DetectedFace] = []
    for f in raw:
        x1, y1, x2, y2 = [int(v) for v in f.bbox]
        emb = np.asarray(f.normed_embedding, dtype=np.float32)
        result.append(
            DetectedFace(
                bbox=(x1, y1, x2, y2),
                embedding=emb,
                det_score=float(f.det_score),
            )
        )
    return result


def best_face(faces: List[DetectedFace]) -> Optional[DetectedFace]:
    """Return the largest-area face (heuristically the one closest to the
    camera — typically the user). Returns None if the list is empty."""
    if not faces:
        return None
    return max(faces, key=lambda f: f.area)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two L2-normalized vectors == their dot product.

    Inputs must already be normalized (InsightFace's `normed_embedding` is).
    """
    return float(np.dot(a, b))


def best_similarity(known: np.ndarray, faces: List[DetectedFace]) -> float:
    """Return the highest similarity between `known` and any face embedding
    in `faces`. Returns -1.0 for an empty list (i.e. no match possible)."""
    if not faces:
        return -1.0
    return max(cosine_similarity(known, f.embedding) for f in faces)
