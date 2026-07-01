#!/usr/bin/env python3
"""Environment probe — run AFTER installing OpenCV, BEFORE enrolling.

Reports what the face stack can actually do on this machine, so we choose the
recognition backend from facts, not guesses:

    python3 face/probe_env.py

Checks: numpy/opencv presence + versions, Haar cascades (needed for liveness),
whether the stronger YuNet+SFace ONNX models are importable and present, and
whether the camera opens and yields a frame.
"""

from __future__ import annotations

import os
import sys


def line(label, ok, detail=""):
    mark = "OK  " if ok else "-- "
    print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))


def main() -> int:
    print("AppLocker face environment probe\n")

    try:
        import numpy
        line("numpy", True, numpy.__version__)
    except ImportError:
        line("numpy", False, "sudo apt install python3-numpy")

    try:
        import cv2
    except ImportError:
        line("opencv (cv2)", False, "sudo apt install python3-opencv opencv-data")
        print("\nCannot proceed without OpenCV.")
        return 1
    line("opencv (cv2)", True, cv2.__version__)

    # Haar cascades — the liveness + v0 recognition path. Ubuntu's apt OpenCV
    # lacks cv2.data, so resolve the dir the same way the engine does.
    from engine import haarcascade_dir

    try:
        base = haarcascade_dir(cv2)
        line("haarcascade dir", True, base)
    except RuntimeError as e:
        line("haarcascade dir", False, str(e))
        base = None
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_eye.xml",
                 "haarcascade_profileface.xml"):
        p = os.path.join(base, name) if base else name
        present = bool(base) and os.path.exists(p) and not cv2.CascadeClassifier(p).empty()
        line(f"cascade {name}", present, p if present else "install opencv-data")

    # Stronger recognition path (optional upgrade): YuNet detector + SFace embedder.
    has_yn = hasattr(cv2, "FaceDetectorYN")
    has_sf = hasattr(cv2, "FaceRecognizerSF")
    line("cv2.FaceDetectorYN (YuNet API)", has_yn)
    line("cv2.FaceRecognizerSF (SFace API)", has_sf)
    # contrib LBPH, the other possible recognizer.
    line("cv2.face (contrib LBPH)", hasattr(cv2, "face"))

    # Look for the ONNX model files the YuNet/SFace path needs (not bundled).
    model_dir = os.path.expanduser("~/.config/applocker/models")
    for m in ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"):
        line(f"model {m}", os.path.exists(os.path.join(model_dir, m)),
             f"place in {model_dir}/ to enable the SFace backend")

    # Camera.
    idx = int(os.environ.get("APPLOCKER_CAMERA", "0"))
    cap = cv2.VideoCapture(idx)
    if cap.isOpened():
        ok, frame = cap.read()
        line(f"camera /dev/video{idx}", ok,
             f"frame {frame.shape}" if ok else "opened but no frame")
        cap.release()
    else:
        line(f"camera /dev/video{idx}", False, "cannot open (permissions? in use?)")

    print("\nRecommendation:")
    if has_yn and has_sf:
        print("  YuNet+SFace API present. If the two ONNX models are in place,")
        print("  we can use the strong embedding backend. Otherwise the v0")
        print("  Haar-pixel backend works today with no downloads.")
    else:
        print("  Use the v0 Haar-pixel backend (engine.py). Liveness is fully")
        print("  supported; recognition is coarse until a DNN model is available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
