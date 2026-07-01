#!/usr/bin/env python3
"""AppLocker fallback auth prompt (GTK 3).

A thin, dumb dialog: it collects a PIN or the sudo password and writes the
result to **stdout**, then exits. It makes no security decision itself — the
root daemon verifies whatever comes back (see ../daemon/src/pam.rs). Keeping the
secret on a pipe (never on argv, never on screen) is the whole point.

Protocol
--------
Invoked by the daemon, e.g.::

    auth_prompt.py --app "gnome-calculator" --methods pin,sudo [--error "..."]

``--methods`` is a comma list of the fallbacks to offer (``pin`` and/or
``sudo``); at least one is always present. ``--error`` shows a red hint from the
previous failed attempt.

On stdout it writes exactly one line and exits 0::

    pin\t<secret>       # user chose PIN
    sudo\t<secret>      # user chose sudo password
    cancel              # user cancelled / closed the window

Any other exit (nonzero, no line) is treated by the daemon as a cancel.

Portability
-----------
This is the one desktop-specific surface. The KDE port reskins *this file* in
Qt; the protocol above is the contract the daemon depends on, so keep it stable.
"""

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402


class AuthPrompt(Gtk.Window):
    def __init__(self, app_name, methods, error):
        super().__init__(title="AppLocker")
        self.result = None  # ("pin"|"sudo", secret) or None

        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_keep_above(True)
        self.set_modal(True)
        self.set_resizable(False)
        self.set_border_width(18)
        self.set_default_size(360, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        title = Gtk.Label()
        title.set_markup(
            f"<b>Unlock “{GLib_escape(app_name)}”</b>"
            if app_name
            else "<b>Authentication required</b>"
        )
        title.set_xalign(0.0)
        box.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label="Face not recognised — enter your PIN or password.")
        subtitle.set_xalign(0.0)
        subtitle.get_style_context().add_class("dim-label")
        subtitle.set_line_wrap(True)
        box.pack_start(subtitle, False, False, 0)

        # Method chooser, only when both fallbacks are offered.
        self.method = methods[0]
        if len(methods) > 1:
            method_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            first = None
            labels = {"pin": "PIN", "sudo": "Password"}
            for m in methods:
                rb = Gtk.RadioButton.new_with_label_from_widget(first, labels[m])
                if first is None:
                    first = rb
                rb.connect("toggled", self._on_method_toggled, m)
                method_box.pack_start(rb, False, False, 0)
            box.pack_start(method_box, False, False, 0)

        # The secret entry: never echoes, submits on Enter.
        self.entry = Gtk.Entry()
        self.entry.set_visibility(False)
        self.entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.entry.set_placeholder_text("PIN" if self.method == "pin" else "Password")
        self.entry.set_activates_default(True)
        box.pack_start(self.entry, False, False, 0)

        if error:
            err = Gtk.Label()
            err.set_markup(f"<span foreground='#c0392b'>{GLib_escape(error)}</span>")
            err.set_xalign(0.0)
            box.pack_start(err, False, False, 0)

        # Buttons.
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self._on_cancel)
        unlock = Gtk.Button(label="Unlock")
        unlock.get_style_context().add_class("suggested-action")
        unlock.connect("clicked", self._on_unlock)
        unlock.set_can_default(True)
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(unlock, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_key)
        self.set_default(unlock)

    def _on_method_toggled(self, button, method):
        if button.get_active():
            self.method = method
            self.entry.set_placeholder_text("PIN" if method == "pin" else "Password")

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._on_cancel(None)
            return True
        return False

    def _on_unlock(self, _button):
        secret = self.entry.get_text()
        if secret == "":
            self.entry.grab_focus()
            return
        self.result = (self.method, secret)
        self.close()

    def _on_cancel(self, _button):
        self.result = None
        self.close()

    def _on_destroy(self, _widget):
        Gtk.main_quit()


def GLib_escape(text):
    """Escape Pango markup special chars without importing extra machinery."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def main():
    parser = argparse.ArgumentParser(description="AppLocker auth prompt")
    parser.add_argument("--app", default="", help="name of the app being unlocked")
    parser.add_argument(
        "--methods",
        default="pin",
        help="comma list of offered fallbacks: pin,sudo",
    )
    parser.add_argument("--error", default="", help="error hint from the last attempt")
    args = parser.parse_args()

    methods = [m for m in args.methods.split(",") if m in ("pin", "sudo")]
    if not methods:
        methods = ["sudo"]

    win = AuthPrompt(args.app, methods, args.error)
    win.show_all()
    win.present()
    Gtk.main()

    if win.result is None:
        sys.stdout.write("cancel\n")
        sys.stdout.flush()
        return 1

    method, secret = win.result
    # One line, tab-separated. secret may contain anything except a newline;
    # GTK entries can't contain a newline, so this framing is safe.
    sys.stdout.write(f"{method}\t{secret}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
