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

import os
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


def read_locked_folders() -> list:
    path = cfg_path("APPLOCKER_LOCKED_FOLDERS", "/etc/applocker/locked-folders")
    return _read_tsv(path, fields=2)


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


def spawn(args: list) -> None:
    try:
        subprocess.Popen(args)
    except (OSError, subprocess.SubprocessError):
        pass


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

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add(scroller)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                           border_width=18)
        scroller.add(self.box)

        self._build_face_section()
        self._build_apps_section()
        self._build_folders_section()
        self._build_policy_section()

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
        self.face_switch.connect("notify::active", self._on_face_toggled)
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

    def _build_folders_section(self):
        box = self._section("Locked files and folders")
        self.folders_list = Gtk.ListBox()
        self.folders_list.set_selection_mode(Gtk.SelectionMode.NONE)
        box.pack_start(self.folders_list, False, False, 0)

        add = Gtk.Button(label="Add folder…")
        add.connect("clicked", self._on_add_folder)
        add.set_halign(Gtk.Align.START)
        box.pack_start(add, False, False, 0)
        self._refresh_folders()

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
        self.reauth.connect("changed", self._on_reauth_changed)
        rrow.pack_start(self.reauth, False, False, 0)
        box.pack_start(rrow, False, False, 0)

        arow, self.attention_switch = self._switch_row(
            "Lock when I leave (presence watcher)", cfg["attention"])
        self.attention_switch.connect("notify::active", self._on_attention_toggled)
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
        self.attention_interval.connect("changed", self._on_attention_interval_changed)
        irow.pack_start(self.attention_interval, False, False, 0)
        box.pack_start(irow, False, False, 0)

        acrow, self.attention_ac_switch = self._switch_row(
            "Only when plugged in (pause on battery)", cfg["attention_ac_only"])
        self.attention_ac_switch.connect("notify::active", self._on_attention_ac_toggled)
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

    def _refresh_folders(self):
        self._clear(self.folders_list)
        rows = read_locked_folders()
        if not rows:
            self.folders_list.add(self._info_row("No folders locked."))
        for path, name in rows:
            self.folders_list.add(self._list_row(
                f"{name}  —  {path}", lambda _b, p=path: self._remove_folder(p)))
        self.folders_list.show_all()

    def _info_row(self, text: str):
        row = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class("dim-label")
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(4)
        row.add(lbl)
        return row

    # -- handlers ------------------------------------------------------------

    def _on_face_toggled(self, switch, _param):
        run_privileged(["set-face", "on" if switch.get_active() else "off"])

    def _on_attention_toggled(self, switch, _param):
        on = switch.get_active()
        run_privileged(["set-attention", "on" if on else "off"])
        if on:
            # Start the watcher in this session right away; on later logins the
            # autostart entry (packaging step) will do it.
            spawn([sys.executable, script_path("watch_presence.py")])

    def _on_attention_interval_changed(self, combo):
        run_privileged(["set-attention-interval", combo.get_active_id()])

    def _on_attention_ac_toggled(self, switch, _param):
        run_privileged(["set-attention-ac-only",
                        "on" if switch.get_active() else "off"])

    def _on_fallback_toggled(self, switch, _param, which):
        pin = self.pin_switch.get_active()
        sudo = self.sudo_switch.get_active()
        if not pin and not sudo:
            # Enforce the invariant: bounce the just-turned-off switch back on.
            switch.set_active(True)
            self._toast("At least one alternative must stay enabled.")
            return
        value = "both" if pin and sudo else ("pin" if pin else "sudo")
        run_privileged(["set-fallback", value])

    def _on_reauth_changed(self, combo):
        run_privileged(["set-reauth", combo.get_active_id()])

    def _on_add_app(self, _btn):
        AppPicker(self, self._add_app)

    def _add_app(self, key: str):
        if run_privileged(["lock-app", key]):
            self._refresh_apps()

    def _remove_app(self, key: str):
        if run_privileged(["unlock-app", key]):
            self._refresh_apps()

    def _on_add_folder(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="Choose a folder to lock", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Lock", Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            if path and run_privileged(["lock-folder", path]):
                self._refresh_folders()
        dlg.destroy()

    def _remove_folder(self, path: str):
        if run_privileged(["unlock-folder", path]):
            self._refresh_folders()

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
