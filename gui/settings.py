#!/usr/bin/env python3
"""AppLocker settings window (GTK 3) — the UI over the daemon's CLI.

This is the window from the design sketch: manage locked apps and folders, add a
face, and choose the auth policy (face / PIN / sudo / re-auth). It is a **thin
client** — it never decides security itself; it reads the daemon's config files
and shells out to `applockerd` for every change.

Theming: built entirely from **native GTK widgets**, so it follows the active
Mint-Y theme (light/dark + accent) automatically — no hardcoded colours, no
custom CSS fighting the theme. (Same approach as the auth prompt; the KDE port
reskins in Qt.)

Tamper protection: opening this window **requires auth** (`applockerd
authorize`), because otherwise anyone could just open it and remove the locks or
enrol their own face. Privileged changes go through `pkexec applockerd …`.

Reads are unprivileged (the config files are world-readable); writes need root.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

# Face profiles are the user's own data (in ~/.config/applocker/faces), so we
# read/manage them directly via the face-pipeline's matcher helpers — no daemon,
# no root. matcher imports only the stdlib (no OpenCV), so this is cheap.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "face"))
import matcher  # noqa: E402

# The vault helper (gocryptfs folder locking). In the installed flat layout it
# sits next to us; in the repo it's ../vault. Userspace, no root.
for _vp in (os.path.dirname(os.path.abspath(__file__)),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vault")):
    if os.path.exists(os.path.join(_vp, "vault.py")):
        sys.path.insert(0, _vp)
        break
import vault as vaultlib  # noqa: E402


# ── locating the daemon + its config ─────────────────────────────────────────

def bin_path() -> str:
    """Find the `applockerd` binary: $APPLOCKER_BIN, the repo build, or PATH."""
    env = os.environ.get("APPLOCKER_BIN")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.join(here, "..", "daemon", "target", "release", "applockerd")
    if os.path.exists(repo):
        return repo
    return "applockerd"  # rely on PATH (installed layout)


def script_path(name: str) -> str:
    """Locate a bundled helper script whether we're running from the repo
    (gui/ next to face/) or the installed flat layout (/usr/lib/applocker/)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, name),               # installed: flat
                 os.path.join(here, "..", "face", name)):  # repo: ../face
        if os.path.exists(cand):
            return cand
    return os.path.join(here, name)


def cfg_path(env: str, default: str) -> str:
    return os.environ.get(env, default)


def read_config() -> dict:
    """Parse /etc/applocker/config into {face, allow_pin, allow_sudo, reauth}."""
    out = {"face": False, "pin": True, "sudo": True, "reauth_every": False,
           "attention": False, "attention_interval": 2, "attention_ac_only": False}
    path = cfg_path("APPLOCKER_CONFIG", "/etc/applocker/config")
    try:
        with open(path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = (p.strip().lower() for p in line.split("=", 1))
                if k == "face":
                    out["face"] = v in ("on", "true", "1", "yes")
                elif k == "fallback":
                    toks = [t.strip() for t in v.replace("+", ",").split(",")]
                    if "both" in toks or "all" in toks:
                        out["pin"], out["sudo"] = True, True
                    else:
                        out["pin"] = "pin" in toks
                        out["sudo"] = "sudo" in toks or "password" in toks
                elif k == "reauth":
                    out["reauth_every"] = v in ("always", "every", "everytime")
                elif k == "attention":
                    out["attention"] = v in ("on", "true", "1", "yes")
                elif k == "attention_ac_only":
                    out["attention_ac_only"] = v in ("on", "true", "1", "yes")
                elif k == "attention_interval":
                    try:
                        n = int(v)
                        if n in (2, 5, 10, 15, 30):
                            out["attention_interval"] = n
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def read_locked_apps() -> list:
    path = cfg_path("APPLOCKER_LOCKED_APPS", "/etc/applocker/locked-apps")
    return _read_tsv(path, fields=3)


def _read_tsv(path: str, fields: int) -> list:
    rows = []
    try:
        with open(path) as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= fields:
                    rows.append([p.strip() for p in parts])
    except OSError:
        pass
    return rows


def enrolled_face() -> bool:
    path = os.environ.get(
        "APPLOCKER_FACE_ENROLLMENT",
        os.path.expanduser("~/.config/applocker/owner.face"),
    )
    return os.path.exists(path)


def list_installed() -> list:
    """[(kind, key, name)] from the daemon's porcelain output."""
    try:
        out = subprocess.run(
            [bin_path(), "list-installed", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    apps = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            apps.append(tuple(parts))
    return apps


# ── talking to the daemon ────────────────────────────────────────────────────

def authorize() -> bool:
    """Gate opening the window. Returns True only if the auth routine passes."""
    try:
        rc = subprocess.run([bin_path(), "authorize", "AppLocker settings"]).returncode
        return rc == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_privileged(args: list) -> bool:
    """Run a mutating `applockerd` subcommand as root. Uses pkexec unless
    $APPLOCKER_NO_PKEXEC=1 (for dev/testing with user-writable config paths)."""
    cmd = [bin_path(), *args]
    if os.environ.get("APPLOCKER_NO_PKEXEC") != "1":
        cmd = ["pkexec", *cmd]
    try:
        return subprocess.run(cmd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_privileged_batch(ops: list) -> bool:
    """Apply several mutating `applockerd` subcommands under **one** auth prompt.
    `ops` is a list of arg-lists. Runs them as `pkexec sh -c 'cmd1 && cmd2 …'` so
    the user authenticates once for the whole Apply, not once per change."""
    if not ops:
        return True
    binp = bin_path()
    script = " && ".join(
        " ".join(shlex.quote(tok) for tok in [binp, *op]) for op in ops
    )
    cmd = ["sh", "-c", script]
    if os.environ.get("APPLOCKER_NO_PKEXEC") != "1":
        cmd = ["pkexec", *cmd]
    try:
        return subprocess.run(cmd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def spawn(args: list) -> None:
    try:
        subprocess.Popen(args)
    except (OSError, subprocess.SubprocessError):
        pass


# ── the gate service (start/stop/status) ─────────────────────────────────────

SERVICE = "applockerd.service"


def service_present() -> bool:
    """Is the systemd unit installed (i.e. running from the .deb, not the repo)?"""
    try:
        r = subprocess.run(["systemctl", "list-unit-files", SERVICE],
                           capture_output=True, text=True)
        return SERVICE in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def service_active() -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", SERVICE],
                             capture_output=True, text=True).stdout.strip()
        return out == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def dev_mode() -> bool:
    """Dev build marker: while it exists, the daemon refuses to run the
    system-wide gate (so it can't freeze the machine). The service will start
    then immediately exit doing nothing — see _on_service_toggle."""
    return os.path.exists("/etc/applocker/dev-mode")


def set_service(active: bool) -> bool:
    """Start or stop the gate service (root, via pkexec unless dev mode)."""
    cmd = ["systemctl", "start" if active else "stop", SERVICE]
    if os.environ.get("APPLOCKER_NO_PKEXEC") != "1":
        cmd = ["pkexec", *cmd]
    try:
        return subprocess.run(cmd).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ── the window ───────────────────────────────────────────────────────────────

class SettingsWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="AppLocker settings")
        self.set_default_size(460, 640)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("destroy", Gtk.main_quit)

        header = Gtk.HeaderBar(title="AppLocker settings", show_close_button=True)
        header.set_subtitle("unlocked — re-locks on close")
        self.set_titlebar(header)

        # Policy changes are staged, not applied per-toggle. `_persisted` is the
        # on-disk truth; `_loading` guards programmatic control updates so they
        # don't count as edits.
        self._persisted = read_config()
        self._loading = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)
        self._build_service_bar(outer)  # pinned at the top

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(scroller, True, True, 0)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                           border_width=18)
        scroller.add(self.box)

        self._build_face_section()
        self._build_apps_section()
        self._build_vault_section()
        self._build_policy_section()
        self._build_apply_bar(outer)  # pinned at the bottom

    # -- service bar ---------------------------------------------------------

    def _build_service_bar(self, container):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                      border_width=10)
        self.service_label = Gtk.Label(xalign=0)
        self.service_label.set_use_markup(True)
        bar.pack_start(self.service_label, True, True, 0)
        self.service_btn = Gtk.Button(label="Start")
        self.service_btn.connect("clicked", self._on_service_toggle)
        bar.pack_start(self.service_btn, False, False, 0)
        container.pack_start(bar, False, False, 0)
        container.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                             False, False, 0)
        self._refresh_service()
        # Keep the label live if the service changes state elsewhere.
        GLib.timeout_add_seconds(3, self._service_tick)

    def _service_tick(self):
        self._refresh_service()
        return True  # repeat

    def _refresh_service(self):
        if not service_present():
            self.service_label.set_markup(
                "<b>AppLocker service</b>  —  not installed (run from a .deb to use it)")
            self.service_btn.set_label("Start")
            self.service_btn.set_sensitive(False)
            return
        # In dev mode the service can't actually run the system-wide gate — say so
        # instead of offering a Start button that silently does nothing.
        if dev_mode():
            self.service_label.set_markup(
                "<b>AppLocker service</b>  —  <span foreground='#e67e22'>dev mode</span>"
                "  (system-wide gate off — can't freeze)")
            self.service_btn.set_label("Why?")
            self.service_btn.set_sensitive(True)
            return
        self.service_btn.set_sensitive(True)
        if service_active():
            self.service_label.set_markup(
                "<b>AppLocker service</b>  —  <span foreground='#27ae60'>running</span>")
            self.service_btn.set_label("Stop")
        else:
            self.service_label.set_markup(
                "<b>AppLocker service</b>  —  <span foreground='#c0392b'>stopped</span>")
            self.service_btn.set_label("Start")

    def _on_service_toggle(self, _btn):
        if dev_mode():
            self._toast(
                "Dev mode is on, so the system-wide app/file gate is disabled — a "
                "bug there could freeze the whole machine (it has before). That's "
                "why the service starts then immediately stops doing nothing.\n\n"
                "• Encrypted “Private folder” locking works normally right now.\n"
                "• To test app-locking safely, use the sandbox in a terminal:\n"
                "    sudo applocker-test-scope up\n"
                "    sudo applocker-test-scope gate   (Ctrl-C to stop)\n"
                "    sudo applocker-test-scope down\n\n"
                "The real system-wide gate is deliberately deferred until it's "
                "proven safe. (It lives behind /etc/applocker/dev-mode.)")
            return
        set_service(not service_active())
        self._refresh_service()

    # -- sections ------------------------------------------------------------

    def _section(self, title: str) -> Gtk.Box:
        frame = Gtk.Frame(label=title)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        border_width=10)
        frame.add(inner)
        self.box.pack_start(frame, False, False, 0)
        return inner

    def _switch_row(self, label: str, active: bool):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=0)
        row.pack_start(lbl, True, True, 0)
        sw = Gtk.Switch(active=active)
        sw.set_valign(Gtk.Align.CENTER)
        row.pack_start(sw, False, False, 0)
        return row, sw

    def _build_face_section(self):
        cfg = read_config()
        box = self._section("Face unlock")

        row, self.face_switch = self._switch_row("Use face unlock", cfg["face"])
        self.face_switch.connect("notify::active", self._on_policy_changed)
        box.pack_start(row, False, False, 0)

        # The list of enrolled face profiles (up to MAX_FACES), each with a name
        # and an X to delete. These are the user's own files — no root needed.
        self.faces_list = Gtk.ListBox()
        self.faces_list.set_selection_mode(Gtk.SelectionMode.NONE)
        box.pack_start(self.faces_list, False, False, 0)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_face_btn = Gtk.Button(label="Add a new face…")
        self.add_face_btn.connect("clicked", self._on_add_face)
        btns.pack_start(self.add_face_btn, False, False, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic",
                                                Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh after enrolling")
        refresh.connect("clicked", lambda _b: self._refresh_faces())
        btns.pack_start(refresh, False, False, 0)
        box.pack_start(btns, False, False, 0)

        hint = Gtk.Label(xalign=0, label="Add a few looks (glasses, new haircut) — "
                         "any of them will unlock.")
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)
        self._refresh_faces()

    def _refresh_faces(self):
        self._clear(self.faces_list)
        try:
            profiles = matcher.list_profiles()  # faces dir + legacy owner.face
        except Exception:
            profiles = []
        if not profiles:
            self.faces_list.add(self._info_row("No faces enrolled yet."))
        for path, enr in profiles:
            self.faces_list.add(self._list_row(
                enr.display_name(), lambda _b, p=path: self._delete_face(p)))
        # Cap at MAX_FACES.
        full = len(profiles) >= matcher.MAX_FACES
        self.add_face_btn.set_sensitive(not full)
        self.add_face_btn.set_label(
            f"Add a new face…  ({len(profiles)}/{matcher.MAX_FACES})")
        self.faces_list.show_all()

    def _delete_face(self, path: str):
        try:
            os.remove(path)
        except OSError as e:
            self._toast(f"Couldn't delete: {e}")
        self._refresh_faces()

    def _on_add_face(self, _btn):
        # The enrollment window asks for the name itself (one dialog, one owner).
        here = os.path.dirname(os.path.abspath(__file__))
        enroll = os.path.join(here, "enroll_window.py")

        def wait_and_refresh(proc):
            proc.wait()
            GLib.idle_add(self._refresh_faces)

        proc = subprocess.Popen([sys.executable, enroll])
        threading.Thread(target=wait_and_refresh, args=(proc,), daemon=True).start()

    def _ask_text(self, title: str, placeholder: str):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
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

    def _build_apps_section(self):
        box = self._section("Locked apps")
        self.apps_list = Gtk.ListBox()
        self.apps_list.set_selection_mode(Gtk.SelectionMode.NONE)
        box.pack_start(self.apps_list, False, False, 0)

        add = Gtk.Button(label="Add app…")
        add.connect("clicked", self._on_add_app)
        add.set_halign(Gtk.Align.START)
        box.pack_start(add, False, False, 0)
        self._refresh_apps()

    # -- Private folder (encrypted vault) ------------------------------------

    def _standard_vault(self):
        """The registry entry for the standard ~/Private vault, or None."""
        reg = vaultlib.load_registry()
        vid = vaultlib.resolve(reg, vaultlib.standard_path())
        return (vid, reg[vid]) if vid else (None, None)

    def _build_vault_section(self):
        self.vault_box = self._section("Private folder")
        self._refresh_vault()

    def _refresh_vault(self):
        box = self.vault_box
        for child in box.get_children():
            box.remove(child)
        path = vaultlib.standard_path()
        vid, v = self._standard_vault()

        if v is None:
            # OFF by default — offer to create the encrypted Private folder.
            lbl = Gtk.Label(xalign=0, label=(
                "File locking is off. Create an encrypted <b>Private</b> folder in "
                "your home — locked, it's empty and unreadable; unlocked, your files "
                "are there. It can't freeze the system."))
            lbl.set_use_markup(True)
            lbl.set_line_wrap(True)
            box.pack_start(lbl, False, False, 0)
            self.vault_hide_chk = Gtk.CheckButton(
                label="Hide the folder in the file manager while locked")
            self.vault_hide_chk.set_active(True)
            box.pack_start(self.vault_hide_chk, False, False, 0)
            btn = Gtk.Button(label="Enable file lock")
            btn.get_style_context().add_class("suggested-action")
            btn.set_halign(Gtk.Align.START)
            btn.connect("clicked", self._on_vault_enable)
            box.pack_start(btn, False, False, 0)
            box.show_all()
            return

        mounted = vaultlib.is_mounted(v["mount"])
        state = ("<span foreground='#c0392b'>Locked</span>" if not mounted
                 else "<span foreground='#27ae60'>Unlocked</span>")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info = Gtk.Label(xalign=0)
        info.set_markup(f"<b>{GLib.markup_escape_text(v['name'])}</b>  —  {state}")
        row.pack_start(info, True, True, 0)
        toggle = Gtk.Button(label="Lock" if mounted else "Unlock")
        toggle.connect("clicked", self._on_vault_toggle)
        row.pack_start(toggle, False, False, 0)
        box.pack_start(row, False, False, 0)

        pathlbl = Gtk.Label(xalign=0, label=v["mount"])
        pathlbl.get_style_context().add_class("dim-label")
        box.pack_start(pathlbl, False, False, 0)

        hrow, self.vault_hide_switch = self._switch_row(
            "Hide folder when locked", bool(v.get("hide")))
        self.vault_hide_switch.connect("notify::active", self._on_vault_hide_toggled)
        box.pack_start(hrow, False, False, 0)

        delbtn = Gtk.Button(label="Delete & disable file lock")
        delbtn.get_style_context().add_class("destructive-action")
        delbtn.set_halign(Gtk.Align.START)
        delbtn.connect("clicked", self._on_vault_delete)
        box.pack_start(delbtn, False, False, 0)
        box.show_all()

    def _on_vault_enable(self, _btn):
        hide = self.vault_hide_chk.get_active()
        ns = argparse.Namespace(path=vaultlib.standard_path(), name="Private",
                                unlock=True, hide=hide)
        if vaultlib.cmd_create(ns) != 0:
            self._toast("Couldn't create the Private folder — is gocryptfs installed?")
        self._refresh_vault()

    def _on_vault_toggle(self, _btn):
        path = vaultlib.standard_path()
        vid, v = self._standard_vault()
        if v is None:
            return
        ns = argparse.Namespace(key=path)
        if vaultlib.is_mounted(v["mount"]):
            vaultlib.cmd_lock(ns)
        else:
            # The settings window is already behind an auth check (opened via
            # `applockerd authorize`), so unlocking here doesn't re-prompt yet.
            vaultlib.cmd_unlock(ns)
        self._refresh_vault()

    def _on_vault_hide_toggled(self, switch, _param):
        vaultlib.cmd_hide(argparse.Namespace(
            key=vaultlib.standard_path(), off=not switch.get_active()))

    def _on_vault_delete(self, _btn):
        vid, v = self._standard_vault()
        if v is None:
            return
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Delete the Private folder?")
        confirm.format_secondary_text(
            "This permanently deletes its encrypted contents. Unlock and copy out "
            "anything you want to keep first.")
        go = confirm.run() == Gtk.ResponseType.OK
        confirm.destroy()
        if not go:
            return
        path = vaultlib.standard_path()
        if vaultlib.is_mounted(v["mount"]):
            vaultlib.cmd_lock(argparse.Namespace(key=path))
        rc = vaultlib.cmd_destroy(argparse.Namespace(key=path, force=True))
        if rc != 0:
            self._toast("Couldn't delete the vault (is a file still open in it?).")
        self._refresh_vault()

    def _build_policy_section(self):
        cfg = read_config()
        box = self._section("Unlock alternatives")

        prow, self.pin_switch = self._switch_row("Use PIN", cfg["pin"])
        self.pin_switch.connect("notify::active", self._on_fallback_toggled, "pin")
        box.pack_start(prow, False, False, 0)

        srow, self.sudo_switch = self._switch_row("Use sudo password", cfg["sudo"])
        self.sudo_switch.connect("notify::active", self._on_fallback_toggled, "sudo")
        box.pack_start(srow, False, False, 0)

        note = Gtk.Label(xalign=0,
                         label="Keep at least one on, so a broken camera can't lock you out.")
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)

        rrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rrow.pack_start(Gtk.Label(label="Re-ask for auth", xalign=0), True, True, 0)
        self.reauth = Gtk.ComboBoxText()
        self.reauth.append("session", "once per session")
        self.reauth.append("always", "every launch")
        self.reauth.set_active_id("always" if cfg["reauth_every"] else "session")
        self.reauth.connect("changed", self._on_policy_changed)
        rrow.pack_start(self.reauth, False, False, 0)
        box.pack_start(rrow, False, False, 0)

        arow, self.attention_switch = self._switch_row(
            "Lock when I leave (presence watcher)", cfg["attention"])
        self.attention_switch.connect("notify::active", self._on_policy_changed)
        box.pack_start(arow, False, False, 0)
        anote = Gtk.Label(xalign=0, label="The camera stays off while you work. "
                          "Once you're idle it takes a quick photo now and then; "
                          "if you're gone it locks the session. Any face counts — "
                          "it never checks who you are.")
        anote.get_style_context().add_class("dim-label")
        anote.set_line_wrap(True)
        box.pack_start(anote, False, False, 0)

        irow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        irow.pack_start(Gtk.Label(label="Check every", xalign=0), True, True, 0)
        self.attention_interval = Gtk.ComboBoxText()
        for m in (2, 5, 10, 15, 30):
            self.attention_interval.append(str(m), f"{m} minutes")
        self.attention_interval.set_active_id(str(cfg["attention_interval"]))
        self.attention_interval.connect("changed", self._on_policy_changed)
        irow.pack_start(self.attention_interval, False, False, 0)
        box.pack_start(irow, False, False, 0)

        acrow, self.attention_ac_switch = self._switch_row(
            "Only when plugged in (pause on battery)", cfg["attention_ac_only"])
        self.attention_ac_switch.connect("notify::active", self._on_policy_changed)
        box.pack_start(acrow, False, False, 0)

    # -- refreshers ----------------------------------------------------------

    def _clear(self, listbox: Gtk.ListBox):
        for child in listbox.get_children():
            listbox.remove(child)

    def _list_row(self, text: str, on_remove):
        row = Gtk.ListBoxRow()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, border_width=4)
        hb.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        rm = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        rm.set_relief(Gtk.ReliefStyle.NONE)
        rm.connect("clicked", on_remove)
        hb.pack_start(rm, False, False, 0)
        row.add(hb)
        return row

    def _refresh_apps(self):
        self._clear(self.apps_list)
        rows = read_locked_apps()
        if not rows:
            self.apps_list.add(self._info_row("No apps locked."))
        for kind, key, name in rows:
            label = name if kind == "native" else f"{name}  ({kind})"
            self.apps_list.add(self._list_row(
                label, lambda _b, k=key: self._remove_app(k)))
        self.apps_list.show_all()

    def _info_row(self, text: str):
        row = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class("dim-label")
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(4)
        row.add(lbl)
        return row

    # -- staged policy: Apply / Revert ---------------------------------------

    def _build_apply_bar(self, container):
        container.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                             False, False, 0)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      border_width=10)
        self.dirty_label = Gtk.Label(xalign=0)
        self.dirty_label.get_style_context().add_class("dim-label")
        bar.pack_start(self.dirty_label, True, True, 0)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", self._revert)
        bar.pack_start(self.revert_btn, False, False, 0)
        self.apply_btn = Gtk.Button(label="Apply changes")
        self.apply_btn.get_style_context().add_class("suggested-action")
        self.apply_btn.connect("clicked", self._apply)
        bar.pack_start(self.apply_btn, False, False, 0)
        container.pack_start(bar, False, False, 0)
        self._update_apply_state()

    def _snapshot_ui(self) -> dict:
        return {
            "face": self.face_switch.get_active(),
            "pin": self.pin_switch.get_active(),
            "sudo": self.sudo_switch.get_active(),
            "reauth_every": self.reauth.get_active_id() == "always",
            "attention": self.attention_switch.get_active(),
            "attention_interval": int(self.attention_interval.get_active_id()),
            "attention_ac_only": self.attention_ac_switch.get_active(),
        }

    def _compute_ops(self, ui: dict, base: dict) -> list:
        """The `applockerd` subcommands needed to turn `base` into `ui`."""
        ops = []
        if ui["face"] != base["face"]:
            ops.append(["set-face", "on" if ui["face"] else "off"])
        if ui["pin"] != base["pin"] or ui["sudo"] != base["sudo"]:
            val = ("both" if ui["pin"] and ui["sudo"]
                   else ("pin" if ui["pin"] else "sudo"))
            ops.append(["set-fallback", val])
        if ui["reauth_every"] != base["reauth_every"]:
            ops.append(["set-reauth", "always" if ui["reauth_every"] else "session"])
        if ui["attention"] != base["attention"]:
            ops.append(["set-attention", "on" if ui["attention"] else "off"])
        if ui["attention_interval"] != base["attention_interval"]:
            ops.append(["set-attention-interval", str(ui["attention_interval"])])
        if ui["attention_ac_only"] != base["attention_ac_only"]:
            ops.append(["set-attention-ac-only",
                        "on" if ui["attention_ac_only"] else "off"])
        return ops

    def _update_apply_state(self):
        dirty = bool(self._compute_ops(self._snapshot_ui(), self._persisted))
        self.apply_btn.set_sensitive(dirty)
        self.revert_btn.set_sensitive(dirty)
        self.dirty_label.set_text("Unsaved changes" if dirty else "")

    def _reset_controls(self):
        """Snap every control back to the persisted values, silently."""
        self._loading = True
        p = self._persisted
        self.face_switch.set_active(p["face"])
        self.pin_switch.set_active(p["pin"])
        self.sudo_switch.set_active(p["sudo"])
        self.reauth.set_active_id("always" if p["reauth_every"] else "session")
        self.attention_switch.set_active(p["attention"])
        self.attention_interval.set_active_id(str(p["attention_interval"]))
        self.attention_ac_switch.set_active(p["attention_ac_only"])
        self._loading = False

    def _apply(self, _btn=None):
        ui = self._snapshot_ui()
        ops = self._compute_ops(ui, self._persisted)
        if not ops:
            return
        started_attention = ui["attention"] and not self._persisted["attention"]
        if run_privileged_batch(ops):
            self._persisted = read_config()
            self._reset_controls()  # resync to what actually saved
            self._update_apply_state()
            self._toast("Changes applied.")
            if started_attention:
                # Launch the watcher now; the autostart entry covers later logins.
                spawn([sys.executable, script_path("watch_presence.py")])
        else:
            # Cancelled or failed → the UI must not keep showing the change.
            self._revert()
            self._toast("Changes not applied.")

    def _revert(self, _btn=None):
        self._reset_controls()
        self._update_apply_state()

    # -- handlers ------------------------------------------------------------

    def _on_policy_changed(self, *_args):
        if self._loading:
            return
        self._update_apply_state()

    def _on_fallback_toggled(self, switch, _param, which):
        if self._loading:
            return
        if not self.pin_switch.get_active() and not self.sudo_switch.get_active():
            # Enforce the invariant: bounce the just-turned-off switch back on.
            self._loading = True
            switch.set_active(True)
            self._loading = False
            self._toast("At least one alternative must stay enabled.")
            return
        self._update_apply_state()

    def _on_add_app(self, _btn):
        AppPicker(self, self._add_app)

    def _add_app(self, key: str):
        if run_privileged(["lock-app", key]):
            self._refresh_apps()

    def _remove_app(self, key: str):
        if run_privileged(["unlock-app", key]):
            self._refresh_apps()

    def _toast(self, text: str):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=text)
        dlg.run()
        dlg.destroy()


class AppPicker(Gtk.Dialog):
    """A searchable list of installed apps to add to the locked list."""

    def __init__(self, parent, on_pick):
        super().__init__(title="Add a locked app", transient_for=parent, modal=True)
        self.on_pick = on_pick
        self.set_default_size(420, 480)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)

        self.search = Gtk.SearchEntry()
        self.search.connect("search-changed", lambda _e: self.filter.refilter())
        box.pack_start(self.search, False, False, 0)

        self.store = Gtk.ListStore(str, str, str)  # name, key, kind
        for kind, key, name in list_installed():
            self.store.append([name, key, kind])
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self._match)

        view = Gtk.TreeView(model=self.filter)
        view.append_column(Gtk.TreeViewColumn("App", Gtk.CellRendererText(), text=0))
        view.append_column(Gtk.TreeViewColumn("Kind", Gtk.CellRendererText(), text=2))
        view.connect("row-activated", self._on_activate)
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)

        self.show_all()

    def _match(self, model, it, _data):
        q = self.search.get_text().lower()
        return not q or q in (model[it][0] or "").lower()

    def _on_activate(self, _view, path, _col):
        it = self.filter.get_iter(path)
        key = self.filter[it][1]
        self.on_pick(key)
        self.destroy()


def main():
    # --no-auth skips the open-time authentication gate. DEV ONLY — for iterating
    # on the layout without re-authing each launch. Production always gates.
    dev_no_auth = "--no-auth" in sys.argv
    if not dev_no_auth and not authorize():
        sys.stderr.write("AppLocker: authentication required to open settings.\n")
        return 1
    win = SettingsWindow()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
