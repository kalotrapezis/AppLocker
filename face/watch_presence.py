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
import gc
import glob
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


def _session_id() -> str:
    """Our login session id for loginctl. `$XDG_SESSION_ID` in a real desktop
    session; otherwise the newest session owned by us. Empty string means "let
    loginctl resolve the caller's session", which is what the old call did."""
    sid = os.environ.get("XDG_SESSION_ID", "").strip()
    if sid:
        return sid
    try:
        out = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                             capture_output=True, text=True, timeout=5).stdout
        me = os.environ.get("USER", "")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == me:
                return parts[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


SESSION_ID = _session_id()


def session_locked() -> bool:
    try:
        out = subprocess.run(
            ["loginctl", "show-session", SESSION_ID, "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def session_active() -> bool:
    """Is our session the one on the screen? False after a user/VT switch —
    unlocking then would expose this desktop behind someone else's session."""
    try:
        out = subprocess.run(
            ["loginctl", "show-session", SESSION_ID, "-p", "Active", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


# ── no-button unlock of the lock screen ──────────────────────────────────────
# The PAM tier (pam_applocker in /etc/pam.d/kde) only runs when the KDE greeter
# starts a conversation — i.e. after you click Unlock or type something. That is
# the "confirmation" nobody wants. So while the screen is locked we run the same
# face check ourselves and, on a match, tell logind to unlock the session; the
# Plasma locker dismisses itself on that signal. No PAM involved, so a failure
# here can never lock anyone out: the greeter stays exactly as it is.

LOCKSCREEN_PAM = "/etc/pam.d/kde"

# recognize.py sits next to us both in the repo and in the flat install layout.
RECOGNIZE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recognize.py")

# The camera must not sit on all the while the screen is locked. Two bounds:
#   1. we only look when the lock screen's display is actually ON (it blanks
#      seconds after you walk away, and wakes the moment anyone touches the
#      machine — a free sysfs read, no camera involved);
#   2. after UNLOCK_TRIES misses with the screen continuously on (you're sitting
#      there on your phone), we fall back to UNLOCK_SLOW_GAP until the screen
#      blanks and wakes again.
UNLOCK_TRIES = 6
UNLOCK_SLOW_GAP = 30.0

# A fingerprint touch is spoof-resistant, so for a while afterwards a plain
# glance is enough and the turn challenge can be skipped (the Apple-style
# "recent strong factor" trust window). Same for a trusted phone being
# *connected* over Bluetooth — weaker, hence opt-in by listing its MAC.
TRUST_WINDOW = 20 * 60
FP_STAMP = "/run/applocker/last-fingerprint"
TRUSTED_BT = os.path.expanduser("~/.config/applocker/trusted-bt")


def screen_on() -> bool:
    """Is any display actually lit? The DRM connectors' dpms state, straight
    from sysfs — no camera, no D-Bus. Unknown (no readable connector) counts as
    on, so a machine we can't read still gets face unlock."""
    states = []
    for f in glob.glob("/sys/class/drm/card*/card*/dpms"):
        try:
            with open(f) as fh:
                states.append(fh.read().strip().lower())
        except OSError:
            pass
    return (not states) or ("on" in states)


def recent_fingerprint(window: float = TRUST_WINDOW) -> bool:
    """Did a fingerprint check pass within `window` seconds? The daemon stamps
    the file on every accepted touch; it lives on tmpfs, so a reboot clears the
    trust rather than carrying it over."""
    try:
        return (time.time() - os.path.getmtime(FP_STAMP)) < window
    except OSError:
        return False


def trusted_phone_near() -> bool:
    """Is a phone from ~/.config/applocker/trusted-bt *connected* (not merely
    paired) right now? One MAC per line. Empty/missing file → off."""
    try:
        with open(TRUSTED_BT) as f:
            macs = [l.split("#", 1)[0].strip() for l in f]
    except OSError:
        return False
    for mac in [m for m in macs if m]:
        try:
            out = subprocess.run(["bluetoothctl", "info", mac],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "Connected: yes" in out:
            return True
    return False


# Enabling this needs no root and no PAM edit: just the marker file. The PAM
# tier also counts, so the existing Settings toggle ("Unlock these with your
# face" → Lock screen) switches both the click-path and this one at once.
UNLOCK_MARKER = os.path.expanduser("~/.config/applocker/lockscreen-unlock")


def lockscreen_face_enabled() -> bool:
    """On when the marker file exists, or when the user's existing lock-screen
    face toggle has put pam_applocker into KDE's PAM stack. Two ways in, one
    intent — no second switch to forget about."""
    if os.path.exists(UNLOCK_MARKER):
        return True
    try:
        with open(LOCKSCREEN_PAM) as f:
            return "pam_applocker.so" in f.read()
    except OSError:
        return False


def owner_at_screen(cam_index, timeout: float, liveness: bool = True) -> bool:
    """One identity check at LOCKSCREEN camera priority. True only on a match;
    every failure (busy camera, no face, not you) is False, so we stay locked.

    Liveness is KEPT by default: this is the login tier, and a photo must not
    open the machine. `--unlock-no-liveness` trades that for a glance.
    """
    cmd = [sys.executable, RECOGNIZE, "--camera", str(cam_index),
           "--timeout", str(timeout)]
    if not liveness:
        cmd.append("--no-liveness")
    try:
        env = {**os.environ, "APPLOCKER_CAMERA_PRIORITY": "lockscreen"}
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 15, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip().splitlines()[-1:] == ["match"]


def unlock_session() -> bool:
    """logind unlock of our own session — allowed for the session's owner, no
    polkit prompt, no root. Verified to dismiss kscreenlocker on Plasma 6."""
    try:
        r = subprocess.run(["loginctl", "unlock-session", SESSION_ID],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
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


def snapshot_face_found(cam_index, warmup=3, samples=4):
    """Open the camera, grab a few frames (discarding the first `warmup` while
    the sensor auto-exposes), report whether ANY face was seen, then release.
    Returns ``(presence, luma)``: presence is True/False, or None when we're
    *blind* (camera busy, no readable frame, or too dark to tell) — the caller
    must treat None as "present" and never lock on it. ``luma`` is the mean
    brightness (0-255) of the last usable frame, or None (used for auto-brightness)."""
    import cv2

    engine = None

    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        cap.release()
        return None, None
    frame = None
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
            # Keep OpenCV's face models out of the permanent watcher process.
            # Loading them lazily also avoids doing any model work when every
            # captured frame is too dark to judge.
            if engine is None:
                try:
                    from engine import build_engine
                    engine = build_engine()
                except Exception as e:
                    print(f"presence: face engine unavailable ({e})", file=sys.stderr)
                    return None, last_luma
            try:
                face_found = engine.measure(frame).face_found
            except Exception as e:
                print(f"presence: face engine failed ({e})", file=sys.stderr)
                return None, last_luma
            if face_found:
                return True, m
        return (False if usable else None), last_luma
    finally:
        cap.release()
        # The engine owns OpenCV classifiers/models and their native buffers.
        # Drop it after each snapshot rather than retaining it for the watcher's
        # entire lifetime.
        del engine
        del frame
        gc.collect()


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
    ap.add_argument("--no-unlock-face", action="store_true",
                    help="don't try to unlock the lock screen with your face "
                         "(the lock-screen face toggle still works on a click)")
    ap.add_argument("--unlock-no-liveness", action="store_true",
                    help="unlock the screen on a plain face match, no turn "
                         "challenge — faster, but a photo could do it")
    ap.add_argument("--unlock-interval", type=float, default=5.0,
                    help=f"seconds between the first {UNLOCK_TRIES} unlock "
                         "attempts after the lock screen lights up")
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
    # Presence off is no longer a reason to quit: with the lock-screen face
    # unlock on, this process still has a job (no-button unlock while locked).
    if not args.force and not conf["enabled"] and (
            args.no_unlock_face or not lockscreen_face_enabled()):
        print("attention is off in the config and the lock-screen face unlock "
              "is off (enable either in AppLocker settings, or run with "
              "--force)", file=sys.stderr)
        return 2

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
        locked_since = None      # when the current lock started (unlock cadence)
        unlock_state = {"misses": 0, "dark": False}
        next_brightness_at = time.monotonic()  # fire an initial adjust on startup
        while True:
            # Re-read config each tick so the settings GUI takes effect live:
            # interval, AC-only, and the on/off switch (turning it off exits).
            conf = read_config()
            attention_on = args.force or conf["enabled"]
            # With presence off we still stay alive if the lock-screen face
            # toggle is on: the no-button unlock is this process's other job.
            unlock_on = (not args.no_unlock_face) and lockscreen_face_enabled()
            if not attention_on and not unlock_on:
                print("attention off and no lock-screen face unlock — "
                      "watcher exiting", file=sys.stderr)
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

            # Locked screen: try to let the owner straight back in, no button.
            # Any lock counts (manual Meta+L, lid, or our own walk-away lock).
            # Disabled → falls through to the old sit-idle behaviour below.
            if unlock_on and session_locked():
                if locked_since is None:
                    locked_since = time.monotonic()
                if not session_active():
                    time.sleep(3.0)
                    continue
                if not screen_on():
                    # Lock screen is dark: nobody is looking at it, so there is
                    # nothing to recognise. Camera stays off; this poll is free.
                    unlock_state["dark"] = True
                    time.sleep(3.0)
                    continue
                if unlock_state["dark"]:
                    unlock_state["dark"] = False   # screen just woke → someone's here
                    unlock_state["misses"] = 0
                # A recent fingerprint, or your phone connected over Bluetooth,
                # earns a plain glance; otherwise the turn challenge stands.
                why = ("--unlock-no-liveness" if args.unlock_no_liveness
                       else "recent fingerprint" if recent_fingerprint()
                       else "trusted phone connected" if trusted_phone_near()
                       else None)
                if owner_at_screen(args.camera, 5.0, liveness=why is None) \
                        and unlock_session():
                    how = f"glance ({why})" if why else "face + liveness"
                    print(f"presence: {how} matched at the lock screen — "
                          "session unlocked", file=sys.stderr)
                    go_present()
                    next_snapshot_at = None
                    locked_since = None
                    unlock_state["misses"] = 0
                    continue
                unlock_state["misses"] += 1
                time.sleep(args.unlock_interval
                           if unlock_state["misses"] < UNLOCK_TRIES
                           else UNLOCK_SLOW_GAP)
                continue
            locked_since = None

            # Presence itself is off — we're here only for the unlock job.
            if not attention_on:
                time.sleep(max(args.poll, 2.0))
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
                    found, luma = snapshot_face_found(args.camera)
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
    if args.no_unlock_face:
        print("lock-screen face unlock: off (--no-unlock-face)", file=sys.stderr)
    else:
        live = ("without liveness" if args.unlock_no_liveness
                else "liveness unless a fingerprint in the last "
                     f"{int(TRUST_WINDOW / 60)} min or a trusted phone")
        print(f"lock-screen face unlock: on ({live}); only while the lock "
              f"screen is lit, {args.unlock_interval:.0f}s apart for "
              f"{UNLOCK_TRIES} tries, then {int(UNLOCK_SLOW_GAP)}s",
              file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
