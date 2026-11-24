#!/usr/bin/env python3
"""Recognition + liveness — the routine the daemon runs to answer "is this me?".

Opens the camera and, within a time budget, requires BOTH:
  1. the liveness challenge (turn left / turn right, random order, each from a
     centred start) to be completed, and
  2. the face to match an enrolled profile for K of N frames.

Recognition alone, without passing liveness, never succeeds here (the login
tier's rule; the app-gate passes --no-liveness for frictionless unlocks).

Daemon protocol — one line on **stdout**, then exit (mirrors gui/auth_prompt.py):

    match         # live + recognised   (exit 0)
    nomatch       # live but not you     (exit 1)
    noface        # never saw a usable face / camera failed (exit 3)
    nolive        # face seen but liveness not proven in time (exit 4)

stderr carries human progress/challenge text (safe for the daemon to log).

`--ui` (or APPLOCKER_UI=1) additionally shows the guided camera window — the
same design as enrollment: live preview, one instruction chip at a time, a ✓
per completed step. If GTK or a display is unavailable it silently falls back
to headless, so the PAM/daemon callers can always set it safely.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import build_engine  # noqa: E402
from liveness import LivenessVerifier, Status, turn_challenge  # noqa: E402
from matcher import (  # noqa: E402
    MatchAccumulator, Matcher, default_faces_dir, list_profiles, pooled,
)


def emit(result: str, code: int) -> int:
    sys.stdout.write(result + "\n")
    sys.stdout.flush()
    return code


class HeadlessNotify:
    """Progress on stderr — the daemon/PAM log surface."""

    def __call__(self, kind: str, **kw):
        if kind == "challenge":
            print(f"Liveness: {kw['text']}", file=sys.stderr)
        elif kind == "step_done":
            print(f"  ✓ {kw['n']}/{kw['total']}", file=sys.stderr)
        elif kind == "status":
            print(kw["text"], file=sys.stderr)
        # 'frame' and 'result' need no headless output


def routine(args, notify) -> tuple:
    """The full camera routine. Returns (word, exit_code); `notify` receives
    ('frame', frame=bgr), ('step', text/n/total), ('step_done'), ('status',
    text) and ('result', word) events along the way."""
    import cv2

    engine = build_engine()

    # Load every enrolled profile (faces dir + optional legacy file) and pool the
    # ones matching this engine's backend, so we match against ANY enrolled face.
    faces_dir = args.faces_dir or default_faces_dir()
    legacy = args.enrollment if args.enrollment is not None else ""
    profiles = list_profiles(faces_dir=faces_dir, legacy=legacy)
    if not profiles:
        notify("status", text=f"error: no enrolled faces in {faces_dir}")
        return "noface", 3
    try:
        combined = pooled([e for _, e in profiles], backend=engine.name)
    except ValueError:
        names = ", ".join(e.backend for _, e in profiles)
        notify("status", text=f"error: no profiles match engine {engine.name!r} "
               f"(have: {names}); re-enrol after switching backends")
        return "noface", 3
    notify("status", text=f"matching against {len(profiles)} profile(s): "
           + ", ".join(e.display_name() for _, e in profiles))

    mtch = Matcher(combined)
    acc = MatchAccumulator(k=args.k, n=args.n)
    # Turn-only challenge (no blink — undetectable on plain webcams); yaw comes
    # from YuNet landmarks, see engine.SFaceEngine._landmark_yaw.
    challenge = turn_challenge(random.Random())
    live = LivenessVerifier(challenge)
    liveness_done = args.no_liveness
    announced = False
    steps_done = 0

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        notify("status", text=f"error: cannot open camera {args.camera}")
        return "noface", 3

    saw_face = False
    start = time.time()
    last_dbg = 0.0
    try:
        while time.time() - start < args.timeout:
            ok, frame = cap.read()
            if not ok:
                continue
            t = time.time() - start
            notify("frame", frame=frame)

            obs = engine.measure(frame)
            saw_face = saw_face or obs.face_found

            if args.debug and t - last_dbg >= 0.25:
                last_dbg = t
                step = live.current.value if live.current else "-"
                yaw = f"{obs.yaw:+.2f}" if obs.yaw is not None else "  ?  "
                print(f"  t={t:5.1f} face={int(obs.face_found)} "
                      f"eyes={obs.eyes_open} yaw={yaw} step={step}", file=sys.stderr)

            # Phase 1: prove liveness. One human instruction ("side to side")
            # covers the whole randomized challenge — users wiggle rather than
            # read (real-hardware feedback), and wiggling is valid proof: a
            # photo can't do it, and each step still arms from centre.
            if not liveness_done:
                if not announced:
                    notify("challenge",
                           text="slowly turn your head side to side",
                           total=len(challenge))
                    announced = True
                st = live.update(obs, t)
                if st is not Status.FAILED and live.completed > steps_done:
                    steps_done = live.completed
                    notify("step_done", n=steps_done, total=len(challenge))
                if st is Status.PASSED:
                    liveness_done = True
                    notify("status", text="liveness: passed")
                elif st is Status.FAILED:
                    notify("status", text=f"liveness: failed ({live.reason})")
                    return "nolive", 4
                # keep going; don't try to match until we're live
                continue

            # Phase 2: recognise, debounced over K of N frames.
            emb = engine.embed(frame)
            if emb is None:
                continue
            if acc.feed(mtch.matches(emb)):
                return "match", 0
            if acc.rejected:
                return "nomatch", 1
    finally:
        cap.release()

    # Timed out — report the most informative reason.
    if not saw_face:
        return "noface", 3
    if not liveness_done:
        return "nolive", 4
    return "nomatch", 1


def run_with_ui(args) -> int:
    """The guided window (same design as enrollment: preview + instruction chip
    + progress). Returns the routine's exit code; raises only before the camera
    starts, so callers can fall back to headless."""
    import threading

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    class UnlockWindow(Gtk.Window):
        def __init__(self):
            super().__init__(title="AppLocker")
            self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
            self.set_keep_above(True)
            self.set_resizable(False)
            self.set_border_width(14)
            self._frame = None
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.add(box)
            self.video = Gtk.DrawingArea()
            self.video.set_size_request(400, 280)
            self.video.connect("draw", self._on_draw)
            frame = Gtk.Frame()
            frame.add(self.video)
            box.pack_start(frame, False, False, 0)
            self.instruction = Gtk.Label(label="Looking for you…")
            chip = Gtk.Button()
            chip.add(self.instruction)
            chip.set_sensitive(False)
            chip.get_style_context().add_class("suggested-action")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            row.pack_start(chip, False, False, 0)
            box.pack_start(row, False, False, 0)

        def _on_draw(self, area, cr):
            w, h = area.get_allocated_width(), area.get_allocated_height()
            cr.set_source_rgb(0.08, 0.08, 0.08)
            cr.paint()
            f = self._frame
            if f is not None:
                fh, fw = f.shape[:2]
                # new_from_bytes keeps the pixel buffer alive (GBytes); new_from_data
                # does not, so the freed buffer paints blank — the "no preview" bug.
                data = GLib.Bytes.new(f.tobytes())
                pb = GdkPixbuf.Pixbuf.new_from_bytes(
                    data, GdkPixbuf.Colorspace.RGB, False, 8, fw, fh, fw * 3)
                scale = min(w / fw, h / fh)
                cr.save()
                cr.translate((w - fw * scale) / 2, (h - fh * scale) / 2)
                cr.scale(scale, scale)
                Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
                cr.paint()
                cr.restore()
            return False

    win = UnlockWindow()
    win.show_all()  # raises inside Gtk if there's no display — caller catches

    result = {"word": "noface", "code": 3}
    headless = HeadlessNotify()  # keep stderr logging alongside the window

    def notify(kind, **kw):
        headless(kind, **kw)
        if kind == "frame":
            import cv2
            import numpy as np
            rgb = np.ascontiguousarray(
                cv2.cvtColor(cv2.flip(kw["frame"], 1), cv2.COLOR_BGR2RGB))
            win._frame = rgb
            GLib.idle_add(win.video.queue_draw)
        elif kind == "challenge":
            GLib.idle_add(win.instruction.set_text,
                          "↔  Slowly turn your head side to side")
        elif kind == "step_done":
            ticks = "✓" * kw["n"] + "·" * (kw["total"] - kw["n"])
            text = (f"{ticks}  Hold still…" if kw["n"] == kw["total"]
                    else f"{ticks}  keep going…")
            GLib.idle_add(win.instruction.set_text, text)
        elif kind == "result":
            ok = kw["word"] == "match"
            GLib.idle_add(win.instruction.set_text,
                          "✓  Welcome back" if ok else "✕  Not recognised")
            GLib.timeout_add(900, Gtk.main_quit)

    def worker():
        word, code = routine(ARGS, notify)
        result["word"], result["code"] = word, code
        notify("result", word=word)

    global ARGS
    threading.Thread(target=worker, daemon=True).start()
    Gtk.main()
    return emit(result["word"], result["code"])


def main() -> int:
    global ARGS
    ap = argparse.ArgumentParser(description="AppLocker face recognition + liveness")
    ap.add_argument("--faces-dir", default=None,
                    help="directory of named face profiles (matches against any)")
    ap.add_argument("--enrollment", default=None,
                    help="also include this single profile file (legacy)")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=15.0, help="overall budget (s)")
    ap.add_argument("--k", type=int, default=3, help="matching frames required")
    ap.add_argument("--n", type=int, default=5, help="sliding window size")
    ap.add_argument("--no-liveness", action="store_true",
                    help="skip the liveness challenge (NOT for login; testing only)")
    ap.add_argument("--ui", action="store_true",
                    help="show the guided camera window (falls back to headless)")
    ap.add_argument("--debug", action="store_true",
                    help="print per-frame detector readings (face/eyes/yaw) to stderr")
    ap.add_argument("--camera-priority", default=None,
                    help="camera arbitration level: lockscreen|app|file|presence "
                         "(also read from $APPLOCKER_CAMERA_PRIORITY)")
    ARGS = ap.parse_args()

    # Arbitrate the camera against the other AppLocker helpers, at the caller's
    # priority. Unset → run unarbitrated (standalone use / tests). If a higher
    # level is holding the camera we wait out our budget; failing that, 'noface'.
    from cameralock import camera_lock, priority_from_name
    level = priority_from_name(ARGS.camera_priority
                               or os.environ.get("APPLOCKER_CAMERA_PRIORITY"))
    if level is None:
        return _run(ARGS)
    with camera_lock(level, timeout=ARGS.timeout + 5.0) as got:
        if not got:
            print("camera busy (out-prioritised) — giving up this round",
                  file=sys.stderr)
            return emit("noface", 3)
        return _run(ARGS)


def _run(args) -> int:
    if args.ui or os.environ.get("APPLOCKER_UI") == "1":
        try:
            return run_with_ui(args)
        except Exception as e:  # no display / no GTK — never block the auth
            print(f"ui unavailable ({e}); running headless", file=sys.stderr)
    word, code = routine(args, HeadlessNotify())
    return emit(word, code)


if __name__ == "__main__":
    sys.exit(main())
