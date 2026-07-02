#!/usr/bin/env python3
"""Presence watcher — dim when you leave, lock when you're gone (roadmap step 5).

Runs in *your* session (not the daemon): watches the camera for **any** face
(identity-blind, lenient — see attention.py's look-down debounce), and:

    gone ~3s  → dims the screen (translucent overlay, reversible)
    gone ~10s → `loginctl lock-session`

Locking goes through logind, so the daemon's lock listener wipes the app-unlock
cache — manual lock, lid-close, and this watcher all behave identically.

    python3 face/watch_presence.py            # honours `attention` in the config
    python3 face/watch_presence.py --force    # run even if the config says off

Being blind never locks you out of your desk by mistake:
  - camera unavailable (a video call has it) → treated as PRESENT, retry later
  - after LOCK, it waits for the session to unlock (LockedHint) and resumes
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attention import AttentionWatcher, Config, Phase  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def attention_enabled() -> bool:
    """The `attention` key from the daemon's config (default: off)."""
    path = os.environ.get("APPLOCKER_CONFIG", "/etc/applocker/config")
    try:
        with open(path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = (p.strip().lower() for p in line.split("=", 1))
                    if k == "attention":
                        return v in ("on", "true", "1", "yes")
    except OSError:
        pass
    return False


def session_locked() -> bool:
    try:
        out = subprocess.run(
            ["loginctl", "show-session", "", "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


class DimOverlay(Gtk.Window):
    """A fullscreen translucent black window — the "are you still there?" warn.
    Click-through and focus-free, so it never interferes; it just darkens."""

    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        screen = Gdk.Screen.get_default()
        self.set_default_size(screen.get_width(), screen.get_height())
        self.move(0, 0)
        # Real transparency when a compositor is running (Cinnamon: always).
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self._draw)
        self.connect("realize", self._click_through)

    def _draw(self, _w, cr):
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.paint()
        return False

    def _click_through(self, _w):
        try:
            import cairo

            self.get_window().input_shape_combine_region(
                cairo.Region(), 0, 0)  # empty input region = clicks pass through
        except Exception:
            pass  # cosmetic; worst case the overlay eats a click


def main() -> int:
    ap = argparse.ArgumentParser(description="AppLocker presence watcher")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--dim-after", type=float, default=3.0)
    ap.add_argument("--lock-after", type=float, default=10.0)
    ap.add_argument("--fps", type=float, default=2.0, help="camera poll rate")
    ap.add_argument("--force", action="store_true",
                    help="run even if the config has attention off")
    args = ap.parse_args()

    if not args.force and not attention_enabled():
        print("attention is off in the config (enable it in AppLocker settings, "
              "or run with --force)", file=sys.stderr)
        return 2

    import cv2

    from engine import build_engine

    engine = build_engine()
    watcher = AttentionWatcher(Config(dim_after=args.dim_after,
                                      lock_after=args.lock_after))
    overlay = DimOverlay()
    state = {"phase": Phase.PRESENT}

    def set_phase(phase):
        if phase == state["phase"]:
            return
        state["phase"] = phase
        if phase is Phase.DIMMED:
            overlay.show_all()
        else:
            overlay.hide()
        if phase is Phase.LOCKED:
            print("presence lost — locking session", file=sys.stderr)
            subprocess.run(["loginctl", "lock-session"], timeout=10)

    def camera_loop():
        cap = None
        period = 1.0 / max(args.fps, 0.2)
        while True:
            # After a lock, idle until the session is unlocked again.
            if state["phase"] is Phase.LOCKED:
                if cap is not None:
                    cap.release()  # free the camera while locked
                    cap = None
                if not session_locked():
                    watcher.reset()
                    GLib.idle_add(set_phase, Phase.PRESENT)
                else:
                    time.sleep(2.0)
                    continue

            if cap is None:
                cap = cv2.VideoCapture(args.camera)
                if not cap.isOpened():
                    # Camera busy (video call?) or absent: we're blind — NEVER
                    # lock blind. Count as present and retry in a bit.
                    cap.release()
                    cap = None
                    watcher.reset()
                    time.sleep(5.0)
                    continue

            ok, frame = cap.read()
            now = time.monotonic()
            if not ok:
                cap.release()
                cap = None
                watcher.reset()  # blind — same rule as above
                time.sleep(5.0)
                continue

            found = engine.measure(frame).face_found
            phase = watcher.update(found, now)
            GLib.idle_add(set_phase, phase)
            time.sleep(period)

    threading.Thread(target=camera_loop, daemon=True).start()
    print(f"presence watcher: dim {args.dim_after}s, lock {args.lock_after}s, "
          f"{args.fps} fps", file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
