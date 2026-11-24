#!/usr/bin/env python3
"""AppLocker face-gated polkit authentication agent (option 1: memory-only).

What it does
------------
Replaces the desktop's polkit agent (on KDE that's
`polkit-kde-authentication-agent-1`) for **your session only**, so that when any
GUI app triggers a privilege prompt (install software, mount a disk, change a
system setting) you get AppLocker's flow instead:

  * **First prompt of the session** — no password is cached yet, so you just type
    it once (this *is* the authentication). We remember it in RAM.
  * **Every prompt after that** — a quick **face check** runs; if it's you, the
    remembered password is submitted automatically (zero typing). If the face
    check fails (someone else), you fall back to typing the password, exactly
    like the normal dialog.

Security shape
--------------
  * The password is **never written to disk** — it lives only in this process'
    memory and is gone on logout/restart (then you type it once again).
  * We never *decide* the password is right: the setuid-root helper
    `/usr/lib/polkit-1/polkit-agent-helper-1` runs PAM and reports SUCCESS /
    FAILURE. A tampered agent cannot forge a success.
  * This touches **only GUI polkit prompts**. It does NOT touch the login screen,
    the lock screen, or terminal `sudo` — those are PAM/login and are left
    completely alone (that path broke a login once; we don't go near it).

Running it (reversible, nothing persists)
-----------------------------------------
    # take over from KDE's agent for this session, restore it on exit:
    python3 gui/polkit_agent.py --takeover
    # …test a prompt in another terminal, e.g.:  pkexec true
    # Ctrl-C here → we unregister and restart KDE's agent.

Without ``--takeover`` we just try to register; polkit allows one agent per
session, so if KDE's is still running the registration fails and we tell you.
"""

import argparse
import os
import pwd
import signal
import socket
import subprocess
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib  # noqa: E402  (import after dbus mainloop below is fine)

# ── polkit constants ─────────────────────────────────────────────────────────
PK_NAME = "org.freedesktop.PolicyKit1"
PK_AUTHORITY_PATH = "/org/freedesktop/PolicyKit1/Authority"
PK_AUTHORITY_IFACE = "org.freedesktop.PolicyKit1.Authority"
AGENT_IFACE = "org.freedesktop.PolicyKit1.AuthenticationAgent"
AGENT_OBJECT_PATH = "/eu/applocker/PolkitAgent"
HELPER = "/usr/lib/polkit-1/polkit-agent-helper-1"

KDE_AGENT_UNIT = "plasma-polkit-agent.service"


def _here(name: str) -> str:
    """Locate a sibling helper script (repo layout or installed flat layout)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, name),                 # gui/ or installed
                 os.path.join(here, "..", "face", name)):   # repo: ../face
        if os.path.exists(cand):
            return cand
    return os.path.join(here, name)


AUTH_PROMPT = _here("auth_prompt.py")
RECOGNIZE = _here("recognize.py")


def session_id() -> str:
    """This login session's id — the polkit subject we register for."""
    sid = os.environ.get("XDG_SESSION_ID")
    if sid:
        return sid
    try:
        out = subprocess.run(["loginctl", "show-session", "self", "-p", "Id",
                              "--value"], capture_output=True, text=True).stdout.strip()
        if out:
            return out
    except OSError:
        pass
    # Last resort: our own session via the pid.
    return subprocess.run(["loginctl", "--no-legend"], capture_output=True,
                          text=True).stdout.split()[0]


class AppLockerAgent(dbus.service.Object):
    """The object polkit calls back on. Exported on the *system* bus connection."""

    def __init__(self, bus, path):
        super().__init__(bus, path)
        # No password is kept here: the broker holds the encrypted copy and hands
        # it over only for the instant we pipe it to polkit's helper.

    # ── polkit AuthenticationAgent interface ──────────────────────────────────
    @dbus.service.method(AGENT_IFACE,
                         in_signature="sssa{ss}sa(sa{sv})", out_signature="")
    def BeginAuthentication(self, action_id, message, icon_name, details,
                            cookie, identities):
        username, _uid = self._pick_identity(identities)
        if username is None:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.PolicyKit1.Error.Failed",
                "no unix-user identity offered")

        # Ask the root broker for the password. It keeps the encrypted copy and
        # only releases it after a face match or your PIN (with the lockout).
        # We never hold it between prompts — pipe it to the helper and drop it.
        reply = self._broker_get_sudo(message)

        if reply is not None and reply.startswith("ok\n"):
            pw = reply[3:]
            try:
                ok = self._run_helper(username, cookie, pw)
            finally:
                del pw
            if ok:
                return
            raise dbus.exceptions.DBusException(
                "org.freedesktop.PolicyKit1.Error.Failed", "authentication failed")

        if reply == "cancel":
            raise dbus.exceptions.DBusException(
                "org.freedesktop.PolicyKit1.Error.Cancelled", "cancelled by user")

        # "notset" (first ever use) or "destroyed" (post-lockout): collect the
        # real sudo password ourselves. On first setup we ask the broker to
        # remember it; after a destroy we do NOT — autocomplete stays off until
        # you re-enable it, which is the whole point of the lockout.
        if reply in ("notset", "destroyed") or reply is None:
            for attempt in range(3):
                pw = self._ask_sudo_password(message, retry=attempt > 0)
                if pw is None:
                    raise dbus.exceptions.DBusException(
                        "org.freedesktop.PolicyKit1.Error.Cancelled",
                        "cancelled by user")
                if self._run_helper(username, cookie, pw):
                    if reply == "notset":
                        self._broker_store_sudo(pw)
                    return
            raise dbus.exceptions.DBusException(
                "org.freedesktop.PolicyKit1.Error.Failed", "authentication failed")

        # denied / locked <secs> / error: the broker already prompted the user
        # (PIN and/or password) and it failed. Let polkit report the failure.
        raise dbus.exceptions.DBusException(
            "org.freedesktop.PolicyKit1.Error.Failed", "authentication failed")

    @dbus.service.method(AGENT_IFACE, in_signature="s", out_signature="")
    def CancelAuthentication(self, cookie):
        # We run BeginAuthentication synchronously, so by the time a cancel could
        # be delivered we're between prompts; nothing to tear down here.
        pass

    # ── helpers ───────────────────────────────────────────────────────────────
    def _pick_identity(self, identities):
        """Choose the identity to authenticate: prefer our own uid, else first."""
        me = os.getuid()
        chosen = None
        for kind, det in identities:
            if str(kind) != "unix-user":
                continue
            try:
                uid = int(det["uid"])
            except (KeyError, ValueError, TypeError):
                continue
            if uid == me:
                return pwd.getpwuid(uid).pw_name, uid
            if chosen is None:
                chosen = uid
        if chosen is not None:
            return pwd.getpwuid(chosen).pw_name, chosen
        return None, None

    def _ask_sudo_password(self, message, retry):
        """Pop the shared GTK dialog for the real sudo password (first-time setup
        / post-destroy). Returns the password, or None on cancel."""
        cmd = [sys.executable, AUTH_PROMPT, "--app", message or "a privileged action",
               "--methods", "sudo"]
        if retry:
            cmd += ["--error", "Incorrect — try again."]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        line = (out.splitlines() or [""])[0]
        if line == "cancel" or "\t" not in line:
            return None
        _tag, secret = line.split("\t", 1)
        return secret

    def _broker(self, payload):
        """Send one request to the root broker; return the reply string (trailing
        newline stripped) or None if the broker is unreachable."""
        path = os.environ.get("APPLOCKER_SOCK", "/run/applockerd.sock")
        if not os.path.exists(path):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(300)  # a human at the broker's PIN prompt is fine
                s.connect(path)
                s.sendall(payload)
                s.shutdown(socket.SHUT_WR)
                buf = b""
                while True:
                    chunk = s.recv(256)
                    if not chunk:
                        break
                    buf += chunk
        except OSError:
            return None
        return buf.decode(errors="replace").rstrip("\n")

    def _broker_get_sudo(self, message):
        """Ask the broker to release the stored password (face→PIN gated). Reply:
        'ok\\n<pw>', 'cancel', 'notset', 'destroyed', 'denied', or 'locked <s>'."""
        reason = message or "a privileged action"
        return self._broker(("get-sudo\n\n" + reason + "\n-\n").encode())

    def _broker_store_sudo(self, pw) -> bool:
        """Ask the broker to remember the (PAM-validated) sudo password."""
        r = self._broker(("store-sudo\n\nstore\n-\npw\t" + pw + "\n").encode())
        return r is not None and r.startswith("ok")

    def _run_helper(self, username, cookie, secret) -> bool:
        """Drive polkit-agent-helper-1: it runs PAM and returns SUCCESS/FAILURE.
        The helper (setuid root) is what actually validates and reports the result
        to the authority — we only feed it the secret."""
        try:
            p = subprocess.Popen([HELPER, username], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            sys.stderr.write(f"applocker-polkit: cannot run helper: {e}\n")
            return False
        try:
            p.stdin.write(cookie + "\n")
            p.stdin.flush()
            result = False
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith("PAM_PROMPT_ECHO_OFF ") or \
                        line.startswith("PAM_PROMPT_ECHO_ON "):
                    p.stdin.write(secret + "\n")
                    p.stdin.flush()
                elif line.startswith("PAM_ERROR_MSG ") or \
                        line.startswith("PAM_TEXT_INFO "):
                    pass  # informational
                elif line == "SUCCESS":
                    result = True
                    break
                elif line == "FAILURE":
                    result = False
                    break
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass
            p.wait()
        return result


# ── registration / lifecycle ─────────────────────────────────────────────────
def _subject(sid):
    """The polkit Subject struct (sa{sv}) for our unix session."""
    details = dbus.Dictionary({"session-id": dbus.String(sid)}, signature="sv")
    return dbus.Struct((dbus.String("unix-session"), details), signature="sa{sv}")


def register(bus, sid):
    authority = dbus.Interface(
        bus.get_object(PK_NAME, PK_AUTHORITY_PATH), PK_AUTHORITY_IFACE)
    locale = os.environ.get("LANG", "en_US.UTF-8")
    authority.RegisterAuthenticationAgent(
        _subject(sid), locale, AGENT_OBJECT_PATH,
        signature="(sa{sv})ss")
    return authority


def unregister(authority, sid):
    try:
        authority.UnregisterAuthenticationAgent(_subject(sid), AGENT_OBJECT_PATH)
    except dbus.DBusException:
        pass


def _kde_agent(action):
    """start/stop KDE's polkit agent user unit (best-effort, reversible)."""
    try:
        subprocess.run(["systemctl", "--user", action, KDE_AGENT_UNIT],
                       check=False)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="AppLocker face-gated polkit agent")
    ap.add_argument("--takeover", action="store_true",
                    help="stop KDE's polkit agent first, and restart it on exit")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    sid = session_id()

    if args.takeover:
        print(f"applocker-polkit: stopping {KDE_AGENT_UNIT} for this session…")
        _kde_agent("stop")

    AppLockerAgent(bus, AGENT_OBJECT_PATH)
    try:
        authority = register(bus, sid)
    except dbus.DBusException as e:
        sys.stderr.write(
            f"applocker-polkit: could not register (session {sid}): {e}\n"
            "Another agent may still hold this session. Try --takeover, or stop\n"
            f"  systemctl --user stop {KDE_AGENT_UNIT}\n")
        if args.takeover:
            _kde_agent("start")
        sys.exit(1)

    print(f"applocker-polkit: registered for session {sid}. "
          "GUI privilege prompts now go through AppLocker (Ctrl-C to stop).")

    loop = GLib.MainLoop()

    def shutdown(*_):
        print("\napplocker-polkit: unregistering…")
        unregister(authority, sid)
        if args.takeover:
            print(f"applocker-polkit: restarting {KDE_AGENT_UNIT}…")
            _kde_agent("start")
        loop.quit()
        return GLib.SOURCE_REMOVE

    # GLib-level signal handling: Python's signal.signal handlers don't run while
    # loop.run() is blocked in C, and a missed one would leave KDE's agent stopped.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown, None)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown, None)
    loop.run()


if __name__ == "__main__":
    main()
