"""Face engine — pixels → signals. The one OpenCV-dependent module.

Turns a BGR camera frame into:
  - a FrameObservation (face_found / eyes_open / yaw) for liveness.py, and
  - an embedding vector for matcher.py.

`cv2`/`numpy` are imported **lazily** (inside `build_engine()`), so this file
imports fine on a box without them — liveness.py, matcher.py and the env probe
never pull OpenCV in. Only constructing an engine requires it.

Backend reality on this machine (Mint 22 / Ubuntu noble, no pip/network):
  - `python3-opencv` + `opencv-data` give Haar cascades → detection, a coarse
    eyes-open signal, and a coarse profile-based yaw. Good enough to drive the
    blink/turn liveness challenge, zero downloads.
  - Recognition embeddings are the weak spot without dlib/MediaPipe. This engine
    ships a **pixel-template** embedding (aligned, equalised, flattened grayscale
    crop) so the whole pipeline *runs* end-to-end after a plain apt install. It
    is deliberately a v0 placeholder — lighting/pose sensitive — to be swapped
    for OpenCV's YuNet+SFace ONNX models once those two files can be fetched
    (see face/README.md → "Upgrading recognition"). The `Matcher`/enrollment
    format doesn't change when we upgrade; only this class does.

⚠️ The code below the interface is exercised only on real hardware with a camera
and OpenCV installed; it has no offline unit tests. Run `face/probe_env.py`
first to confirm the environment, then `enroll.py` / `recognize.py`.
"""

from __future__ import annotations

import abc
from typing import List, Optional

from liveness import FrameObservation


class FaceEngine(abc.ABC):
    name: str = "abstract"
    dim: int = 0
    #: A reasonable default cosine threshold for this backend's embeddings,
    #: used at enrollment time unless the user overrides it.
    default_threshold: float = 0.9

    @abc.abstractmethod
    def measure(self, frame) -> FrameObservation:
        """Detect the face and derive liveness signals for one frame."""

    @abc.abstractmethod
    def embed(self, frame) -> Optional[List[float]]:
        """Return an embedding for the primary face, or None if no usable face."""


def build_engine() -> FaceEngine:
    """Construct the best engine the environment supports. Raises RuntimeError
    with actionable text if OpenCV/cascades are missing."""
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "OpenCV not installed. Run:\n"
            "  sudo apt install python3-opencv python3-numpy opencv-data\n"
            f"(import error: {e})"
        )
    return HaarPixelEngine()


def haarcascade_dir(cv2) -> str:
    """Locate the Haar cascade directory. pip's `opencv-python` exposes
    `cv2.data.haarcascades`, but Ubuntu's apt `python3-opencv` does NOT — its
    cascades ship in the `opencv-data` package under /usr/share. Try both."""
    import os

    d = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if d and os.path.isdir(d):
        return d
    for cand in (
        "/usr/share/opencv4/haarcascades/",
        "/usr/share/opencv/haarcascades/",
        "/usr/share/OpenCV/haarcascades/",
    ):
        if os.path.isdir(cand):
            return cand
    raise RuntimeError(
        "Haar cascades not found — install them with: sudo apt install opencv-data"
    )


class HaarPixelEngine(FaceEngine):
    """Haar-cascade detection + pixel-template embedding. See module docstring."""

    name = "haar-pixel-v0"
    dim = 100 * 100
    default_threshold = 0.92  # pixel templates are high-correlation; keep it tight

    def __init__(self, crop: int = 100):
        import os

        import cv2

        self.cv2 = cv2
        self.crop = crop
        base = haarcascade_dir(cv2)
        self._face = cv2.CascadeClassifier(os.path.join(base, "haarcascade_frontalface_default.xml"))
        self._eye = cv2.CascadeClassifier(os.path.join(base, "haarcascade_eye.xml"))
        self._profile = cv2.CascadeClassifier(os.path.join(base, "haarcascade_profileface.xml"))
        for name, c in (("frontalface", self._face), ("eye", self._eye),
                        ("profileface", self._profile)):
            if c.empty():
                raise RuntimeError(
                    f"Haar cascade '{name}' failed to load — install `opencv-data`."
                )

    # ── internals ────────────────────────────────────────────────────────────

    def _gray(self, frame):
        g = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        return self.cv2.equalizeHist(g)

    def _largest(self, rects):
        return max(rects, key=lambda r: r[2] * r[3]) if len(rects) else None

    def _detect_frontal(self, gray):
        faces = self._face.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                             minSize=(80, 80))
        return self._largest(faces)

    def _detect_profile_yaw(self, gray):
        """Coarse yaw from the profile cascade: it fires on left-facing profiles;
        flip the image to catch right-facing ones. Returns -1.0 / +1.0 / None."""
        left = self._profile.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        if len(left):
            return -1.0
        flipped = self.cv2.flip(gray, 1)
        right = self._profile.detectMultiScale(flipped, 1.1, 5, minSize=(80, 80))
        if len(right):
            return 1.0
        return None

    # ── FaceEngine ───────────────────────────────────────────────────────────

    def measure(self, frame) -> FrameObservation:
        gray = self._gray(frame)
        face = self._detect_frontal(gray)

        if face is not None:
            x, y, w, h = face
            # Eyes live in the upper ~60% of the face box.
            roi = gray[y:y + int(h * 0.6), x:x + w]
            eyes = self._eye.detectMultiScale(roi, 1.1, 6, minSize=(20, 20))
            eyes_open = len(eyes) >= 1
            # A cleanly-detected frontal face is, by definition, near centre yaw.
            return FrameObservation(face_found=True, eyes_open=eyes_open, yaw=0.0)

        # No frontal face — maybe the head is turned. Try the profile cascade.
        yaw = self._detect_profile_yaw(gray)
        if yaw is not None:
            # Eyes can't be reliably read on a profile; leave it unknown.
            return FrameObservation(face_found=True, eyes_open=None, yaw=yaw)

        return FrameObservation(face_found=False)

    def embed(self, frame) -> Optional[List[float]]:
        gray = self._gray(frame)
        face = self._detect_frontal(gray)
        if face is None:
            return None
        x, y, w, h = face
        crop = gray[y:y + h, x:x + w]
        crop = self.cv2.resize(crop, (self.crop, self.crop))
        crop = self.cv2.equalizeHist(crop)
        # Flatten to a plain Python float list; matcher.l2_normalize handles scale.
        return [float(px) for px in crop.flatten()]
