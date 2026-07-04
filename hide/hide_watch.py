#!/usr/bin/env python3
"""AppLocker hidden-files auto-reveal — face check when you open the folder.

Companion to hidelist.py. hidelist only flips a name in/out of a `.hidden` file;
this watcher is what makes it *automatic*:

    you open a folder in the file manager that has hidden items
        → a SILENT face check (camera, no dialog — recognize.py --no-liveness)
        → if it's you, the hidden items in that folder are revealed
        → you walk away (session locks) or a while passes → they re-hide

The trigger is **inotify IN_OPEN** on the *containing directory* — when Dolphin
(or any file manager) lists a folder it `opendir()`s it, which fires IN_OPEN with
an empty name. inotify is watch-only: unlike the fanotify file-gate (which froze
the machine), it can NEVER block or slow a file operation. Nothing is mounted or
encrypted — we only edit `.hidden`, exactly like doing it by hand.

Why face-only (no PIN/sudo fallback): background daemons (baloo, backups) open
your home dir constantly. A full auth prompt would pop a PIN dialog while you're
away. A face check is silent — if you're at the camera it reveals; if not, the
items simply stay hidden and nothing pops up. Manual reveal is always available
via the X in AppLocker settings.

    python3 hide/hide_watch.py              # runs quietly; idle if nothing hidden
    python3 hide/hide_watch.py --camera 1 --reveal-timeout 600

Single-instance (a flock), so the settings window can spawn it freely.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# hidelist sits next to us (installed flat) or here in the repo.
import hidelist  # noqa: E402


def _recognize_script() -> str:
    for cand in (os.path.join(HERE, "recognize.py"),          # installed: flat
                 os.path.join(HERE, "..", "face", "recognize.py")):  # repo
        if os.path.exists(cand):
            return cand
    return os.path.join(HERE, "recognize.py")


RECOGNIZE = _recognize_script()


# ── inotify (ctypes → libc, no external deps) ────────────────────────────────

IN_OPEN = 0x00000020
IN_ONLYDIR = 0x01000000
IN_IGNORED = 0x00008000  # watch removed (dir deleted/moved) — drop our bookkeeping
_HDR = struct.Struct("iIII")  # wd, mask, cookie, len

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int
_libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
_libc.inotify_rm_watch.restype = ctypes.c_int


class Inotify:
    def __init__(self):
        self.fd = _libc.inotify_init1(os.O_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.wd_to_path: dict[int, str] = {}
        self.path_to_wd: dict[str, int] = {}

    def watch(self, path: str) -> bool:
        if path in self.path_to_wd:
            return True
        wd = _libc.inotify_add_watch(self.fd, path.encode(), IN_OPEN | IN_ONLYDIR)
        if wd < 0:
            return False
        self.wd_to_path[wd] = path
        self.path_to_wd[path] = wd
        return True

    def unwatch(self, path: str) -> None:
        wd = self.path_to_wd.pop(path, None)
        if wd is not None:
            _libc.inotify_rm_watch(self.fd, wd)
            self.wd_to_path.pop(wd, None)

    def read_events(self):
        """(dir_path, name) for each event; name '' means the dir itself opened."""
        try:
            data = os.read(self.fd, 8192)
        except BlockingIOError:
            return []
        except OSError:
            return []
        out, i = [], 0
        while i + _HDR.size <= len(data):
            wd, mask, _cookie, length = _HDR.unpack_from(data, i)
            i += _HDR.size
            name = data[i:i + length].split(b"\0", 1)[0].decode(errors="replace")
            i += length
            path = self.wd_to_path.get(wd)
            if path is None:
                continue
            if mask & IN_IGNORED:
                # Kernel dropped the watch (dir gone) — forget it so reconcile re-adds.
                self.wd_to_path.pop(wd, None)
                self.path_to_wd.pop(path, None)
                continue
            out.append((path, name))
        return out


# ── session state ────────────────────────────────────────────────────────────

def session_locked() -> bool:
    try:
        out = subprocess.run(
            ["loginctl", "show-session", "", "-p", "LockedHint", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def owner_present(camera: int, timeout: float) -> bool:
    """Silent camera check: True only if an enrolled face matches. No dialog,
    no liveness challenge (frictionless, like the app-gate). Any failure — no
    camera, no face, not you — is False, so items stay hidden."""
    try:
        env = {**os.environ, "APPLOCKER_CAMERA_PRIORITY": "file"}  # below app/lock
        r = subprocess.run(
            [sys.executable, RECOGNIZE, "--no-liveness",
             "--camera", str(camera), "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 12, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip().splitlines()[-1:] == ["match"]


# ── the watcher ──────────────────────────────────────────────────────────────

def _rehide(entries: list[str]) -> None:
    for e in entries:
        if os.path.exists(os.path.dirname(e)):  # dir still there
            hidelist.set_hidden(e, True)


def run(args) -> int:
    ino = Inotify()
    targets: list[str] = []                   # managed entries (abs paths)
    revealed: dict[str, float] = {}           # target -> re-hide deadline (monotonic)
    suppress: dict[str, float] = {}           # parent dir -> "don't face-check until"
    last_check = 0.0                           # global cooldown between camera peeks
    last_reconcile = 0.0

    print(f"hide-watch: started (recognize={RECOGNIZE})", file=sys.stderr)

    def reconcile():
        """Watch each target's PARENT (to catch you entering the folder) and, for
        targets that are folders, the TARGET itself (so being inside it keeps it
        shown). Drop watches/state for entries no longer managed."""
        nonlocal targets
        targets = hidelist.load_registry()
        want = {os.path.dirname(t) for t in targets}
        want |= {t for t in targets if os.path.isdir(t)}
        have = set(ino.path_to_wd)
        for d in want - have:
            if os.path.isdir(d):
                ino.watch(d)
        for d in have - want:
            ino.unwatch(d)
        for t in list(revealed):  # forget reveals for entries we no longer manage
            if t not in targets:
                del revealed[t]

    def rehide(target: str, reason: str):
        _rehide([target])
        revealed.pop(target, None)
        # The re-hide edits .hidden → the file manager re-lists the folder, which
        # looks just like re-entering. Suppress face checks on that folder briefly
        # so we don't immediately re-prompt and flicker.
        suppress[os.path.dirname(target)] = time.monotonic() + args.recheck_cooldown
        print(f"hide-watch: re-hid {target} ({reason})", file=sys.stderr)

    reconcile()

    import select
    while True:
        try:
            select.select([ino.fd], [], [], 1.0)
        except OSError:
            time.sleep(1.0)
        events = ino.read_events()
        now = time.monotonic()

        # Watched dirs just *listed* (the dir itself opened → empty name).
        opened = {d for (d, name) in events if name == ""}

        for d in opened:
            # (a) You're INSIDE a revealed target folder → keep it shown.
            if d in revealed:
                revealed[d] = now + args.reveal_timeout
            # (b) d is a target's PARENT → refresh any already-revealed siblings,
            #     and offer a face check for the ones still hidden.
            here = [t for t in targets if os.path.dirname(t) == d]
            for t in here:
                if t in revealed:
                    revealed[t] = now + args.reveal_timeout
            hidden = [t for t in here if hidelist.is_hidden(t)]
            if (hidden and now >= suppress.get(d, 0.0)
                    and (now - last_check) >= args.cooldown):
                print(f"hide-watch: {d} opened with hidden items — face check",
                      file=sys.stderr)
                ok = owner_present(args.camera, args.face_timeout)  # blocks briefly
                last_check = now = time.monotonic()
                if ok:
                    for t in hidden:
                        hidelist.set_hidden(t, False)
                        revealed[t] = now + args.reveal_timeout
                    print(f"hide-watch: revealed {len(hidden)} item(s) in {d}",
                          file=sys.stderr)
                else:
                    print(f"hide-watch: no owner match — {d} stays hidden",
                          file=sys.stderr)

        # Re-hide: on session lock (walked away) — instant, reliable — or when a
        # reveal has aged out with no further activity in the folder/target.
        if revealed:
            if session_locked():
                for t in list(revealed):
                    rehide(t, "session locked")
            else:
                for t in list(revealed):
                    if now >= revealed[t]:
                        rehide(t, "inactivity timeout")

        # Pick up registry changes (added/removed hidden items) periodically.
        if now - last_reconcile >= 3.0:
            reconcile()
            last_reconcile = now


def _single_instance_or_exit() -> None:
    """flock a per-user lockfile so a second launch (e.g. settings re-spawning us)
    just exits instead of running a duplicate watcher."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache/applocker")
    os.makedirs(runtime, exist_ok=True)
    lock_path = os.path.join(runtime, "applocker-hide-watch.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("hide-watch: already running — exiting", file=sys.stderr)
        sys.exit(0)
    # Keep fd open for the process lifetime (holds the lock).
    global _LOCK_FD
    _LOCK_FD = fd


def main() -> int:
    ap = argparse.ArgumentParser(description="AppLocker hidden-files auto-reveal")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--face-timeout", type=float, default=8.0,
                    help="seconds to look for your face on each folder open")
    ap.add_argument("--cooldown", type=float, default=10.0,
                    help="min seconds between camera peeks (avoids hammering)")
    ap.add_argument("--reveal-timeout", type=float, default=60.0,
                    help="re-hide this long after you last touch the folder/target "
                         "(also re-hides instantly when the screen locks)")
    ap.add_argument("--recheck-cooldown", type=float, default=15.0,
                    help="after an auto re-hide, don't face-check that folder again "
                         "for this long (stops reveal/hide flicker)")
    args = ap.parse_args()

    _single_instance_or_exit()
    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
