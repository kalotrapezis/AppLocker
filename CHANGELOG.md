# Changelog

Newest first. `0.0.1-<letter>` entries were testing rounds leading up to the first
real release, **0.0.1**.

## 0.0.10 — you can finally see that face unlock worked

### Fixed
- **The "Welcome back" ✓ was never actually shown.** This is the real cause of
  the complaint that started this whole run — "Viber opens even when
  authentication fails". `Feedback::ok()` wrote `ok` to the feedback window and
  then immediately called `reap()`, which drops the child's stdin; that EOF made
  the window quit at once, racing its own 700ms timer. So a successful face
  unlock showed nothing at all, the app just opened, and that is
  indistinguishable from "the lock did nothing".
  The daemon now waits for the window to close itself, and the window ignores an
  EOF that arrives behind an `ok` (belt and braces: either side alone fixes it).
  Measured: the window lived 0.18s before — pure GTK startup, nothing drawn —
  and 1.38s after.
- The ✓ now stays up **1.2s** instead of 700ms. A 700ms flash at the end of a
  1–2s scan was easy to miss, which was half the problem. A successful face
  unlock therefore costs ~1.4s more; that delay *is* the feature.

### Still silent (follow-ups, noted in plan.md)
- The fingerprint tier shows nothing at all — it runs before any window exists.
- The broker's own face path (Settings, sudo-password release) never spawns the
  feedback window; only the gate and `auth-test` do.

## 0.0.9 — an upgrade now takes effect without a reboot

### Fixed
- **`postinst` restarted the broker but not the gate.** With enforcement on, an
  upgrade installed the new binary while the *old* one kept running — so a fix
  looked like it had done nothing until the next reboot. Both units are
  `try-restart`ed now, which is a no-op when the gate is off (the common case),
  so upgrading still never turns enforcement on by itself.

## 0.0.8 — a broken reader must not cost you a password attempt

### Fixed
- **A failed fingerprint consumed one of the three password attempts.** Attempts
  are a brute-force budget and a finger is not a guessable secret — and on flaky
  reader hardware it fails for reasons that have nothing to do with the person.
  Only typed answers (PIN, password) spend a try now. Found on the first real
  unlock with the gate on: the reader errored and the dialog came back with
  "Incorrect — try again" having already eaten a try.
- Reader PAM timeout 10s → 6s, so a broken reader cannot stall an unlock.
  `APPLOCKER_FP=0` still turns the tier off entirely.

### Corrected — the Viber diagnosis was wrong, twice
The journal settled it. Every Viber launch under the gate (7 of them, Nov 20–23)
ended in `allow (auth passed)` within 1–3 seconds. No `DENY`, no
`ALLOW (fail-open)`, no second instance anywhere in the log. So:
- it was **not** the 30s fail-open watchdog (0.0.3) — that never fired here;
- it was **not** an AppImage second instance (0.0.5) — the gate matched
  `viber.appimage` correctly on every launch.
What actually happened: **face recognition kept passing**, silently, in about
two seconds, so Viber opened with no dialog — which reads as "it opened even
though auth failed". Both earlier fixes close real holes and both stay, but
neither was the cause of this symptom. With the gate on, a cancelled prompt now
gives `DENY (auth failed)`, no Viber process and no FUSE mount (verified
07:20:07).

Also worth knowing, since it shapes what to trust: liveness passed in ~2s on
every one of those launches — ordinary head movement satisfies the turn
challenge. It is a spoof speed bump, not a real liveness test.

### Known hardware problem (not fixable here)
This machine's reader (Chipsailing CS9711) fails inside libfprint:
`device was disconnected`, `expected 8 bytes but got 8000`, and once
`Driver reported a match providing a scanned print that is not matching any in
the gallery` — libfprint rejecting a match the *driver* claimed. That is also
why `sudo` times out at the reader. Nothing in AppLocker can fix it.

## 0.0.7 — a PAM service of our own for typed passwords

### Fixed
- **Checking a typed password went through the distro `sudo` stack**, which on
  Ubuntu begins with `pam_fprintd` (`timeout=15`): every password check sat at
  the reader for ~15s before looking at what was typed, and a finger touch could
  satisfy a call whose whole question was "is this password correct?". New
  `/etc/pam.d/applocker-password` (pam_unix only) is used when present, with the
  old behaviour as the fallback if you delete it.

## 0.0.6 — refuse a redundant fingerprint tier

### Fixed
- `applocker-pam enable fingerprint` now refuses when `pam_fprintd` is already
  in `/etc/pam.d/common-auth` (Ubuntu's `pam-auth-update` puts it there, and
  `/etc/pam.d/kde` includes it). Enabling it anyway would have stacked a second
  reader wait in front of the greeter's existing one.
- The installer's text still advertised the old 30s fail-open timeout.

## 0.0.5 — fingerprint first, a trust window, and the camera stops idling

### Security
- **A locked AppImage's inner binary is now gated too.** An AppImage runs its
  real program out of its own FUSE mount (`/tmp/.mount_viberXXXX/AppRun`) — a
  path with nothing in common with the `.appimage` file we lock, so an exec in
  there used to sail straight past. The mount table is the missing link:
  squashfuse records the image's name as the mount source, so the gate now
  resolves any exec inside a `fuse.*` mount back to its source image and matches
  that against the locked list (`LockList::matches_appimage_source`, longest
  mount point wins, path-boundary checked). This closes "it starts another
  instance": a stale mount left by an earlier allowed launch, an
  `--appimage-extract-and-run` leftover, or anything else exec'ing inside an
  already-mounted image. Found a stale `viber.appimage` mount still sitting in
  `/tmp` on the dev box while investigating.

### Changed
- **Fingerprint is the default factor when a finger is enrolled**: reader →
  face → PIN → password, in the gate *and* in the broker's sudo-password
  release (which previously went straight to face). The unlock dialog now leads
  with "Touch the fingerprint reader" instead of mentioning it last.
- **The camera no longer runs the whole time the screen is locked.** Two bounds:
  it only looks while the lock screen's display is actually lit (DRM `dpms` from
  sysfs — free, no camera, and it blanks seconds after you leave and wakes the
  instant anyone touches the machine), and after 6 misses with the screen
  continuously on it drops to one look every 30s until the screen blanks and
  wakes again. Walking up to a dark lock screen costs zero camera time; the old
  behaviour polled every 5s for 3 minutes then every 30s forever.
- **Trust window (your idea, and a sound one):** within 20 minutes of an
  accepted fingerprint, or while a phone listed in
  `~/.config/applocker/trusted-bt` is *connected* over Bluetooth, the lock
  screen accepts a plain glance and skips the turn challenge. Outside that, the
  challenge stands. The daemon stamps `/run/applocker/last-fingerprint` (tmpfs,
  so a reboot drops the trust; mtime only, no identity in it) on every accepted
  touch. The log line says which grant was used, so it is never a mystery why it
  asked for a turn.
  Caveats, stated rather than hidden: Bluetooth presence is a *weak* signal —
  "connected" is not "the owner is at the keyboard", and it is nothing like
  Apple's challenge-response to a worn Watch. That is why it is opt-in per MAC
  and off until you write that file. And a fingerprint accepted by the *greeter*
  (the opt-in `pam_fprintd` tier) does not stamp anything, because that path
  never touches our code — only app/sudo unlocks open the window.

## 0.0.4 — no button: the lock screen lets you straight in

### Added
- **Face unlock with no confirmation click.** The PAM tier could only ever run
  *after* the KDE greeter started a conversation — i.e. after you clicked Unlock
  or typed something. That click was the "confirmation" nobody wanted, and it is
  not something a PAM module can remove (the greeter owns it). So the presence
  watcher now does the work itself: while the session is locked it runs the same
  identity check and, on a match, asks **logind** to unlock the session, which
  the Plasma locker obeys. No PAM involved on this path, so it cannot lock
  anyone out — if it fails, the ordinary greeter is still sitting there.
  Verified live on Plasma 6.6/Wayland: locked → face + head-turn → in.
  - Enabled by the existing Settings toggle (Unlock these with your face → Lock
    screen), **or** by `touch ~/.config/applocker/lockscreen-unlock`, which
    needs no root and no PAM edit at all.
  - Liveness is kept (your earlier call: a photo must not open the machine), so
    it is a glance *plus* a head-turn. `--unlock-no-liveness` trades that away.
  - Cadence: every 5s for the first 3 minutes after the lock, then every 30s, so
    coming back an hour later still works without the camera running flat out.
    Skipped entirely while the session is not the active one (VT/user switch),
    and while on battery if "pause on battery" is on.
  - The watcher no longer exits when presence/attention is off — with the
    unlock job enabled it stays resident for that alone.
- **The fingerprint reader is tried before the prompt**, so there is no
  Fingerprint button to click either: launch a locked app, touch the reader,
  you're in. Ignore it and it falls through to face, then the dialog (where the
  button still exists as the retry). `APPLOCKER_FP=0` turns the tier off.
- **`applocker-pam fingerprint`** — an opt-in tier that puts `pam_fprintd` into
  KDE's own PAM stack, so the *greeter itself* offers "touch the sensor" with no
  click (Plasma ≥ 6 starts that conversation as soon as the lock screen
  appears). Sandbox-tested alongside the face tier: both can live in
  `/etc/pam.d/kde` and each disables independently. **Not enabled by anything** —
  it edits a login PAM stack, so you run it yourself with a root shell open:
  `sudo applocker-pam enable fingerprint`.

### Changed
- The gate's fail-open watchdog default went 30s → 60s. The bounded pre-prompt
  tiers can legitimately take ~35s now (reader ~10s + three face runs ~25s), and
  the watchdog must only fire for work that has actually hung; once a dialog is
  up, the 5-minute prompt grace from 0.0.3 takes over.

## 0.0.3 — the fail-open hole, bystanders, fingerprint unlock, and a clean uninstall

### Security
- **A locked app opened anyway if auth took longer than 30 seconds.** The gate's
  fail-open watchdog (`$APPLOCKER_GATE_TIMEOUT`, default 30s) exists so a hung
  recognizer can never freeze a launch — but its clock covered the *whole* auth
  routine. Three `recognize.py` runs (fresh python + OpenCV + ONNX load + 5s
  camera budget each, 1s apart) burn 18–25s before the PIN dialog even appears,
  so a wrong PIN or a few seconds of typing fell off the end and the gate
  ALLOWed. Reported as "Viber opens even when authentication fails".
  The watchdog now stops at the moment a dialog goes on screen (a human is
  answering, so the hung-process deadline no longer applies) and switches to a
  5-minute grace, which still can't block a launch forever. If the auth thread
  dies without answering, that is a DENY, not an allow.
- **The unlock cache survived a Plasma screen lock.** The listener only matched
  logind's `Session.Lock` and `PrepareForSleep`; KDE's locker sets `LockedHint`
  instead, which arrives as a PropertiesChanged signal. Unlocks therefore lasted
  until the daemon restarted — lock your screen, walk away, and your unlocked
  apps were still open to anyone. The listener now also matches `LockedHint`.

### Fixed
- **Your PC would not unlock with someone standing next to you.** The YuNet
  detector picked the *highest-confidence* box in the frame, not the nearest
  one, so a bystander's cleaner face won and the owner was never embedded →
  `nomatch`. It now takes the largest box (= closest person) above a 0.7 score
  floor. Matching every visible face would also have "fixed" it, by letting
  anyone beside you unlock your machine.
- **Uninstalling actually lets go now.** `prerm` used to disable two services
  and `postrm` merely printed a warning, so removal left behind: the
  `pam_applocker.so` line in `/etc/pam.d/kde` (hence an AppLocker unlock button
  still on the lock screen, pointing at a module dpkg had just deleted), the
  `.hidden` markers (your folders stayed invisible with no tool left to reveal
  them), mounted gocryptfs vaults, and the per-user helpers still running until
  logout. `prerm` now runs `applocker-pam disable all`, kills
  `/usr/lib/applocker/*` helpers, runs `hidelist.py reveal-all` as each owner,
  and unmounts their vaults. Per-user data in `~/.config/applocker` is still
  *not* deleted — it holds face embeddings and encrypted vaults — but `postrm`
  now says exactly what is there and what deleting it costs.

### Added
- **Fingerprint unlock.** When `libpam-fprintd` is installed and
  `fprintd-list $USER` shows an enrolled finger, the unlock prompt offers a
  **Fingerprint** button beside PIN/Password; the daemon verifies it through a
  dedicated `/etc/pam.d/applocker-fprintd` stack (`pam_fprintd` alone, 20s
  timeout) so a touch can never be answered by a password and a broken fprintd
  can't weaken `sudo`. It also unlocks the stored sudo password in the broker,
  like face and PIN do. Faster than face, and it works with people around.
  Known gap: the dialog closes before the reader starts waiting, so there is no
  on-screen "touch now" hint yet, and there is no Settings toggle — it appears
  whenever a finger is enrolled.

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
