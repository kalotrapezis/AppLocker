#!/usr/bin/env python3
"""AppLocker first-run welcome + setup wizard.

Shown the first time AppLocker is opened. It brings the machine up to a working
state with a single click, then hands off to the settings window:

  1. **System libraries** — OpenCV + numpy (apt `python3-opencv opencv-data
     python3-numpy`), installed via one `pkexec` prompt. Skipped if already
     importable.
  2. **Recognition models** — the YuNet + SFace ONNX files, downloaded by
     `fetch_models.py` into ~/.config/applocker/models (no root, needs network).
  3. **Enroll your face** — launches the GUI `enroll_window.py` (live camera
     preview) so the user captures a profile.

Everything here is **userspace**: no systemd service, no PAM edits, nothing that
persists across a reboot beyond the user's own ~/.config/applocker data. Turning
the gate on system-wide is a separate, deliberate step in the settings window.

A `~/.config/applocker/.setup-done` marker records completion so the wizard only
appears once; `--force` shows it again. Run standalone with `python3 welcome.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402


# ── locating helpers (mirrors settings.py) ───────────────────────────────────

def here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def script_path(name: str) -> str:
    """Find a bundled script whether running from the repo (gui/ next to face/)
    or the installed flat layout (/usr/lib/applocker/)."""
    for cand in (os.path.join(here(), name),                 # installed: flat
                 os.path.join(here(), "..", "face", name)):   # repo: ../face
        if os.path.exists(cand):
            return cand
    return os.path.join(here(), name)


def bin_path() -> str:
    """Find the `applockerd` binary: $APPLOCKER_BIN, the installed flat layout,
    the repo build, or PATH."""
    env = os.environ.get("APPLOCKER_BIN")
    if env and os.path.exists(env):
        return env
    for cand in (os.path.join(here(), "applockerd"),                               # installed
                 os.path.join(here(), "..", "daemon", "target", "release", "applockerd")):  # repo
        if os.path.exists(cand):
            return cand
    return "applockerd"  # rely on PATH


def set_applocker_pin(sudo_pw: str, pin: str) -> tuple[bool, str]:
    """Set the AppLocker PIN as root, authorised by the sudo password from the
    dialog (no pkexec/polkit needed). `sudo -S` consumes the first stdin line as
    the password; `applockerd set-pin` then reads the two PIN lines that follow."""
    cmd = ["sudo", "-S", "-p", "", bin_path(), "set-pin"]
    try:
        r = subprocess.run(cmd, input=f"{sudo_pw}\n{pin}\n{pin}\n", text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        out = (r.stdout or "").strip()
        # sudo's own failure is usually a wrong password.
        hint = "wrong sudo password?" if "password" in out.lower() or not out else out
        return False, hint
    return True, ""


def config_home() -> str:
    return os.path.expanduser("~/.config/applocker")


def models_dir() -> str:
    return os.environ.get("APPLOCKER_MODELS", os.path.join(config_home(), "models"))


def faces_dir() -> str:
    return os.environ.get("APPLOCKER_FACES_DIR", os.path.join(config_home(), "faces"))


def setup_marker() -> str:
    return os.path.join(config_home(), ".setup-done")


# ── step checks (what's already done?) ───────────────────────────────────────

def libs_present() -> bool:
    """Can we import the face stack? Runs in a subprocess so a broken install
    can't take down this GUI's own interpreter."""
    r = subprocess.run(
        [sys.executable, "-c", "import cv2, numpy"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def models_present() -> bool:
    d = models_dir()
    try:
        onnx = [f for f in os.listdir(d) if f.endswith(".onnx")]
    except OSError:
        return False
    # Need at least the SFace recogniser and a YuNet detector.
    has_sface = any("sface" in f.lower() for f in onnx)
    has_yunet = any("yunet" in f.lower() for f in onnx)
    return has_sface and has_yunet


def enrolled() -> bool:
    try:
        if any(f.endswith(".face") for f in os.listdir(faces_dir())):
            return True
    except OSError:
        pass
    # Legacy single-file profile.
    return os.path.isfile(os.path.join(config_home(), "enrollment.json"))


# ── step actions (each returns (ok, message); run OFF the GTK thread) ─────────

def install_libs() -> tuple[bool, str]:
    """apt-install the face stack under one graphical pkexec prompt."""
    inner = ("DEBIAN_FRONTEND=noninteractive apt-get update && "
             "DEBIAN_FRONTEND=noninteractive apt-get install -y "
             "python3-opencv opencv-data python3-numpy")
    try:
        r = subprocess.run(["pkexec", "sh", "-c", inner],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return False, f"could not launch pkexec: {e}"
    if r.returncode != 0:
        tail = (r.stdout or "").strip().splitlines()[-3:]
        return False, "apt failed:\n" + "\n".join(tail)
    if not libs_present():
        return False, "installed, but OpenCV still won't import — check the log."
    return True, "OpenCV + numpy ready."


def fetch_models() -> tuple[bool, str]:
    script = script_path("fetch_models.py")
    try:
        r = subprocess.run([sys.executable, script],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return False, f"could not run fetch_models.py: {e}"
    if r.returncode != 0 or not models_present():
        tail = (r.stdout or "").strip().splitlines()[-3:]
        return False, "download failed (network?):\n" + "\n".join(tail)
    return True, "Recognition models downloaded."


def run_enrollment() -> tuple[bool, str]:
    """Launch the GUI enrollment window (with live camera preview) and wait for
    it. Blocks (camera UI), so it runs on the worker thread like the others.
    enroll_window.py sits next to us — in gui/ in the repo, flattened into
    /usr/lib/applocker/ when installed — so it's always alongside welcome.py."""
    script = os.path.join(here(), "enroll_window.py")
    try:
        r = subprocess.run([sys.executable, script, "--name", "me"])
    except OSError as e:
        return False, f"could not launch enroll_window.py: {e}"
    if r.returncode != 0 or not enrolled():
        return False, "enrollment was cancelled or captured no samples."
    return True, "Face enrolled."


# ── the step model ───────────────────────────────────────────────────────────

class Step:
    def __init__(self, key, title, subtitle, check, action):
        self.key = key
        self.title = title
        self.subtitle = subtitle
        self.check = check      # () -> bool  (already done?)
        self.action = action    # () -> (ok, msg)


STEPS = [
    Step("libs", "System libraries",
         "OpenCV + numpy for the camera pipeline (asks for your password).",
         libs_present, install_libs),
    Step("models", "Recognition models",
         "Downloads the face detector + recogniser (~38 MB, needs internet).",
         models_present, fetch_models),
    Step("enroll", "Enroll your face",
         "Captures a few angles so AppLocker can recognise you.",
         enrolled, run_enrollment),
]


# ── UI ───────────────────────────────────────────────────────────────────────

ICON_DONE = "✔"      # ✔
ICON_PENDING = "○"   # ○
ICON_ERROR = "✗"     # ✗


class StepRow(Gtk.Box):
    def __init__(self, step: Step):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.step = step
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        self.icon = Gtk.Label(label=ICON_PENDING)
        self.icon.set_width_chars(2)
        self.icon.set_xalign(0.5)
        self.spinner = Gtk.Spinner()
        icon_stack = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        icon_stack.pack_start(self.icon, False, False, 0)
        icon_stack.pack_start(self.spinner, False, False, 0)
        self.pack_start(icon_stack, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=step.title, xalign=0)
        title.set_attributes(_bold())
        self.subtitle = Gtk.Label(label=step.subtitle, xalign=0)
        self.subtitle.set_line_wrap(True)
        self.subtitle.get_style_context().add_class("dim-label")
        text.pack_start(title, False, False, 0)
        text.pack_start(self.subtitle, False, False, 0)
        self.pack_start(text, True, True, 0)

    def set_state(self, state: str, message: str | None = None):
        """state: 'pending' | 'working' | 'done' | 'error'."""
        if state == "working":
            self.icon.hide()
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()
            self.icon.show()
            self.icon.set_label(
                {"done": ICON_DONE, "error": ICON_ERROR}.get(state, ICON_PENDING)
            )
        if message:
            self.subtitle.set_label(message)


def _bold() -> "Pango.AttrList":
    al = Pango.AttrList()
    al.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    return al


class WelcomeWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Welcome to AppLocker")
        self.set_default_size(460, 420)
        self.set_border_width(24)
        self.set_position(Gtk.WindowPosition.CENTER)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.add(root)

        heading = Gtk.Label(xalign=0)
        heading.set_markup(
            "<span size='x-large' weight='bold'>Let's set up AppLocker</span>")
        blurb = Gtk.Label(xalign=0)
        blurb.set_line_wrap(True)
        blurb.set_label(
            "A few one-time steps to unlock apps and folders with your face. "
            "Nothing runs system-wide yet — that's a later choice in Settings.")
        blurb.get_style_context().add_class("dim-label")
        root.pack_start(heading, False, False, 0)
        root.pack_start(blurb, False, False, 0)
        root.pack_start(Gtk.Separator(), False, False, 0)

        self.rows = {}
        for step in STEPS:
            row = StepRow(step)
            self.rows[step.key] = row
            root.pack_start(row, False, False, 0)

        root.pack_start(Gtk.Box(), True, True, 0)  # spacer

        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        root.pack_start(self.status, False, False, 0)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        self.skip_btn = Gtk.Button(label="Skip face — use a PIN")
        self.skip_btn.connect("clicked", self.on_skip)
        self.primary = Gtk.Button(label="Set up AppLocker")
        self.primary.get_style_context().add_class("suggested-action")
        self.primary.connect("clicked", self.on_primary)
        btns.pack_start(self.skip_btn, False, False, 0)
        btns.pack_start(self.primary, False, False, 0)
        root.pack_start(btns, False, False, 0)

        self.refresh_states()

    # ── state ────────────────────────────────────────────────────────────────

    def refresh_states(self):
        """Reflect current on-disk state into the rows; update the primary btn."""
        all_done = True
        for step in STEPS:
            done = step.check()
            self.rows[step.key].set_state("done" if done else "pending")
            all_done = all_done and done
        if all_done:
            self.primary.set_label("Finish")
            self.status.set_markup("<b>Everything's ready.</b> Click Finish to open Settings.")
        return all_done

    def on_primary(self, _btn):
        if self.refresh_states():   # already all done → finish
            self.finish()
            return
        self.primary.set_sensitive(False)
        self.skip_btn.set_sensitive(False)
        self.status.set_label("Working…")
        threading.Thread(target=self._run_all, daemon=True).start()

    def _run_all(self):
        """Worker thread: run each not-yet-done step in order, stopping on the
        first failure. All UI updates are marshalled back via GLib.idle_add."""
        for step in STEPS:
            if step.check():
                GLib.idle_add(self.rows[step.key].set_state, "done")
                continue
            GLib.idle_add(self.rows[step.key].set_state, "working")
            ok, msg = step.action()
            GLib.idle_add(self.rows[step.key].set_state,
                          "done" if ok else "error", msg)
            if not ok:
                GLib.idle_add(self._on_failed, step, msg)
                return
        GLib.idle_add(self._on_all_done)

    def _on_failed(self, step: Step, msg: str):
        # Face is optional: if enrollment was skipped/cancelled, offer a PIN
        # instead of a dead end (this is the "user skips face" path).
        if step.key == "enroll":
            self.status.set_markup("<b>No face enrolled.</b> Set a PIN to unlock instead.")
            if self._pin_dialog():
                self.finish()
                return
        self.status.set_markup(
            f"<b>Couldn't finish “{GLib.markup_escape_text(step.title)}”.</b> "
            "Fix the issue and try again.")
        self.primary.set_label("Try again")
        self.primary.set_sensitive(True)
        self.skip_btn.set_sensitive(True)

    def _on_all_done(self):
        self.status.set_markup("<b>All set!</b> Opening Settings…")
        write_marker()
        self.primary.set_label("Finish")
        self.primary.set_sensitive(True)
        self.skip_btn.set_sensitive(True)
        self.finish()

    def finish(self):
        write_marker()
        launch_settings()
        self.close()

    # ── skip face → set a PIN instead ─────────────────────────────────────────

    def on_skip(self, _btn):
        """Face is optional. Skipping it opens the PIN setup so there's still an
        unlock method (and no camera needed — ideal in a VM). If they set a PIN,
        we're done; if they cancel it, just close (the sudo password still works
        as a fallback at the unlock prompt)."""
        if self._pin_dialog():
            self.finish()
        else:
            self.close()

    def _pin_dialog(self) -> bool:
        """The 'New PIN setup' dialog: sudo password + PIN + confirm → Apply.
        Returns True once a PIN is set."""
        dlg = Gtk.Dialog(title="New PIN setup", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=16)
        dlg.get_content_area().add(grid)

        def row(r, label, placeholder):
            lbl = Gtk.Label(label=label, xalign=0)
            e = Gtk.Entry()
            e.set_visibility(False)
            e.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            e.set_placeholder_text(placeholder)
            e.set_activates_default(True)
            e.set_hexpand(True)
            e.set_width_chars(24)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(e, 1, r, 1, 1)
            return e

        sudo_e = row(0, "Sudo password", "your login/sudo password")
        pin_e = row(1, "Set new PIN", "PIN")
        confirm_e = row(2, "Confirm new PIN", "repeat PIN")
        err = Gtk.Label(xalign=0)
        grid.attach(err, 0, 3, 2, 1)
        dlg.show_all()

        done = False
        while True:
            if dlg.run() != Gtk.ResponseType.OK:
                break
            sudo_pw, pin, confirm = sudo_e.get_text(), pin_e.get_text(), confirm_e.get_text()
            if not sudo_pw:
                err.set_markup("<span foreground='#c0392b'>Enter your sudo password.</span>")
                continue
            if not pin:
                err.set_markup("<span foreground='#c0392b'>PIN can't be empty.</span>")
                continue
            if pin != confirm:
                err.set_markup("<span foreground='#c0392b'>PINs don't match.</span>")
                continue
            err.set_markup("<i>Setting PIN…</i>")
            while Gtk.events_pending():
                Gtk.main_iteration()
            ok, msg = set_applocker_pin(sudo_pw, pin)
            if ok:
                done = True
                break
            err.set_markup(f"<span foreground='#c0392b'>Couldn't set PIN — "
                           f"{GLib.markup_escape_text(msg)}</span>")
        dlg.destroy()
        return done


# ── completion marker + handoff ──────────────────────────────────────────────

def write_marker():
    try:
        os.makedirs(config_home(), exist_ok=True)
        with open(setup_marker(), "w") as f:
            f.write("setup completed by welcome.py\n")
    except OSError:
        pass


def launch_settings():
    settings = os.path.join(here(), "settings.py")
    if os.path.exists(settings):
        try:
            subprocess.Popen([sys.executable, settings])
        except (OSError, subprocess.SubprocessError):
            pass


def first_run_needed() -> bool:
    """True if the wizard should be shown (no marker, and something's missing)."""
    if os.path.exists(setup_marker()):
        return False
    return not (libs_present() and models_present() and enrolled())


def main() -> int:
    if "--check" in sys.argv:
        # For a launcher: exit 0 if the wizard is needed, 1 if not.
        return 0 if first_run_needed() else 1
    if "--force" not in sys.argv and not first_run_needed():
        # Nothing to do — go straight to settings.
        launch_settings()
        return 0
    Gtk.Window.set_default_icon_name("applocker")  # window/taskbar icon
    win = WelcomeWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    # Spinners start hidden; show_all revealed them.
    for row in win.rows.values():
        row.spinner.hide()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
