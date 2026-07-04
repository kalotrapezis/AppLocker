#!/usr/bin/env python3
"""Guided face enrollment window (GTK 3) — the "Add a new face" experience.

A live camera preview with step-by-step instructions ("Look straight", "Turn
your head slightly left", …), progress dots, and a green check per captured
sample. Saves a named profile exactly like face/enroll.py (which remains the
headless/terminal path); the settings window spawns THIS.

    python3 gui/enroll_window.py --name "new haircut"

Exit codes: 0 saved, 1 cancelled/failed. Native GTK widgets → Mint-Y theme.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "face"))

import matcher  # noqa: E402

SAMPLES = 8
# One instruction per capture — small pose variety makes the match reliable.
STEPS = [
    "Look straight at the camera",
    "Look straight at the camera",
    "Turn your head slightly left",
    "Turn your head slightly left",
    "Turn your head slightly right",
    "Turn your head slightly right",
    "Tilt your chin up a little",
    "Tilt your chin down a little",
]


class EnrollWindow(Gtk.Window):
    def __init__(self, label, out_path):
        super().__init__(title="Add a new face")
        self.label = label
        self.out_path = out_path
        self.saved = False
        self._stop = threading.Event()

        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(480, 460)
        self.set_resizable(False)
        self.set_border_width(18)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)

        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>Add a new face</b> — {matcher_escape(label)}")
        box.pack_start(title, False, False, 0)

        # Camera preview. A Gtk.Image we set a fresh pixbuf on each frame (from the
        # main thread) — more reliable across X11/Wayland than a DrawingArea +
        # manual cairo draw, which rendered blank on KDE/Wayland. The green "got it"
        # check rides an overlay instead of a per-paint cairo arc.
        self.image = Gtk.Image()
        self.image.set_size_request(440, 300)
        self._check = Gtk.Image.new_from_icon_name(
            "emblem-ok-symbolic", Gtk.IconSize.DIALOG)
        self._check.set_halign(Gtk.Align.END)
        self._check.set_valign(Gtk.Align.START)
        self._check.set_margin_top(8)
        self._check.set_margin_end(8)
        self._check.set_no_show_all(True)  # hidden until a capture flashes it
        overlay = Gtk.Overlay()
        overlay.add(self.image)
        overlay.add_overlay(self._check)
        frame = Gtk.Frame()
        frame.add(overlay)
        box.pack_start(frame, False, False, 0)

        # Current instruction — styled like a suggestion chip.
        self.instruction = Gtk.Label(label="Starting camera…")
        chip = Gtk.Button()
        chip.add(self.instruction)
        chip.set_sensitive(False)
        chip.get_style_context().add_class("suggested-action")
        chip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        chip_row.pack_start(chip, False, False, 0)
        box.pack_start(chip_row, False, False, 0)

        # Progress dots + counter.
        prog = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.dots = Gtk.Label(xalign=0)
        prog.pack_start(self.dots, False, False, 0)
        self.counter = Gtk.Label(label=f"captured 0 of {SAMPLES}")
        prog.pack_start(self.counter, False, False, 0)
        box.pack_start(prog, False, False, 0)

        hint = Gtk.Label(
            xalign=0,
            label="Move slowly — we capture a few angles for a reliable match.")
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.set_halign(Gtk.Align.END)
        row.pack_start(cancel, False, False, 0)
        box.pack_start(row, False, False, 0)

        self._set_progress(0)
        self.connect("destroy", self._on_destroy)
        threading.Thread(target=self._capture_loop, daemon=True).start()

    # ── UI updates (main thread only) ─────────────────────────────────────
    def _set_progress(self, n):
        filled = "●" * n + "○" * (SAMPLES - n)
        self.dots.set_markup(f"<span size='large'>{filled}</span>")
        self.counter.set_text(f"captured {n} of {SAMPLES}")
        if n < SAMPLES:
            self.instruction.set_text(STEPS[n])

    def _show_frame(self, rgb):
        """Paint one RGB frame into the preview (main thread). new_from_bytes (not
        new_from_data) keeps the pixel buffer alive for the pixbuf's lifetime."""
        fh, fw = rgb.shape[:2]
        data = GLib.Bytes.new(rgb.tobytes())
        pb = GdkPixbuf.Pixbuf.new_from_bytes(
            data, GdkPixbuf.Colorspace.RGB, False, 8, fw, fh, fw * 3)
        w = self.image.get_allocated_width() or 440
        h = self.image.get_allocated_height() or 300
        scale = min(w / fw, h / fh)
        if scale > 0:
            pb = pb.scale_simple(max(1, int(fw * scale)), max(1, int(fh * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        self.image.set_from_pixbuf(pb)
        return False

    def _flash_check(self):
        """Show the green check briefly after a captured sample."""
        self._check.show()
        GLib.timeout_add(500, self._check.hide)  # hide() returns None → one-shot
        return False

    def _finish(self, message, ok):
        self.instruction.set_text(message)
        if ok:
            self.saved = True
            GLib.timeout_add(1200, self.close)

    def _on_destroy(self, _w):
        self._stop.set()
        Gtk.main_quit()

    # ── camera thread ─────────────────────────────────────────────────────
    def _capture_loop(self):
        import cv2
        import numpy as np

        # Load camera first, independent of engine — user should see what camera sees
        # even if recognition engine fails to load.
        try:
            cap = cv2.VideoCapture(ARGS.camera)
            if not cap.isOpened():
                GLib.idle_add(self._finish, 'Cannot open the camera.', False)
                return
        except Exception as e:
            GLib.idle_add(self._finish, f'Cannot start camera: {e}', False)
            return

        # Load recognizer engine. If it fails, we still show video but can't embed.
        engine = None
        try:
            from engine import build_engine
            engine = build_engine()
        except Exception as e:
            err = str(e).split('\n')[0]  # first line only, avoid spam
            GLib.idle_add(self._finish, f'Cannot start the recognizer: {err}', False)
            # Continue anyway — camera preview will show, but face capture unavailable.

        embeddings = []
        last = 0.0
        try:
            while not self._stop.is_set() and len(embeddings) < SAMPLES:
                ok, frame = cap.read()
                if not ok:
                    continue
                # Mirror the preview so it behaves like a mirror. Force a
                # contiguous buffer so tobytes()/rowstride line up in the pixbuf.
                rgb = np.ascontiguousarray(
                    cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB))
                GLib.idle_add(self._show_frame, rgb)

                if engine is None:  # engine failed; just show video, don't embed
                    continue
                if time.time() - last < 0.9:  # give time to follow the prompt
                    continue
                emb = engine.embed(frame)
                if emb is None:
                    continue
                embeddings.append(emb)
                last = time.time()
                GLib.idle_add(self._flash_check)
                GLib.idle_add(self._set_progress, len(embeddings))
        except Exception as e:
            err = str(e).split('\n')[0]
            GLib.idle_add(self._finish, f'Camera error: {err}', False)
            return
        finally:
            cap.release()

        if self._stop.is_set():
            return
        if engine is None:
            return  # engine load failed; already reported via _finish
        if len(embeddings) < max(3, SAMPLES // 2):
            GLib.idle_add(self._finish, 'Too few good samples — try better lighting.', False)
            return

        enr = matcher.Enrollment(
            user=os.environ.get('USER', 'owner'),
            label=self.label,
            dim=engine.dim,
            threshold=ARGS.threshold if ARGS.threshold is not None else engine.default_threshold,
            embeddings=embeddings,
            backend=engine.name,
        )
        enr.save(self.out_path)
        GLib.idle_add(self._finish, f'Saved — “{self.label}” can now unlock.', True)


def ask_name():
    """Small dialog for the profile name when --name wasn't given."""
    dlg = Gtk.Dialog(title="Name this face")
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_position(Gtk.WindowPosition.CENTER)
    entry = Gtk.Entry()
    entry.set_placeholder_text("e.g. me, with glasses, new haircut")
    entry.set_activates_default(True)
    entry.set_margin_top(8)
    entry.set_margin_bottom(8)
    entry.set_margin_start(10)
    entry.set_margin_end(10)
    dlg.get_content_area().add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text().strip()
    dlg.destroy()
    return text if resp == Gtk.ResponseType.OK else ""


def matcher_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="AppLocker guided face enrollment")
    ap.add_argument("--name", default=None,
                    help="profile display name (asked in a dialog if omitted)")
    ap.add_argument("--faces-dir", default=None)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=None)
    ARGS = ap.parse_args()

    name = ARGS.name or ask_name()
    if not name:
        return 1

    faces_dir = ARGS.faces_dir or matcher.default_faces_dir()
    out = os.path.join(faces_dir, matcher.slugify(name) + ".face")
    existing = matcher.list_profiles(faces_dir=faces_dir, legacy="")
    if len(existing) >= matcher.MAX_FACES and not os.path.exists(out):
        print(f"error: already {matcher.MAX_FACES} faces enrolled", file=sys.stderr)
        return 1

    win = EnrollWindow(name, out)
    win.show_all()
    Gtk.main()
    return 0 if win.saved else 1


if __name__ == "__main__":
    sys.exit(main())
