#!/usr/bin/env python3
"""Recognition + liveness — the routine the daemon runs to answer "is this me?".

Opens the camera and, within a time budget, requires BOTH:
  1. a liveness challenge (blink + a random head turn) to be completed, and
  2. the face to match the enrollment for K of N frames.

Liveness is required (a printed photo blinks/turns for nobody), matching the
user's "liveness required" decision. Recognition alone, without passing liveness,
never succeeds here.

Daemon protocol — one line on **stdout**, then exit (mirrors gui/auth_prompt.py):

    match         # live + recognised   (exit 0)
    nomatch       # live but not you     (exit 1)
    noface        # never saw a usable face / camera failed (exit 3)
    nolive        # face seen but liveness not proven in time (exit 4)

stderr carries human progress/challenge text (safe for the daemon to log).
Camera-dependent; run `face/probe_env.py` first. See face/README.md.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import build_engine  # noqa: E402
from liveness import LivenessVerifier, Status, random_challenge  # noqa: E402
from matcher import Enrollment, Matcher, MatchAccumulator  # noqa: E402


def emit(result: str, code: int) -> int:
    sys.stdout.write(result + "\n")
    sys.stdout.flush()
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="AppLocker face recognition + liveness")
    ap.add_argument("--enrollment", default=os.path.expanduser("~/.config/applocker/owner.face"))
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=15.0, help="overall budget (s)")
    ap.add_argument("--k", type=int, default=3, help="matching frames required")
    ap.add_argument("--n", type=int, default=5, help="sliding window size")
    ap.add_argument("--no-liveness", action="store_true",
                    help="skip the liveness challenge (NOT for login; testing only)")
    ap.add_argument("--debug", action="store_true",
                    help="print per-frame detector readings (face/eyes/yaw) to stderr")
    args = ap.parse_args()

    try:
        enrollment = Enrollment.load(args.enrollment)
    except (OSError, ValueError) as e:
        print(f"error: cannot load enrollment {args.enrollment}: {e}", file=sys.stderr)
        return emit("noface", 3)

    import cv2

    engine = build_engine()
    if engine.name != enrollment.backend:
        print(f"warning: enrollment backend {enrollment.backend!r} != engine "
              f"{engine.name!r}; embeddings may not compare well", file=sys.stderr)

    matcher = Matcher(enrollment)
    acc = MatchAccumulator(k=args.k, n=args.n)
    challenge = random_challenge(random.Random())
    live = LivenessVerifier(challenge)
    liveness_done = args.no_liveness
    if not args.no_liveness:
        print("Liveness: " + " then ".join(a.human() for a in challenge), file=sys.stderr)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"error: cannot open camera {args.camera}", file=sys.stderr)
        return emit("noface", 3)

    saw_face = False
    start = time.time()
    last_dbg = 0.0
    try:
        while time.time() - start < args.timeout:
            ok, frame = cap.read()
            if not ok:
                continue
            t = time.time() - start

            obs = engine.measure(frame)
            saw_face = saw_face or obs.face_found

            if args.debug and t - last_dbg >= 0.25:
                last_dbg = t
                step = live.current.value if live.current else "-"
                yaw = f"{obs.yaw:+.2f}" if obs.yaw is not None else "  ?  "
                print(f"  t={t:5.1f} face={int(obs.face_found)} "
                      f"eyes={obs.eyes_open} yaw={yaw} step={step}", file=sys.stderr)

            # Phase 1: prove liveness.
            if not liveness_done:
                st = live.update(obs, t)
                if st is Status.PASSED:
                    liveness_done = True
                    print("liveness: passed", file=sys.stderr)
                elif st is Status.FAILED:
                    print(f"liveness: failed ({live.reason})", file=sys.stderr)
                    return emit("nolive", 4)
                # keep going; don't try to match until we're live
                continue

            # Phase 2: recognise, debounced over K of N frames.
            emb = engine.embed(frame)
            if emb is None:
                continue
            matched = matcher.matches(emb)
            if acc.feed(matched):
                return emit("match", 0)
            if acc.rejected:
                return emit("nomatch", 1)
    finally:
        cap.release()

    # Timed out — report the most informative reason.
    if not saw_face:
        return emit("noface", 3)
    if not liveness_done:
        return emit("nolive", 4)
    return emit("nomatch", 1)


if __name__ == "__main__":
    sys.exit(main())
