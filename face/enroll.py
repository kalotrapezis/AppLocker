#!/usr/bin/env python3
"""Enrollment — capture several frames of the owner's face and save embeddings.

Run this once, on the real machine, to teach AppLocker your face:

    python3 face/enroll.py --user "$USER" --out ~/.config/applocker/owner.face

It opens the camera, waits until a face is clearly detected, and captures N
embeddings a moment apart (move your head slightly between prompts for angle
variety). The result is written to a `*.face` file (0600) — enrolled biometrics,
never committed (see .gitignore).

Camera-dependent, so it has no offline test; run `face/probe_env.py` first.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Allow running as `python3 face/enroll.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import build_engine  # noqa: E402
from matcher import Enrollment  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="AppLocker face enrollment")
    ap.add_argument("--user", default=os.environ.get("USER", "owner"))
    ap.add_argument("--out", default=os.path.expanduser("~/.config/applocker/owner.face"))
    ap.add_argument("--samples", type=int, default=8, help="embeddings to capture")
    ap.add_argument("--camera", type=int, default=0, help="/dev/videoN index")
    ap.add_argument("--threshold", type=float, default=None,
                    help="override the backend's default match threshold")
    args = ap.parse_args()

    import cv2

    engine = build_engine()
    print(f"engine: {engine.name} (dim={engine.dim})")

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"error: cannot open camera {args.camera}", file=sys.stderr)
        return 2

    embeddings = []
    print(f"Capturing {args.samples} samples — look at the camera, "
          "shift your head slightly between beeps.")
    try:
        last = 0.0
        while len(embeddings) < args.samples:
            ok, frame = cap.read()
            if not ok:
                continue
            now = time.time()
            if now - last < 0.6:  # space samples out for angle variety
                continue
            emb = engine.embed(frame)
            if emb is None:
                print("  ...no face, adjust position")
                continue
            embeddings.append(emb)
            last = now
            print(f"  captured {len(embeddings)}/{args.samples}")
    finally:
        cap.release()

    if len(embeddings) < max(3, args.samples // 2):
        print("error: too few good samples; try better lighting", file=sys.stderr)
        return 2

    threshold = args.threshold if args.threshold is not None else engine.default_threshold
    enr = Enrollment(
        user=args.user,
        dim=engine.dim,
        threshold=threshold,
        embeddings=embeddings,
        backend=engine.name,
    )
    enr.save(args.out)
    print(f"enrolled {len(embeddings)} samples for {args.user!r} -> {args.out} "
          f"(threshold={threshold}, backend={engine.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
