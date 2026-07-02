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
        self._frame = None  # latest RGB frame (numpy), swapped in by the thread
        self._flash_until = 0.0

        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(480, 460)
        self.set_resizable(False)
        self.set_border_width(18)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)

        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>Add a new face</b> — {matcher_escape(label)}")
        box.pack_start(title, False, False, 0)

        # Camera preview.
        self.video = Gtk.DrawingArea()
        self.video.set_size_request(440, 300)
        self.video.connect("draw", self._on_draw)
        frame = Gtk.Frame()
        frame.add(self.video)
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

    def _on_draw(self, area, cr):
        w, h = area.get_allocated_width(), area.get_allocated_height()
        cr.set_source_rgb(0.08, 0.08, 0.08)
        cr.paint()
        f = self._frame
        if f is not None:
            fh, fw = f.shape[:2]
            pb = GdkPixbuf.Pixbuf.new_from_data(
                f.tobytes(), GdkPixbuf.Colorspace.RGB, False, 8, fw, fh, fw * 3)
            scale = min(w / fw, h / fh)
            cr.save()
            cr.translate((w - fw * scale) / 2, (h - fh * scale) / 2)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
            cr.restore()
        if time.time() < self._flash_until:  # green check on each capture
            cr.set_source_rgb(0.15, 0.68, 0.38)
            cr.arc(w - 26, 26, 14, 0, 6.2832)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(3)
            cr.move_to(w - 32, 26)
            cr.line_to(w - 27, 31)
            cr.line_to(w - 19, 20)
            cr.stroke()
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

        from engine import build_engine

        engine = build_engine()
        cap = cv2.VideoCapture(ARGS.camera)
        if not cap.isOpened():
            GLib.idle_add(self._finish, "Cannot open the camera.", False)
            return

        embeddings = []
        last = 0.0
        try:
            while not self._stop.is_set() and len(embeddings) < SAMPLES:
                ok, frame = cap.read()
                if not ok:
                    continue
                # Mirror the preview so it behaves like a mirror.
                rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                self._frame = rgb
                GLib.idle_add(self.video.queue_draw)

                if time.time() - last < 0.9:  # give time to follow the prompt
                    continue
                emb = engine.embed(frame)
                if emb is None:
                    continue
                embeddings.append(emb)
                last = time.time()
                self._flash_until = last + 0.5
                GLib.idle_add(self._set_progress, len(embeddings))
        finally:
            cap.release()

        if self._stop.is_set():
            return
        if len(embeddings) < max(3, SAMPLES // 2):
            GLib.idle_add(self._finish, "Too few good samples — try better lighting.", False)
            return

        enr = matcher.Enrollment(
            user=os.environ.get("USER", "owner"),
            label=self.label,
            dim=engine.dim,
            threshold=ARGS.threshold if ARGS.threshold is not None else engine.default_threshold,
            embeddings=embeddings,
            backend=engine.name,
        )
        enr.save(self.out_path)
        GLib.idle_add(self._finish, f"Saved — “{self.label}” can now unlock.", True)


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
