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


import os

#: SFace ONNX model (OpenCV Zoo) for the strong embedding backend.
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"
#: YuNet face detector, the **2022mar** version — the 2023mar one needs
#: OpenCV ≥ 4.7, but 2022mar loads and runs on Ubuntu's 4.6. YuNet handles
#: tilted / partially occluded faces that Haar misses completely (e.g. head
#: resting on a hand), and its landmarks enable SFace's proper alignCrop.
YUNET_MODEL = "face_detection_yunet_2022mar.onnx"


def model_dir() -> str:
    """Where the YuNet/SFace ONNX models live ($APPLOCKER_MODELS or the default).

    When running as root (the daemon), "~" is /root — but the models live in the
    *invoking* user's home, so prefer $SUDO_USER's home when the default path
    doesn't exist. Otherwise SFace silently degrades to the weak haar backend.
    """
    env = os.environ.get("APPLOCKER_MODELS")
    if env:
        return os.path.expanduser(env)
    default = os.path.expanduser("~/.config/applocker/models")
    if not os.path.isdir(default):
        user = os.environ.get("SUDO_USER")
        if user:
            sudo_path = f"/home/{user}/.config/applocker/models"
            if os.path.isdir(sudo_path):
                return sudo_path
    return default


def build_engine() -> FaceEngine:
    """Construct the best engine the environment supports:

      1. **SFace** (strong 128-d embeddings) if OpenCV has the YuNet+SFace APIs
         AND both ONNX models are present — this is the recommended unlock model.
      2. **Haar-pixel v0** otherwise (works with zero downloads, but weak).

    Recognition strength only affects *unlocking*; the presence/attention tier is
    intentionally lenient and identity-blind regardless (see attention.py).
    """
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "OpenCV not installed. Run:\n"
            "  sudo apt install python3-opencv python3-numpy opencv-data\n"
            f"(import error: {e})"
        )

    md = model_dir()
    sf = os.path.join(md, SFACE_MODEL)
    if hasattr(cv2, "FaceRecognizerSF") and os.path.exists(sf):
        try:
            return SFaceEngine(sf)
        except Exception as e:  # bad model file, API mismatch — fall back loudly
            import sys
            sys.stderr.write(f"SFace unavailable ({e}); falling back to Haar-pixel v0.\n")
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
        # Lenient params: smaller minSize and fewer neighbours catch faces that
        # are tilted (camera above / user looking down) or partially turned.
        faces = self._face.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4,
                                             minSize=(60, 60))
        return self._largest(faces)

    def _detect_profile_yaw(self, gray):
        """Coarse yaw from the profile cascade: it fires on left-facing profiles;
        flip the image to catch right-facing ones. Returns -1.0 / +1.0 / None.
        Profile detection is flaky, so params are lenient."""
        left = self._profile.detectMultiScale(gray, 1.1, 3, minSize=(60, 60))
        if len(left):
            return -1.0
        flipped = self.cv2.flip(gray, 1)
        right = self._profile.detectMultiScale(flipped, 1.1, 3, minSize=(60, 60))
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

    def face_box(self, frame):
        """The largest frontal-face bounding box (x, y, w, h), or None. Shared
        with SFaceEngine so both backends detect the same way."""
        return self._detect_frontal(self._gray(frame))

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


class SFaceEngine(FaceEngine):
    """Strong recognition backend — the recommended unlock model.

    - Detection: **YuNet 2022mar** when its model file is present (loads on
      OpenCV 4.6, robust to tilt/occlusion, provides landmarks for alignment),
      else **Haar** (via an internal `HaarPixelEngine`), which works with zero
      downloads but only on upright frontal faces.
    - Embedding: **SFace** (`cv2.FaceRecognizerSF`) — a 128-d face descriptor.
      With YuNet the crop is properly 5-point aligned (`alignCrop`); with Haar
      it's a plain resized crop. SFace's canonical cosine threshold is ~0.363.

    ⚠️ The two detection paths produce *different* embeddings for the same face
    (aligned vs unaligned crop) — after the YuNet model is added, re-enrol.

    Presence/liveness (`measure`) is delegated to Haar signals, with YuNet
    backing up `face_found` — the attention tier stays identity-blind.
    """

    name = "sface"
    dim = 128
    default_threshold = 0.363  # SFace's recommended cosine threshold

    def __init__(self, sface_path: str):
        import cv2

        self.cv2 = cv2
        self._haar = HaarPixelEngine()  # eyes/yaw signals + no-download fallback
        self._recognizer = cv2.FaceRecognizerSF.create(sface_path, "")
        self._yunet = self._try_yunet()

    def _try_yunet(self):
        """Load YuNet if its model exists AND actually runs on this OpenCV
        (verified with a dummy detect — 4.6 rejects newer models at run time)."""
        import numpy as np

        path = os.path.join(model_dir(), YUNET_MODEL)
        if not (hasattr(self.cv2, "FaceDetectorYN") and os.path.exists(path)):
            return None
        try:
            det = self.cv2.FaceDetectorYN.create(path, "", (320, 240), 0.7)
            det.setInputSize((320, 240))
            det.detect(np.zeros((240, 320, 3), dtype=np.uint8))
            return det
        except self.cv2.error as e:
            import sys
            sys.stderr.write(f"YuNet unavailable ({e}); detecting with Haar.\n")
            return None

    def _yunet_face(self, frame):
        """Best YuNet detection row (box + landmarks) for the frame, or None."""
        if self._yunet is None:
            return None
        h, w = frame.shape[:2]
        self._yunet.setInputSize((w, h))
        _, faces = self._yunet.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        return max(faces, key=lambda f: float(f[-1]))

    @staticmethod
    def _landmark_yaw(face) -> Optional[float]:
        """Continuous yaw from YuNet's landmarks: how far the nose sits from the
        eye midpoint, normalised by the inter-eye distance. 0 ≈ frontal; the
        sign convention matches liveness.py (turn to *your* left → negative:
        in the un-mirrored camera image your nose moves image-right, so we
        negate). A solid deliberate turn reads ~±0.4-0.8; the liveness
        threshold is 0.30."""
        re_x, re_y = float(face[4]), float(face[5])   # right eye
        le_x, le_y = float(face[6]), float(face[7])   # left eye
        nose_x = float(face[8])
        eye_dist = ((le_x - re_x) ** 2 + (le_y - re_y) ** 2) ** 0.5
        if eye_dist < 1.0:
            return None
        mid_x = (re_x + le_x) / 2.0
        return (mid_x - nose_x) / eye_dist

    def measure(self, frame) -> FrameObservation:
        # YuNet first: robust detection AND a real yaw signal from landmarks
        # (Haar's profile-cascade yaw was too noisy for the turn challenge).
        face = self._yunet_face(frame)
        if face is not None:
            obs = self._haar.measure(frame)  # eyes signal, if Haar also sees it
            return FrameObservation(
                face_found=True,
                eyes_open=obs.eyes_open if obs.face_found else None,
                yaw=self._landmark_yaw(face),
            )
        return self._haar.measure(frame)

    def embed(self, frame) -> Optional[List[float]]:
        face = self._yunet_face(frame)
        if face is not None:
            crop = self._recognizer.alignCrop(frame, face)  # 5-point aligned
        else:
            box = self._haar.face_box(frame)
            if box is None:
                return None
            x, y, w, h = box
            crop = frame[y:y + h, x:x + w]  # colour crop (SFace wants BGR)
            if crop.size == 0:
                return None
            crop = self.cv2.resize(crop, (112, 112))
        feat = self._recognizer.feature(crop)  # shape (1, 128)
        return [float(v) for v in feat.flatten()]
