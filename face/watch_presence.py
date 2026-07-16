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
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attention import Config, Phase, SnapshotPresence  # noqa: E402
from cameralock import PRESENCE, camera_lock  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# GTK "draw" handlers are called with a cairo.Context, which needs the pycairo↔gi
# foreign marshaller (python3-gi-cairo). On a fresh install WITHOUT it, any draw
# signal raises `TypeError: Couldn't find foreign struct converter for
# 'cairo.Context'` and kills the watcher mid-run — which is why it stopped locking
# when you walked away. Detect it up front; if absent, skip the purely-cosmetic
# dim overlay (locking still happens through the same state machine).
try:
    gi.require_foreign("cairo")
    _HAVE_CAIRO = True
except Exception:
    _HAVE_CAIRO = False


_TRUE = ("on", "true", "1", "yes", "enabled")
_ALLOWED_INTERVALS = (2, 5, 10, 15, 30)


def read_config() -> dict:
    """The attention-related keys from the daemon's config. The watcher re-reads
    these each loop so changes made in the settings GUI take effect live:
      enabled      — the `attention` switch
      interval_min — minutes between snapshots (one of _ALLOWED_INTERVALS)
      ac_only      — pause the watcher while on battery
    """
    cfg = {"enabled": False, "interval_min": 2, "ac_only": False}
    path = os.environ.get("APPLOCKER_CONFIG", "/etc/applocker/config")
    try:
        with open(path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = (p.strip().lower() for p in line.split("=", 1))
                if k == "attention":
                    cfg["enabled"] = v in _TRUE
                elif k == "attention_ac_only":
                    cfg["ac_only"] = v in _TRUE
                elif k == "attention_interval":
                    try:
                        n = int(v)
                        if n in _ALLOWED_INTERVALS:
                            cfg["interval_min"] = n
                    except ValueError:
                        pass
    except OSError:
        pass
    return cfg


def on_ac_power():
    """True on AC, False on battery, None if it can't be told (e.g. a desktop
    with no battery — the caller then treats it as always-powered)."""
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except OSError:
        return None

    def _read(name, field):
        try:
            with open(os.path.join(base, name, field)) as f:
                return f.read().strip()
        except OSError:
            return None

    for n in names:  # an AC/Mains adapter's `online` is the clearest signal
        if _read(n, "type") == "Mains":
            online = _read(n, "online")
            if online in ("0", "1"):
                return online == "1"
    for n in names:  # else infer from a battery that's discharging
        if _read(n, "type") == "Battery":
            status = _read(n, "status")
            if status:
                return status != "Discharging"
    return None


def session_locked() -> bool:
    try:
        out = subprocess.run(
            ["loginctl", "show-session", "", "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


# ── automatic brightness ─────────────────────────────────────────────────────
# Reads the user's brightness prefs (written by the settings GUI) and, on each
# snapshot, nudges the screen brightness toward the time-of-day level based on how
# dark the room looks. User-side only — no root, no daemon.

BRIGHTNESS_PATH = os.environ.get(
    "APPLOCKER_BRIGHTNESS",
    os.path.expanduser("~/.config/applocker/brightness.json"))

# The presence snapshots only fire when you're *idle*, so brightness would never
# track the room while you're actively typing. This is the independent cadence for
# a brightness-only camera peek that runs whether you're active or idle (every
# 10 min), so the screen follows time-of-day + room light even during heavy use.
# Cheap now: the peek is a silent brightness set (no OSD) and a no-op within ±5%.
BRIGHTNESS_INTERVAL = 10 * 60

_bright_state = {"target": None, "backend": "?"}  # for change-only logging


def read_brightness_config() -> dict:
    cfg = {"enabled": False,
           "levels": {"morning": 100, "midday": 100, "afternoon": 80, "night": 20},
           "area": 25, "pause_on_game": True}
    try:
        with open(BRIGHTNESS_PATH) as f:
            data = json.load(f)
        cfg["enabled"] = bool(data.get("enabled", False))
        cfg["area"] = int(data.get("area", cfg["area"]))
        cfg["pause_on_game"] = bool(data.get("pause_on_game", cfg["pause_on_game"]))
        for k in cfg["levels"]:
            if k in data.get("levels", {}):
                cfg["levels"][k] = int(data["levels"][k])
    except (OSError, ValueError, TypeError):
        pass
    return cfg


# Signals that a Steam game is actually running (not just the Steam client):
# Steam launches games through `.../reaper SteamLaunch AppId=… -- <game>`, and
# many titles run under gamescope. Either means "in a game", so with the
# pause-on-game option on we leave the screen brightness alone.
_GAME_MARKERS = ("SteamLaunch", "gamescope")


def game_running() -> bool:
    for pat in _GAME_MARKERS:
        try:
            if subprocess.run(["pgrep", "-f", pat],
                              capture_output=True, timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


# Desktop notifications for snapshot outcomes, de-duplicated so a steady state
# (e.g. the shutter staying closed) notifies once, not every interval.
_note_state = {"last": None}


def _notify(summary: str, body: str = "") -> None:
    try:
        subprocess.Popen(["notify-send", "-a", "AppLocker", summary, body])
    except OSError:
        pass


def _snapshot_note(kind: str, msg: str) -> None:
    """Log to stderr and pop a desktop notification, but only when the outcome
    kind changes (so we don't spam one per snapshot)."""
    print(f"presence: {msg}", file=sys.stderr)
    if _note_state["last"] != kind:
        _note_state["last"] = kind
        _notify("AppLocker presence", msg)


def _time_of_day_level(levels: dict, hour: int) -> int:
    if 5 <= hour < 11:
        return levels["morning"]
    if 11 <= hour < 16:
        return levels["midday"]
    if 16 <= hour < 20:
        return levels["afternoon"]
    return levels["night"]


def compute_target(cfg: dict, luma: float) -> int:
    """Time-of-day level, nudged ±area by room darkness. luma is 0-255 (128 =
    neutral): a darker room lowers brightness, a brighter room raises it."""
    level = _time_of_day_level(cfg["levels"], time.localtime().tm_hour)
    nudge = ((luma / 255.0) - 0.5) * 2.0 * cfg["area"]
    return int(max(1, min(100, level + nudge)))


def _run_ok(cmd) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=8).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def set_screen_brightness(percent: int):
    """Set brightness to `percent` (1-100) via the first backend that works:
    brightnessctl (laptop backlight) → ddcutil (external DDC monitor) → KDE
    PowerDevil D-Bus. Returns the backend name, or None if none worked.

    The PowerDevil path uses **setBrightnessSilent** — same as setBrightness but
    without popping KDE's brightness OSD, so our automatic nudges stay invisible
    while the user's own brightness keys (which use setBrightness) still show it."""
    p = max(1, min(100, int(percent)))
    if _run_ok(["brightnessctl", "-q", "set", f"{p}%"]):
        return "brightnessctl"
    if _run_ok(["ddcutil", "setvcp", "10", str(p)]):  # VCP 0x10 = luminance 0-100
        return "ddcutil"
    for qd in ("qdbus6", "qdbus"):
        base = ["org.kde.Solid.PowerManagement",
                "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"]
        iface = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"
        try:
            mx = subprocess.run([qd, *base, f"{iface}.brightnessMax"],
                                capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        if mx.returncode == 0 and mx.stdout.strip().isdigit():
            target = int(int(mx.stdout.strip()) * p / 100)
            # setBrightnessSilent → no OSD popup (setBrightness would show it).
            if _run_ok([qd, *base, f"{iface}.setBrightnessSilent", str(target)]):
                return f"powerdevil({qd})"
    return None


def grab_room_luma(cam_index):
    """Open the camera, grab one usable frame's mean luma (0-255) while the sensor
    auto-exposes, then release. Returns the luma, or None if the camera is busy /
    no readable frame / too dark to trust. Face-detection-free — used both by the
    settings GUI's immediate adjust and the watcher's periodic brightness tick."""
    import cv2
    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        cap.release()
        return None
    luma = None
    try:
        for i in range(6):  # a few warmup frames while the sensor auto-exposes
            ok, frame = cap.read()
            if ok and i >= 3 and float(frame.mean()) >= DARK_FLOOR:
                luma = float(frame.mean())
    finally:
        cap.release()
    return luma


def brightness_once(cam_index) -> int:
    """Grab one usable frame, set the screen brightness from it, and exit. Used by
    the settings GUI to adjust *immediately* when you enable auto-brightness,
    instead of waiting for the next idle snapshot. No face detection needed."""
    luma = grab_room_luma(cam_index)
    if luma is None:
        print("brightness-once: no usable frame (too dark / busy)", file=sys.stderr)
        return 1
    cfg = read_brightness_config()
    if cfg["pause_on_game"] and game_running():
        print("brightness-once: skipped (a game is running)", file=sys.stderr)
        return 0
    target = compute_target(cfg, luma)
    backend = set_screen_brightness(target)
    print(f"brightness-once: set {target}% (room luma {luma:.0f}, via {backend})",
          file=sys.stderr)
    return 0 if backend else 1


def maybe_adjust_brightness(luma):
    """If auto-brightness is on and we have a usable room-luma reading, set the
    screen. Logs only when the target or backend changes, to avoid spam."""
    if luma is None:
        return
    cfg = read_brightness_config()
    if not cfg["enabled"]:
        return
    # Don't fight a game's own brightness/HDR while you're playing.
    if cfg["pause_on_game"] and game_running():
        if _bright_state.get("gamepaused") is not True:
            _bright_state["gamepaused"] = True
            print("brightness: paused (a game is running)", file=sys.stderr)
        return
    _bright_state["gamepaused"] = False
    target = compute_target(cfg, luma)
    # Deadband: don't re-apply a level we're already at. Re-setting the SAME
    # brightness still pops KDE's on-screen brightness OSD (very annoying mid-game),
    # and camera luma jitters a few % frame-to-frame — so only act on a real change.
    applied = _bright_state.get("applied")
    if applied is not None and abs(target - applied) < 5:
        return
    backend = set_screen_brightness(target)
    if backend:
        _bright_state["applied"] = target
    if (target, backend) != (_bright_state["target"], _bright_state["backend"]):
        _bright_state["target"], _bright_state["backend"] = target, backend
        if backend:
            print(f"brightness: set {target}% (room luma {luma:.0f}, via {backend})",
                  file=sys.stderr)
        else:
            print("brightness: no working backend (install brightnessctl or ddcutil, "
                  "or check KDE PowerDevil)", file=sys.stderr)


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
            # Verify the server actually HAS the ScreenSaver extension. Under
            # XWayland it is typically MISSING: AllocInfo still succeeds but every
            # QueryInfo fails (and Xlib spams "extension MIT-SCREEN-SAVER missing"),
            # so without this check we'd report available=True while seconds()
            # always returns None — the watcher then can't tell active from idle.
            self._xss.XScreenSaverQueryExtension.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int)]
            eb, errb = ctypes.c_int(), ctypes.c_int()
            if self._xss.XScreenSaverQueryExtension(
                    self._dpy, ctypes.byref(eb), ctypes.byref(errb)) == 0:
                return False  # no extension (Wayland) → this backend can't work
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
    Returns ``(presence, luma)``: presence is True/False, or None when we're
    *blind* (camera busy, no readable frame, or too dark to tell) — the caller
    must treat None as "present" and never lock on it. ``luma`` is the mean
    brightness (0-255) of the last usable frame, or None (used for auto-brightness)."""
    import cv2

    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        cap.release()
        return None, None
    try:
        usable = False  # saw at least one readable, bright-enough frame
        last_luma = None
        for i in range(warmup + samples):
            ok, frame = cap.read()
            if not ok:
                continue
            if i < warmup:
                continue
            m = float(frame.mean())
            if m < DARK_FLOOR:
                continue  # too dark to trust the detector
            usable = True
            last_luma = m
            if engine.measure(frame).face_found:
                return True, m
        return (False if usable else None), last_luma
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
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between snapshots (overrides the config's "
                         "attention_interval, which is in minutes)")
    ap.add_argument("--confirm-after", type=float, default=20.0,
                    help="seconds to the confirming snapshot after an empty one")
    ap.add_argument("--misses", type=int, default=2,
                    help="consecutive empty snapshots before locking")
    ap.add_argument("--poll", type=float, default=3.0,
                    help="how often to sample idle time (cheap, no camera)")
    ap.add_argument("--force", action="store_true",
                    help="run even if the config has attention off")
    ap.add_argument("--no-lock", action="store_true",
                    help="testing: log 'would lock' instead of locking the session")
    ap.add_argument("--brightness-once", action="store_true",
                    help="grab one frame, set brightness from it, and exit")
    ap.add_argument("--brightness-interval", type=float, default=BRIGHTNESS_INTERVAL,
                    help="seconds between brightness-only peeks (runs even while "
                         "active, independent of presence snapshots)")
    args = ap.parse_args()

    if args.brightness_once:
        return brightness_once(args.camera)

    conf = read_config()
    if not args.force and not conf["enabled"]:
        print("attention is off in the config (enable it in AppLocker settings, "
              "or run with --force)", file=sys.stderr)
        return 2

    from engine import build_engine

    engine = build_engine()
    # --interval (seconds) overrides the config's attention_interval (minutes).
    interval = args.interval if args.interval is not None else conf["interval_min"] * 60
    cfg = Config(idle_after=args.idle_after, interval=interval,
                 confirm_after=args.confirm_after, misses_to_lock=args.misses)
    presence = SnapshotPresence(cfg)
    # The dim overlay is cosmetic and needs cairo (see _HAVE_CAIRO). Without it we
    # simply don't dim — but LOCKING still works, which is the part that matters.
    overlay = DimOverlay() if _HAVE_CAIRO else None
    state = {"phase": Phase.PRESENT}

    def set_phase(phase):
        if phase == state["phase"]:
            return
        state["phase"] = phase
        if overlay is not None:
            if phase is Phase.DIMMED:
                overlay.show_all()
            else:
                overlay.hide()
        if phase is Phase.LOCKED:
            if args.no_lock:
                print("presence lost — WOULD lock session (--no-lock)", file=sys.stderr)
            else:
                print("presence lost — locking session", file=sys.stderr)
                subprocess.run(["loginctl", "lock-session"], timeout=10)

    def go_present():
        presence.reset()
        GLib.idle_add(set_phase, Phase.PRESENT)

    def loop():
        next_snapshot_at = None  # monotonic deadline; None = not scheduled yet
        next_brightness_at = time.monotonic()  # fire an initial adjust on startup
        while True:
            # Re-read config each tick so the settings GUI takes effect live:
            # interval, AC-only, and the on/off switch (turning it off exits).
            conf = read_config()
            if not args.force and not conf["enabled"]:
                print("attention turned off in the config — watcher exiting",
                      file=sys.stderr)
                GLib.idle_add(Gtk.main_quit)
                return
            if args.interval is None:
                cfg.interval = conf["interval_min"] * 60  # live-adjust cadence

            # AC-only: on battery, disable ourselves (camera off, no checks).
            if conf["ac_only"] and on_ac_power() is False:
                if state["phase"] is not Phase.PRESENT or next_snapshot_at:
                    go_present()
                next_snapshot_at = None
                time.sleep(max(args.poll, 5.0))
                continue

            # After a lock, sit idle until the session unlocks, then resume.
            if presence.locked or state["phase"] is Phase.LOCKED:
                if not session_locked():
                    go_present()
                    next_snapshot_at = None
                time.sleep(max(args.poll, 2.0))
                continue

            now = time.monotonic()

            # Brightness-only peek on its own cadence. One frame, camera released
            # immediately. Skipped while locked / on battery above.
            if now >= next_brightness_at:
                next_brightness_at = now + max(60.0, args.brightness_interval)
                if read_brightness_config()["enabled"]:
                    # Lowest priority: if a folder reveal / app / lockscreen wants
                    # the camera, skip this brightness peek (luma None → no
                    # adjustment) and try again next interval.
                    with camera_lock(PRESENCE) as got:
                        luma = grab_room_luma(args.camera) if got else None
                    maybe_adjust_brightness(luma)
                    now = time.monotonic()

            # Pure time-based checking: a snapshot fires on the interval, whether
            # you've touched the keyboard/mouse or not. Idle detection (X11
            # ScreenSaver / xprintidle) is unavailable on Wayland, so relying on it
            # meant this never ran here — hence the timer is the single source of
            # truth. First snapshot fires immediately on startup.
            if next_snapshot_at is None:
                next_snapshot_at = now
            if now < next_snapshot_at:
                # Wake at least every --poll so returning activity is noticed
                # quickly (cancels a pending dim without waiting for a snapshot).
                time.sleep(min(args.poll, next_snapshot_at - now))
                continue

            # Lowest priority: yield the camera to a folder reveal / app /
            # lockscreen. If it's busy we get found=None → treated as blind →
            # present (never locks on it), and we retry next interval.
            with camera_lock(PRESENCE) as got:
                if got:
                    found, luma = snapshot_face_found(engine, args.camera)
                else:
                    found, luma = None, None
            now = time.monotonic()
            maybe_adjust_brightness(luma)  # auto-brightness rides the same frame

            # Log every snapshot outcome so the behaviour is observable.
            lstr = f"{luma:.0f}" if luma is not None else "?"
            if found is None:
                if luma is not None and luma < DARK_FLOOR:
                    _snapshot_note("black", f"camera sees BLACK (luma {lstr}) — "
                                   "covered or dark; can't verify")
                else:
                    print("presence: camera busy / no frame — skipping",
                          file=sys.stderr)
                # Blind (camera busy / no frame / too dark): never lock here.
                go_present()
                next_snapshot_at = now + cfg.interval
                continue
            _note_state["last"] = None  # a real reading resets the black dedup
            if found:
                print(f"presence: face found (luma {lstr})", file=sys.stderr)
            else:
                print(f"presence: NO face in frame (luma {lstr})", file=sys.stderr)

            phase = presence.record(found)
            GLib.idle_add(set_phase, phase)
            next_snapshot_at = now + presence.next_delay(phase)

    threading.Thread(target=loop, daemon=True).start()
    ac = " [AC-only]" if conf["ac_only"] else ""
    print(f"presence watcher: time-based, snapshot every {cfg.interval:.0f}s, "
          f"lock after {args.misses} empty snapshots{ac}", file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
