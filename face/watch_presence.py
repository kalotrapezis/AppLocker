#!/usr/bin/env python3
"""Presence watcher — lock when you walk away, without a camera light staring at
you all day (roadmap step 5, **idle-triggered snapshot** model).

Runs in *your* session (not the daemon). The camera stays OFF while you use the
PC. Only once the session goes idle (no keyboard/mouse for `--idle-after`s) does
it periodically wake the camera for a SINGLE frame, check for *any* face
(identity-blind — see attention.py), then release the camera:

    active (typing/mouse)   → camera off
    idle, face seen         → you're reading/watching; re-check every --interval
    idle, no face (once)    → dim the screen (warn) + a quick confirming snapshot
    idle, no face (--misses in a row) → `loginctl lock-session`

Locking goes through logind, so the daemon's lock listener wipes the app-unlock
cache — manual lock, lid-close, and this watcher all behave identically.

    python3 face/watch_presence.py            # honours `attention` in the config
    python3 face/watch_presence.py --force    # run even if the config says off

Being blind never locks you out by mistake:
  - camera unavailable (a video call has it) → treated as PRESENT, retry later
  - after LOCK, it waits for the session to unlock (LockedHint) and resumes
  - if idle time can't be detected, it degrades to plain periodic snapshots
    (still one frame at a time, camera never held open)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attention import Config, Phase, SnapshotPresence  # noqa: E402

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


# ── idle detection ───────────────────────────────────────────────────────────

class _XSSInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),  # milliseconds since last input
        ("event_mask", ctypes.c_ulong),
    ]


class IdleMonitor:
    """Seconds since the last keyboard/mouse input, via the X11 ScreenSaver
    extension (ctypes → libXss, no external process). Falls back to the
    `xprintidle` binary, then reports "unknown" (None) if neither works."""

    def __init__(self):
        self._backend = None
        self._xss = self._x11 = self._dpy = self._root = self._info = None
        if self._init_xss():
            self._backend = "xss"
        elif self._have_xprintidle():
            self._backend = "xprintidle"

    def _init_xss(self) -> bool:
        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
            self._xss = ctypes.CDLL("libXss.so.1")
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
            self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XSSInfo)
            self._xss.XScreenSaverQueryInfo.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XSSInfo)]
            self._dpy = self._x11.XOpenDisplay(None)
            if not self._dpy:
                return False
            self._root = self._x11.XDefaultRootWindow(self._dpy)
            self._info = self._xss.XScreenSaverAllocInfo()
            return bool(self._info)
        except (OSError, AttributeError):
            return False

    @staticmethod
    def _have_xprintidle() -> bool:
        try:
            subprocess.run(["xprintidle"], capture_output=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    @property
    def available(self) -> bool:
        return self._backend is not None

    def seconds(self):
        """Idle seconds, or None if it can't be determined right now."""
        if self._backend == "xss":
            try:
                if self._xss.XScreenSaverQueryInfo(
                        self._dpy, self._root, self._info) == 0:
                    return None
                return self._info.contents.idle / 1000.0
            except OSError:
                return None
        if self._backend == "xprintidle":
            try:
                out = subprocess.run(["xprintidle"], capture_output=True,
                                     text=True, timeout=5).stdout.strip()
                return int(out) / 1000.0
            except (OSError, subprocess.SubprocessError, ValueError):
                return None
        return None


# ── one-shot snapshot ────────────────────────────────────────────────────────

# Below this mean luma (0-255) a frame is essentially black — a covered lens or
# an unlit room. The YuNet detector false-positives on pure black, so we refuse
# to judge presence from such frames (treat as blind → never lock, retry later).
DARK_FLOOR = 8.0


def snapshot_face_found(engine, cam_index, warmup=3, samples=4):
    """Open the camera, grab a few frames (discarding the first `warmup` while
    the sensor auto-exposes), report whether ANY face was seen, then release.
    Returns True/False, or None when we're *blind* (camera busy, no readable
    frame, or too dark to tell) — the caller must treat None as "present" and
    never lock on it."""
    import cv2

    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        usable = False  # saw at least one readable, bright-enough frame
        for i in range(warmup + samples):
            ok, frame = cap.read()
            if not ok:
                continue
            if i < warmup:
                continue
            if float(frame.mean()) < DARK_FLOOR:
                continue  # too dark to trust the detector
            usable = True
            if engine.measure(frame).face_found:
                return True
        return False if usable else None
    finally:
        cap.release()


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
    ap.add_argument("--idle-after", type=float, default=60.0,
                    help="seconds of no keyboard/mouse before checks start")
    ap.add_argument("--interval", type=float, default=120.0,
                    help="seconds between snapshots while present-but-idle")
    ap.add_argument("--confirm-after", type=float, default=20.0,
                    help="seconds to the confirming snapshot after an empty one")
    ap.add_argument("--misses", type=int, default=2,
                    help="consecutive empty snapshots before locking")
    ap.add_argument("--poll", type=float, default=3.0,
                    help="how often to sample idle time (cheap, no camera)")
    ap.add_argument("--force", action="store_true",
                    help="run even if the config has attention off")
    args = ap.parse_args()

    if not args.force and not attention_enabled():
        print("attention is off in the config (enable it in AppLocker settings, "
              "or run with --force)", file=sys.stderr)
        return 2

    from engine import build_engine

    engine = build_engine()
    cfg = Config(idle_after=args.idle_after, interval=args.interval,
                 confirm_after=args.confirm_after, misses_to_lock=args.misses)
    presence = SnapshotPresence(cfg)
    idle = IdleMonitor()
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

    def go_present():
        presence.reset()
        GLib.idle_add(set_phase, Phase.PRESENT)

    def loop():
        next_snapshot_at = None  # monotonic deadline; None = not scheduled yet
        while True:
            # After a lock, sit idle until the session unlocks, then resume.
            if presence.locked or state["phase"] is Phase.LOCKED:
                if not session_locked():
                    go_present()
                    next_snapshot_at = None
                time.sleep(max(args.poll, 2.0))
                continue

            idle_s = idle.seconds()
            now = time.monotonic()

            # Active (or idle unknown but recent activity): camera off, present.
            # When idle can't be measured at all we skip the gate and just poll
            # on the interval below (still one frame at a time).
            if idle.available and idle_s is not None and idle_s < cfg.idle_after:
                if state["phase"] is not Phase.PRESENT or next_snapshot_at:
                    go_present()
                next_snapshot_at = None
                time.sleep(args.poll)
                continue

            # Idle → checking mode. First snapshot fires immediately on entry.
            if next_snapshot_at is None:
                next_snapshot_at = now
            if now < next_snapshot_at:
                # Wake at least every --poll so returning activity is noticed
                # quickly (cancels a pending dim without waiting for a snapshot).
                time.sleep(min(args.poll, next_snapshot_at - now))
                continue

            found = snapshot_face_found(engine, args.camera)
            now = time.monotonic()
            if found is None:
                # Blind (camera busy / no frame): never lock — count as present.
                go_present()
                next_snapshot_at = now + cfg.interval
                continue

            phase = presence.record(found)
            GLib.idle_add(set_phase, phase)
            next_snapshot_at = now + presence.next_delay(phase)

    threading.Thread(target=loop, daemon=True).start()
    how = idle._backend or "none (periodic fallback)"
    print(f"presence watcher: idle-after {args.idle_after}s, every {args.interval}s, "
          f"lock after {args.misses} empty (idle backend: {how})", file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
