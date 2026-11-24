#!/usr/bin/env python3
"""Cross-process camera arbitration with a priority ladder.

Several user-session helpers wake the webcam for a moment. Opening the same
device twice at once just fails, so they must take turns — and when two want it
together, the more urgent one wins. Priority, highest first:

    LOCKSCREEN  unlock the locked session          (most urgent; not wired yet)
    APP         app-gate face unlock               (recognize.py, launched by the daemon)
    FILE        hidden-folder auto-reveal          (recognize.py, launched by hide_watch)
    PRESENCE    "did you walk away?" + brightness  (background; happy to skip a beat)

Mechanism: a single advisory `flock` grants the camera; a per-process marker file
(pid → priority) under a shared "wants" dir lets everyone see what else is
waiting. A requester takes the lock only when no *higher* priority is pending, so
a lower-priority holder releases and steps aside for the next round. PRESENCE
never waits (it treats a busy camera as "blind" and retries later); the higher,
user-driven levels wait up to a timeout.

    from cameralock import camera_lock, FILE, PRESENCE, priority_from_name

    with camera_lock(FILE) as got:        # or PRESENCE, APP, LOCKSCREEN
        if got:
            ...open the camera...
        else:
            ...busy / out-prioritised → skip and retry...

Advisory only — it coordinates *our* helpers, not third-party apps. A video call
holding the camera just makes everyone go blind, the existing safe behaviour.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time

# Priority levels (higher number = more urgent).
LOCKSCREEN = 40
APP = 30
FILE = 20
PRESENCE = 10

_NAMES = {"lockscreen": LOCKSCREEN, "app": APP, "file": FILE, "presence": PRESENCE}


def priority_from_name(name):
    """Map 'lockscreen'/'app'/'file'/'presence' → level; None if unset/unknown."""
    if name is None:
        return None
    return _NAMES.get(str(name).strip().lower())


def _runtime_dir() -> str:
    d = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache/applocker")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~/.cache/applocker")
        os.makedirs(d, exist_ok=True)
    return d


def _lock_path() -> str:
    return os.path.join(_runtime_dir(), "applocker-camera.lock")


def _wants_dir() -> str:
    d = os.path.join(_runtime_dir(), "applocker-camera.wants")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _publish(pid: int, priority: int) -> None:
    try:
        with open(os.path.join(_wants_dir(), str(pid)), "w") as f:
            f.write(str(priority))
    except OSError:
        pass


def _unpublish(pid: int) -> None:
    try:
        os.remove(os.path.join(_wants_dir(), str(pid)))
    except OSError:
        pass


def _higher_pending(mypid: int, mypri: int) -> bool:
    """True if another *live* process wants the camera at a higher priority.
    Stale markers (whose process has died) are cleaned up as we go."""
    d = _wants_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return False
    for nm in names:
        if nm == str(mypid):
            continue
        if not (nm.isdigit() and os.path.exists(f"/proc/{nm}")):
            try:
                os.remove(os.path.join(d, nm))  # process gone → drop its marker
            except OSError:
                pass
            continue
        try:
            with open(os.path.join(d, nm)) as f:
                pri = int(f.read().strip() or 0)
        except (OSError, ValueError):
            pri = 0
        if pri > mypri:
            return True
    return False


@contextlib.contextmanager
def camera_lock(priority: int = PRESENCE, timeout: float = 20.0):
    """Yield True if the camera is ours, else False. PRESENCE never waits; the
    higher (user-driven) levels wait up to `timeout`, stepping aside only for a
    strictly higher-priority request."""
    pid = os.getpid()
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    _publish(pid, priority)
    got = False
    try:
        blocking = priority > PRESENCE
        deadline = time.monotonic() + timeout
        while True:
            if not _higher_pending(pid, priority):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    pass  # someone holds it right now — wait or bail
            if not blocking or time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        _unpublish(pid)
        os.close(fd)
