#!/usr/bin/env python3
"""AppLocker unlock feedback — a small borderless status window (GTK 3).

Shown by the daemon while the face recognizer runs, so an unlock is never a
silent pause: the user sees "looking for you", a shake + ✕ per failed attempt,
and a green ✓ on success. It displays state only — it never sees secrets and
makes no decision (mirrors the philosophy of auth_prompt.py).

Protocol — one command per line on **stdin** (the daemon holds the pipe):

    scanning            # show the "looking for you" state
    fail <n> <total>    # attempt n of total failed: shake, show ✕
    ok                  # matched: show green ✓, then self-close
    close               # go away now (e.g. falling back to the PIN prompt)

EOF on stdin also closes the window, so a crashed daemon can't leave it stuck
on screen. Native GTK widgets only, so the Mint-Y theme applies untouched.
"""

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


class FeedbackWindow(Gtk.Window):
    def __init__(self, app_name):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_resizable(False)
        self.set_accept_focus(False)  # never steal focus from the user's work

        frame = Gtk.Frame()  # a thin themed border so "borderless" still has an edge
        self.add(frame)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(18)
        frame.add(box)

        self.icon = Gtk.Label()
        self.icon.set_markup("<span size='400%'>👤</span>")
        box.pack_start(self.icon, False, False, 0)

        self.status = Gtk.Label(label="Looking for you…")
        box.pack_start(self.status, False, False, 0)

        sub = app_name or "AppLocker"
        self.subtitle = Gtk.Label(label=sub)
        self.subtitle.get_style_context().add_class("dim-label")
        box.pack_start(self.subtitle, False, False, 0)

        self.set_default_size(220, -1)

    # ── states ────────────────────────────────────────────────────────────
    def show_scanning(self):
        self.icon.set_markup("<span size='400%'>👤</span>")
        self.status.set_text("Looking for you…")

    def show_fail(self, n, total):
        self.icon.set_markup("<span size='400%' foreground='#c0392b'>✕</span>")
        self.status.set_text(f"Not recognised — attempt {n} of {total}")
        self._shake()

    def show_ok(self):
        self.icon.set_markup("<span size='400%' foreground='#27ae60'>✓</span>")
        self.status.set_text("Welcome back")
        GLib.timeout_add(700, Gtk.main_quit)

    def _shake(self):
        # Nudge the window left/right for ~300ms — the classic "wrong" gesture.
        x, y = self.get_position()
        offsets = [12, -12, 9, -9, 5, -5, 0]

        def step(i=[0]):
            if i[0] >= len(offsets):
                self.move(x, y)
                return False
            self.move(x + offsets[i[0]], y)
            i[0] += 1
            return True

        GLib.timeout_add(45, step)


def main():
    ap = argparse.ArgumentParser(description="AppLocker unlock feedback window")
    ap.add_argument("--app", default="", help="name of the app being unlocked")
    args = ap.parse_args()

    win = FeedbackWindow(args.app)
    win.show_all()

    def on_stdin(fd, condition):
        line = sys.stdin.readline()
        if not line:  # EOF: daemon went away — never linger on screen
            Gtk.main_quit()
            return False
        parts = line.strip().split()
        if not parts:
            return True
        cmd = parts[0]
        if cmd == "scanning":
            win.show_scanning()
        elif cmd == "fail":
            n = int(parts[1]) if len(parts) > 1 else 1
            total = int(parts[2]) if len(parts) > 2 else 3
            win.show_fail(n, total)
        elif cmd == "ok":
            win.show_ok()
        elif cmd == "close":
            Gtk.main_quit()
            return False
        return True

    GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, on_stdin)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
