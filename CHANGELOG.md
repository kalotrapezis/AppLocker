# Changelog

A personal, alpha-stage project — versions are `0.0.1-<letter>` build rounds, not
stable releases. Newest first.

## 0.0.1-ad — lock-screen face unlock (opt-in)

### Added
- **Face unlock for the KDE lock screen**, behind a new Settings toggle ("Also
  unlock the screen lock with my face"). Wired as `auth sufficient` into
  `/etc/pam.d/kde` (created from the vendor default when absent), so **your password
  always still works — it can never lock you out**; turning it off removes the line
  cleanly. The unlock face check runs with liveness and at top camera priority
  (`priority=lockscreen`, preempting the desktop helpers). Root edits go through the
  broker (PIN works), with a `pkexec` fallback.

> ⚠️ This one edits real PAM. It's `sufficient` (password fallback intact), but test
> it once with a spare TTY handy (Ctrl-Alt-F2) before relying on it.

## 0.0.1-ac — alpha snapshot (backup before the lock-screen work)

The "last safe build" before starting face-unlock for the KDE screen lock. Snapshot
of everything that works today, all userspace unless noted.

### Added
- **Hidden files & folders** — a new hide-in-place tier (`hide/hidelist.py`) that
  edits a directory's `.hidden` list; nothing is moved or encrypted. Settings gets
  a "Hidden files & folders" section (file/folder picker, like the locked-apps
  list) with a clear warning that it's *not* as secure as the Private folder.
- **Auto-reveal watcher** (`hide/hide_watch.py`) — inotify watches the folders of
  hidden items; opening one triggers a **silent face check** and reveals it, then
  re-hides on screen-lock or after a short inactivity timeout. Watch-only, so it
  can never freeze a file operation.
- **Camera priority queue** (`face/cameralock.py`) — an advisory lock + priority
  ladder (lockscreen ▸ app ▸ file-reveal ▸ presence) so the helpers share the one
  webcam instead of colliding; `recognize.py` self-registers by priority.
- **Auto screen-brightness** — the presence watcher nudges brightness by
  time-of-day + room light, including a periodic peek (~30 min) even while active.
- **Encrypted sudo autocomplete** — optional, root-side encrypted sudo password,
  face/PIN gated, with a lockout schedule; a polkit agent to use it.

### Changed
- **PIN works everywhere** — a root auth **broker** (`applockerd serve`) now runs
  the face→PIN→sudo routine over a Unix socket, replacing `pkexec`/polkit for
  Settings changes and prompts. One auth per Settings window; sensitive changes
  re-auth.
- Release `.deb` guides gate-enable via `systemctl` (never the old `applocker on`
  PAM path), and clears any stale dev-mode marker.
- README reframed: this is a **fun personal convenience project, not a security
  tool.**

### Fixed
- Enrollment **camera preview** rewritten to a `Gtk.Image` (was a blank
  DrawingArea on KDE/Wayland).
- Private-folder Settings section no longer overlaps the next section on
  delete/recreate (bounded the wrapped label's height-for-width).
- App gate: Flatpaks matched by launcher cmdline; Wayland session discovery; the
  broker no longer self-kills on SIGHUP.

### Known gaps
- Face unlock for the **screen lock** (PAM) is not wired yet — the next step, behind
  a Settings toggle.
- Hidden-item re-hide leans on a timeout + screen-lock (file managers don't expose
  "which folder is open"), so it can lag behind you leaving by up to a minute.
