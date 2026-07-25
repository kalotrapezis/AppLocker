# Changelog

Newest first. `0.0.1-<letter>` entries were testing rounds leading up to the first
real release, **0.0.1**.

## 0.0.2-1 — the presence watcher stops eating all your RAM

### Fixed
- **The presence watcher no longer holds the face engine resident.** It built the
  engine once at startup and kept it for the process's entire life — and that
  process is a per-user autostart that runs all day. The engine owns OpenCV's
  YuNet/SFace models and their native buffers, so the watcher sat on that memory
  around the clock while doing camera work for a couple of seconds every
  interval. On a machine with little headroom it starved everything else.
  The engine is now built lazily inside each snapshot and dropped when the
  snapshot ends. Same detection, and building it costs a few hundred
  milliseconds against a default 10-minute cadence.
- Related: a snapshot where the face engine can't be built or throws now logs and
  reports "blind" (which never locks) instead of taking the watcher down.

## 0.0.2 — settings redesign + presence that actually works on Wayland

A big pass over the Settings window and the two features that were unreliable:
presence locking and hide-in-place.

### Changed
- **Redesigned Settings window** to match the design mockup: a left navigation
  sidebar (rounded items, separators) with four pages — Security & unlock, Locked
  apps, Private files, Presence & display — laid out as rounded cards. The long
  lists (enrolled faces, locked apps, hidden items, PIN, brightness) moved behind
  per-card **⚙ gear dialogs**, with live "N enrolled / locked / hidden" summaries.
- **Presence is now purely time-based.** It used X11 idle detection (ScreenSaver /
  `xprintidle`), which doesn't exist on Wayland, so on KDE/Wayland it effectively
  never ran. It now takes a snapshot every interval regardless of keyboard/mouse,
  and locks after the configured number of empty snapshots.

### Added
- **"Lock apps with your face" master switch** — a real `apps_enabled` policy flag
  (daemon `set-apps-enabled`); when off, the gate ignores the locked-app list while
  keeping it, so the feature toggles without losing your choices.
- **Adaptive brightness pauses while a game is running** — a new toggle (on by
  default) leaves the screen alone while a Steam game (`SteamLaunch`) or `gamescope`
  is running, so auto-brightness doesn't fight the game.
- **Black-frame notification** — when the camera can only see black (shutter closed
  or a dark room) the watcher pops a desktop notification and logs it, instead of
  silently treating it as "present".
- Testing flag `watch_presence.py --no-lock` (logs "would lock" instead of locking).

### Fixed
- **Hidden files now re-hide on a predictable timer.** The re-hide deadline was
  reset every time the folder was re-listed (by the file manager, Baloo, …), so it
  often never fired. It's now fixed at **1 minute from reveal** (and still hides
  instantly when the screen locks).

## 0.0.1-1 — hidden files actually re-hide

Fixes the hide-in-place watcher so revealed items get hidden again — before this,
a face-revealed folder stayed visible forever.

### Fixed
- **Re-hide on screen lock never fired**: `session_locked()` queried `loginctl`
  with an empty session id, so it always reported "unlocked". The watcher now
  resolves the real session id at startup (`XDG_SESSION_ID`, with a
  `loginctl list-sessions` fallback) and re-hides instantly on lock.
- With the lock re-hide working, the inactivity timeout also does its job —
  revealed items re-hide after a while on their own.

## 0.0.1 — first working release 🎉

The first version that works end-to-end on Kubuntu/KDE/Wayland. It's a fun personal
convenience tool, not a security product (see the README).

Working:
- **App locking** — gate deb / Flatpak / AppImage / system apps behind face / PIN /
  sudo. Enforcement is off until you turn it on: a **Start/Stop** button (this
  session) plus a new **"Start enforcement at boot"** toggle to persist it. The gate
  is exec-only, so it can't freeze on file opens.
- **Encrypted Private folder** (gocryptfs) and **hide-in-place** files/folders with
  inotify face-reveal.
- **Face unlock** (YuNet + SFace, liveness) with mandatory PIN/sudo fallback; PIN
  works everywhere via the root **broker**. Opt-in **lock-screen** and **terminal
  sudo** face tiers.
- **Lock when I leave** presence watcher + **auto screen-brightness** (silent, no KDE
  OSD).

### Added
- Second service-bar toggle **"Start enforcement at boot"** (`systemctl enable/disable
  --now`, via the broker) — the Start/Stop button only affects the running state
  (kept separate as a safety choice while the gate was being proven out).
- `APPLOCKER_VERSION` override in the build script for cutting real versions.

## 0.0.1-al — silent brightness (no OSD), 10-min brightness cadence, release build

### Changed
- Auto-brightness now uses KDE PowerDevil's **`setBrightnessSilent`** — our nudges no
  longer pop the on-screen brightness OSD (your own brightness keys still do). No more
  slider flashing mid-game.
- Brightness peek cadence **30 min → 10 min** (`--brightness-interval`), now that each
  peek is a silent, deadbanded no-op unless the room actually changed.
- Built as a **release** package (no dev-mode marker) so the gate service isn't stuck
  in dev mode. Enforcement is still OFF until you `systemctl enable --now
  applockerd.service`; the gate is exec-only (can't freeze on file opens).

## 0.0.1-aj — presence watcher: stop crashing (so it locks), quieter brightness

### Fixed
- **The watcher crashed when you walked away, so it never locked.** Its dim-overlay
  "draw" handler is called with a `cairo.Context`, which needs the pycairo↔gi
  foreign marshaller (`python3-gi-cairo`); without it, GTK raises `TypeError:
  Couldn't find foreign struct converter for 'cairo.Context'` and the process dies
  before reaching the lock step. Now we detect it (`gi.require_foreign('cairo')`) and
  skip the (cosmetic) dim if it's missing — **locking still happens**. Added
  `python3-gi-cairo` / `python3-cairo` to the package deps so the dim works too.
- **Auto-brightness kept popping KDE's brightness OSD** (even mid-game). It re-applied
  the same level every snapshot, and camera luma jitters a few % — each set re-shows
  the OSD. Added a ±5% deadband: it only changes brightness (and pops the OSD) on a
  real change.

### Still true on Wayland
- Idle can't be measured (no XScreenSaver under XWayland, no evdev access), so
  presence is a periodic check on the interval — it can wake the camera while you're
  actively there. If that bugs you: raise "Check every…" in Settings, or turn
  auto-brightness off. Real idle-gating would need a Wayland idle source we don't
  have cheaply.

## 0.0.1-ag — presence watcher actually runs on KDE

### Fixed
- **The presence watcher never started on KDE**, so "lock when I leave" / the camera
  checks never happened. Cause: its autostart entry had
  `OnlyShowIn=X-Cinnamon;GNOME;MATE;XFCE;Unity;` — which **excludes KDE**. Removed it
  so it autostarts on login (it no-ops if the feature is off). It had only been
  running for the session where you toggled the feature on.
- **Idle detection lied on Wayland.** `IdleMonitor` reported `available=True` even
  though XWayland has no MIT-SCREEN-SAVER extension, so idle time was always
  unknown. It now checks `XScreenSaverQueryExtension` and honestly falls back to the
  periodic-snapshot mode (and stops the `Xlib: extension missing` spam).

### Known limitation
- True idle-gating ("camera off while you actively work") needs a Wayland idle
  source we don't have cheaply (no `input`-group / evdev access here). On Wayland it
  currently does a periodic check on the interval instead. Fine for "did I walk
  away?"; the camera just also blinks on that interval while you're present.

## 0.0.1-af — drop the (impossible) graphical-polkit PAM tier

- Removed the `uisudo` PAM tier. Finding: the graphical password prompt (polkit)
  runs its PAM helper in a hardened systemd sandbox — `PrivateDevices=yes` (no
  camera) and `ProtectHome=yes` (no access to your faces) — so a PAM face module
  *cannot* work there, by polkit's design. The "Unlock these with your face" section
  now shows only the two tiers that actually work: **Lock screen** and **Terminal
  sudo**. Graphical prompts will be handled by AppLocker's polkit *agent*
  (`gui/polkit_agent.py`, runs in-session with camera access) — wired separately.
- If you enabled the graphical tier on an earlier build, clean it up with:
  `sudo applocker-pam disable uisudo` (before upgrading).

## 0.0.1-ae — grouped "Unlock these with your face" section

### Added
- New Settings section **"Unlock these with your face"** grouping the PAM tiers as
  toggles: **Lock screen**, **Terminal sudo password prompts**, and **Graphical
  (app) password prompts** (polkit). All `auth sufficient` — the password always
  still works. The `uisudo`/polkit tier is new (materialises `/etc/pam.d/polkit-1`
  from its vendor default when absent). Broker op generalised to `pam <tier> on|off`.

### Notes
- Kept the head-turn **liveness** on the graphical tiers (security). There's no
  on-screen "turn your head" guide there yet — it shows in a terminal but not on the
  lock screen/polkit dialog. On-greeter feedback via PAM messages is a planned next
  step; for now: look at the camera and turn left, then right.

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
