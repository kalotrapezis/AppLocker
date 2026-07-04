# AppLocker — working plan

Single source of truth for outstanding work. Claude follows this **autonomously**:
pick the top unchecked task, do it, test it, tick it, move on. The user only
interrupts to say "stop" / "continue at <time>". No questions unless a task is
truly ambiguous.

Target env: **Kubuntu / KDE / Wayland**. Test build is dev-mode (gate can't freeze).
Version bumps land in `dist/applocker_0.0.1-<letter>_amd64.deb`.

## Architecture recap (already built)
- **Broker** (`applockerd serve`, `daemon/src/serve.rs`) — root Unix-socket daemon
  at `/run/applockerd.sock`. Runs the face→PIN→sudo routine and does privileged
  changes. Panic-isolated per request; `applockerd-broker.service` (self-healing).
- **Settings** (`gui/settings.py`) — talks to the broker; no pkexec.
- **Sudo autocomplete** (`gui/polkit_agent.py` + broker `get-sudo`/`store-sudo`) —
  encrypted sudo password root-side (`daemon/src/sudopass.rs`), face/PIN gated,
  2→24h→2→destroy lockout.
- **Gate** (`applockerd gate`, fanotify) — the enforcement that blocks launches.
  DEV-MODE DISABLED today (safety). **This is why locked apps still open freely.**

## Conventions
- After Rust edits: `cd daemon && cargo build --release && cargo test --release`.
- After Python edits: `python3 -m py_compile gui/<file>.py`.
- Rebuild deb: `packaging/build-deb.sh` (prints the new path).
- Never enable the real system-wide gate on the host; only in a VM / test-scope.

---

## P0 — core correctness (do first, in order)

### [x] 1. Add/remove apps — ROOT CAUSE FOUND & FIXED (shipped vm4 / o)
THE bug: `lock-app`/`unlock-app` call `signal_daemon_reload()` → SIGHUP to every
`applockerd` process (so the GATE reloads). The BROKER is also `applockerd` but had
no SIGHUP handler → default action = terminate → the child killed the broker right
after doing the change, before it could reply → "may have crashed" toast; next
click hit the dead socket. Fix: broker now `SIG_IGN`s SIGHUP (it has no list to
reload). The ops were succeeding all along (journal: "locked KCalc"); the broker
just died after. Also shipped: always-refresh UI, lenient remove match, no-match
logging, dev-marker auto-clear on release install.
- Logging added (shipped p): client log `~/.cache/applocker/settings.log` records
  every broker call + reply + any COMPLAINT; broker journal logs request→reply.
  If the "works but still complains" persists on metal, these two show the exact
  reply that fails `_ok()`. Expect it fixed already (broker no longer restarts →
  token sticks → no re-prompt, reply lands → no false toast).
Evidence: toast `unlock-app com.belmoussaoui.Authenticator exited 1`; user must
restart Settings to see changes.
- [x] Settings: add/remove ALWAYS `_refresh_apps()` (even on failure) so the list
  matches disk and a phantom row can't be re-clicked.
- [x] Daemon `remove()` now matches key (exact/case-fold), name, OR basename —
  should end the flatpak-id "exited 1".
- [x] On no-match, `unlock-app` logs the loaded path + every entry to the journal.
- [ ] VERIFY on the VM: add → appears instantly; remove → disappears instantly;
  no restart; no spurious "exited 1". If it still misses, `journalctl -u
  applockerd-broker.service -e` now prints the stored keys vs the query.

### [~] 2. The gate blocks locked-app launches — NATIVE + APPIMAGE WORK (metal)
User confirmed on metal: native apps + AppImages are blocked; add/remove works.
Root causes cleared: stale dev-mode marker (release postinst clears it) + broker
SIGHUP self-kill (fixed).

### [x] 2d. Flatpaks — FIXED & VERIFIED ON METAL (vm6)
Confirmed: locks Zen Browser, LocalSend, GearLever. All four app types now gate:
native, AppImage, flatpak, system apps. App-locking core is COMPLETE.
Probe on metal confirmed: a locked flatpak launch shows
`exec="/usr/bin/bwrap" cmdline="/usr/bin/flatpak run … <app-id> …"` — the app-id is
in the launcher's pre-exec cmdline. Fix: `LockList::matches_flatpak_cmdline` matches
a locked flatpak's app-id as a cmdline token when the exec is `flatpak`/`bwrap`;
noise (triggers, system-helper, our prompt) has no app-id → no false hit. Unit-
tested. VERIFY on metal: lock a flatpak → launching it prompts; unlocked flatpaks
and other bwrap uses are unaffected. (Old text below.)

### [ ] 2d-notes. FLATPAKS BYPASS THE GATE (original analysis)
Flatpaks "go right over." Reason: `flatpak run <id>` enters a **bubblewrap mount
namespace FIRST**, then execs the app as `/app/bin/<app>` inside it — so the real
exec's path is the sandbox path, never the host `…/flatpak/app/<id>/…` that
`LockList::matches` looks for. Native/AppImage exec their host binary directly →
they match; flatpaks don't.
Candidate fixes (evaluate — hard problem):
- When the gate sees an exec of the flatpak launcher/`bwrap`, read
  `/proc/<pid>/cmdline` for the locked app-id and gate on that. (Caveat: at
  FAN_OPEN_EXEC_PERM time cmdline may still be the pre-exec value — verify which
  stage exposes the app-id; the bwrap exec likely does.)
- Or match the app binary's **host inode** (dev+ino) instead of its path, so the
  namespace remap doesn't hide it.
- Or override the flatpak's `.desktop` to launch via an AppLocker wrapper that
  auth-checks then `flatpak run`s (mechanism outside fanotify).
Acceptance: launch a locked flatpak → prompt before it opens; unlocked flatpaks
unaffected.
Root cause: the **dev deb disables the fanotify gate on purpose** (freeze-safety),
and the broker is NOT the gate. So on a dev build locked apps always launch. Real
enforcement needs the RELEASE build (no `/etc/applocker/dev-mode` marker).
- [x] Built `dist/applocker_0.0.1-vm1_amd64.deb` (`APPLOCKER_RELEASE=1`) — real
  system-wide, exec-only gate. **VM ONLY — snapshot first.**
- FOUND: a leftover `/etc/applocker/dev-mode` marker survived a dev→release
  upgrade, so even the release gate no-opped ("DEV MODE — not gating"). Fixed:
  release postinst now `rm -f /etc/applocker/dev-mode` (shipped vm3).
- BLOCKER: broker still crashes on the VM (toast "…may have crashed") even with
  catch_unwind → likely a segfault/abort. NEED `journalctl -u
  applockerd-broker.service -e` to see the cause. Everything empty (no faces/apps)
  because add-face/add-app go through the dead broker.
- [ ] VM test: snapshot → install vm3 → set PIN → lock an app →
  `sudo systemctl enable --now applockerd.service` (the GATE unit, not broker) →
  launch the locked app → it must prompt (face→PIN) BEFORE starting; deny blocks,
  allow runs. Unlocked apps unaffected; a hung auth fail-opens after 30s.
- [ ] If solid in the VM, decide the host story: keep gate opt-in, or a scoped
  mode. Do NOT enable the real gate on the daily-driver host until proven.
- Note: gate auth is its own path (cmd_gate → auth::run + GuiPrompter), separate
  from the broker but the same face→PIN→sudo routine.

### [x] 3. Auth flow consistency — RESOLVED BY LOGS
Broker log shows `authenticate(...): face_live=false attempts=1 ... — trying face
first, then fallback prompt`. So it IS face-first; with no enrolled face it goes
straight to PIN/sudo (correct). The "sudo then face" the user saw was the WIZARD
(set-pin needs sudo, then enroll), not the auth routine. No bug. Re-open only if it
recurs with a face actually enrolled.

### [ ] 3b. (was 3) — n/a
Evidence: opening Settings "asks for sudo then face" (wrong order / double), and
add/remove don't re-auth within the window.
- The routine must be **face first (≤3), then PIN/sudo fallback**, one behaviour
  everywhere. Investigate why sudo is prompted before face on open.
- Confirm the window-token model: open = one auth; changes inside ride it;
  weakening a factor / folder reveal / Change-PIN force a fresh auth. Make sure
  this matches what the code does (user reports the re-auth was "removed without
  changing code" — verify the token cache actually behaves).
- Acceptance: open Settings → face tried first, fallback only if it fails; add/
  remove inside the window don't re-prompt; sensitive changes do.

---

### [x] 2b. Neuter the stale `applocker on` script (SAFETY)  (shipped vm2)
KDE note: login manager is **SDDM**, lock screen is **kscreenlocker** (PAM service
`kde`). The Mint targets the old scripts still hardcode (`lightdm`,
`cinnamon-screensaver`) DO NOT EXIST here → they no-op. So PAM login-lockout is far
less likely than feared; the only edit that lands is `/etc/pam.d/sudo` (sudo would
fail but login via SDDM still works). When the (far-later) PAM tier is done, target
`sddm` + `kde`, and `applocker-pam` must be reworked off its Mint targets.
`packaging/bin/applocker` `on` still does Mint-era **PAM edits** (`/etc/pam.d/sudo`)
and references `cinnamon-screensaver`. This is the path that broke login before and
is wrong for KDE. Make `applocker on/off` manage ONLY the gate service
(`systemctl enable/disable applockerd.service`) — never touch PAM. PAM/greeter
integration stays a separate, explicit, far-later step. Verify `applocker off`
cleanly removes anything `on` added to `/etc/pam.d/*`.

### [ ] 2c. Make dev-vs-release obvious
"No enforcement" on a dev build is invisible. Surface the gate state (dev-mode /
release / running / stopped) prominently in Settings and in `applocker` output, so
a user always knows whether launches are actually gated.

### [ ] 2e. Terminal-sudo PAM: confirmed SAFE on KDE — make it a proper opt-in
Metal check: `/etc/pam.d/sudo` has `auth sufficient pam_applocker.so` (only sudo;
login/SDDM untouched). `sufficient` = tries face/PIN, falls back to the password →
CANNOT lock you out. User likes it (it's the "PIN for sudo" they wanted). Revises
the old "PAM breaks login" fear: that was Mint/LightDM; on KDE, sudo-only PAM is
fine. TODO: make `applocker-pam enable sudo` a documented, intentional opt-in
(button in Settings?), keep it `sufficient` + sudo-only, and confirm the module
prompts sanely in a pure TTY (no GUI). Login/lockscreen PAM stays deferred.

## P1 — the hidden files/folders feature (explicitly requested, repeatedly)

### [~] 4. Hidden files & folders list with face-reveal
DECISION (user, hard): the hide-in-place tier does NOT encrypt or mount ANYTHING —
trying to encrypt Documents once wedged the machine. It ONLY edits `.hidden`. The
per-entry-vault idea below is SUPERSEDED for this tier (the encrypted Private folder
stays the strong tier and is NOT touched).
DONE: `hide/hidelist.py` — pure `.hidden` manager (no root/FUSE/daemon). Registry of
managed paths in `~/.config/applocker/hidden.json`; `set_hidden()` adds/removes a
path's basename in its parent dir's `.hidden` (Dolphin/Nautilus honour it — same
convention vault.py already uses). CLI: add/forget/hide/reveal/toggle/hide-all/
reveal-all/list/status + `--selftest` (passes; verifies the file never moves).
Staged into the deb (`hide/*.py`).
DONE: Settings "Hidden files & folders" section (`_build_hidden_section`) — modelled
on the locked-apps list but with an "Add file…" / "Add folder…" file picker
(FileChooser OPEN / SELECT_FOLDER); rows show basename · dir (state), the X reveals +
forgets (`cmd_forget`). Warning label up top: it only hides (no encrypt/move), not as
secure as the Private folder — move files there for real protection.
DONE: auto-reveal watcher `hide/hide_watch.py` — inotify IN_OPEN (ctypes libc, no
deps; watch-only, CANNOT freeze) on each containing dir. Dir listed (opendir → empty
-name event) with hidden items → SILENT face check (`recognize.py --no-liveness`, no
dialog) → on owner match reveal them; re-hide on session lock (loginctl LockedHint) or
after `--reveal-timeout` (default 15 min). Face-only on purpose: background daemons
open ~ constantly, a PIN dialog would pop while away. Single-instance flock; autostart
(`applocker-hide-watch.desktop`, no OnlyShowIn so it runs on KDE) + settings spawns it
on first hide. inotify empty-name detection unit-verified. VERIFY on metal: hide a
folder in ~, open ~ in Dolphin → face check reveals it; lock screen → it re-hides.

**HARD CONSTRAINT (learned the hard way):** NEVER use the fanotify FAN_OPEN_PERM
file-gate. Locking a normal folder (Documents) with it froze the whole system —
the daemon's own file reads got caught in the open-gate and deadlocked. The ONLY
safe mechanism is the **encrypted vault** (gocryptfs), generalized to any user-
chosen location:
- Each list entry = a per-location encrypted vault (same machinery as ~/Private,
  which already works). Locked = ciphertext/empty even to Show-Hidden; unlocked =
  real files.
- A single FILE the user picks → move it into a per-entry hidden vault; reveal on
  face. (gocryptfs is dir-based, so a file becomes a tiny vault.)
- **inotify auto-reveal** (watch-only, CANNOT freeze — unlike fanotify): folder
  accessed → face check → mount/reveal → idle/timeout → unmount/hide.
- Settings: new "Hidden files & folders" section, add/remove like the apps list.
- Acceptance: add a folder → locked it's empty even with Ctrl-H; face-reveal shows
  contents; re-locks on timeout; picking Documents does NOT freeze anything.

---

## P2 — NEXT (user chose this before P1)

### [~] 8. Automatic screen brightness (gated behind active presence)
STEP 1 DONE (shipped u): SIMPLE UI — toggle + explanation, sliders behind an
"Advanced" Gtk.Expander (collapsed). REMOVED the custom yellow-band draw handler
(get_range_rect on every paint) — it made the app sluggish and threw GTK warnings
(KDE crash reports). Keep it simple; do not re-add per-paint custom drawing.
Earlier (shipped q): user-side config `~/.config/applocker/brightness.json`
(read_brightness/write_brightness) + Settings panel `_build_brightness_section`
(toggle + 4 time-of-day Gtk.Scale sliders + adjustment-area slider), only shown
when presence is ON (`_refresh_brightness_visibility`, `set_no_show_all`). Native
Gtk → follows KDE theme. Saves live on change. Config round-trip tested.
STEP 2 DONE (shipped v): watch_presence.py reads brightness.json; the snapshot now
also returns the frame's mean luma; `maybe_adjust_brightness` computes target =
time-of-day level nudged ±area by room luma and sets the screen via backend detect
(brightnessctl → ddcutil setvcp 10 → KDE PowerDevil qdbus), logging the target +
backend on change. Compute logic tested. LIMITATION: only fires on the presence
snapshots, which happen when IDLE — so it adjusts while reading/watching a video
(the main case) but not while actively typing. DONE: periodic brightness-only peek
runs on its own cadence (`--brightness-interval`, default 30 min) whether active or
idle — `grab_room_luma()` grabs one frame, `maybe_adjust_brightness()` sets it, camera
released immediately; skipped while locked / on battery. Fires once on watcher start.
VERIFY on metal: which backend works for the external monitor.
STEP 2 (was TODO): watch_presence.py reads the config; on each idle snapshot compute
target = level(time-of-day) nudged by mean-frame luminance within ±area; set the
screen brightness. Backend detect (brightnessctl → ddcutil setvcp 10 → KDE
PowerDevil D-Bus), log which worked. Time-of-day → level buckets (morning/midday/
afternoon/night) by local hour.
Original design:
User design (mockup): invisible until presence is enabled; then a toggle
"Automatic screen brightness" + a panel: four time-of-day target levels (Morning /
Midday / Afternoon / Night, each 0–100%) and an "automatic adjustment area" slider
(how far room-darkness may shift the target, e.g. ±25%). The presence watcher
already wakes the camera periodically → measure room luminance there (mean frame
brightness) and set the screen within [target − area, target + area].
- **No root/broker:** brightness is a user-session preference. Store in
  `~/.config/applocker/brightness.json` (written by Settings, read by
  watch_presence.py). Same pattern as faces.
- Settings: new panel, only shown when presence is ON. Native Gtk widgets
  (Gtk.Scale sliders) so it follows the KDE theme — DO NOT hardcode colors.
- watch_presence.py: on each idle snapshot, compute target = level(time-of-day)
  nudged by room luminance within the adjustment area; set brightness. Backend
  detect: try `brightnessctl`, then `ddcutil setvcp 10` (external DDC monitor),
  then KDE PowerDevil D-Bus (`org.kde.ScreenBrightness` / PowerManagement). Log
  which backend worked. User has an external monitor (~20% dark, 100% daylight).
- Acceptance: enable presence → brightness panel appears; set levels → screen
  brightness tracks time-of-day + room darkness; disable presence → panel hides.

### [x] 6. GTK → Qt port — VETOED by user
GTK native widgets already follow the KDE theme (no hardcoded colors). A Qt port
would break working theming for no gain. Keep GTK. Do NOT revisit.

## P1 — hidden files/folders (deferred: AFTER P2 brightness, per user)
Design ref (user mockup): a "Hidden files" toggle + "Hide files, add folders (+)"
list (e.g. Videos, Music), with a warning: "Hidden files are not as secure as the
private folder — this only hides your files; move them to the private folder for
better privacy." (So this hide-in-place tier is explicitly the weaker, convenient
option alongside the encrypted Private folder.) Still: NEVER fanotify — see P1.4
constraint above.

## P2b — polish / deferred

### [x] 5. Enroll camera preview is blank — REWORKED render path
Evidence: capture progressed (5/8) but the preview stayed dark. The DrawingArea +
manual cairo `draw` handler never painted on KDE/Wayland (even the bg fill didn't
show → the signal/allocation wasn't landing). Fix: dropped the DrawingArea and now
paint each frame into a `Gtk.Image` via `set_from_pixbuf` from the main thread (the
reliable standard path), with the green "got it" check on a `Gtk.Overlay` instead of
a per-paint cairo arc. Removed the frame/draw stderr diagnostics. VERIFY on metal.

### [ ] 6. GTK → Qt/PySide6 native port
All 5 GUIs are GTK on a KDE box. Port to PySide6 (protocol unchanged). Deferred
until P0/P1 are solid.

### [x] 7. Vault section layout on delete+recreate — ROOT CAUSE was the wrapped label
Earlier destroy()+_reflow() didn't fix it: it's not stale pixels, it's a real
mis-layout. The off-state intro label is `wrap=True` with no width bound, so GTK
measures its height for the UNWRAPPED (one-line) width → the frame gets too little
height → the "Enable file lock" button overflows into the next section. Fix:
`set_max_width_chars(46)` on that label (and the hidden-section warn/hint labels) so
height-for-width is stable. VERIFY on metal: delete → recreate, no overlap.

---

### [x] 9. Camera arbitration (priority queue)
Multiple helpers wake the webcam; opening it twice fails. `face/cameralock.py` — an
advisory flock + per-pid "wants" markers giving a priority ladder:
LOCKSCREEN(40) > APP(30) > FILE(20) > PRESENCE(10). A requester takes the camera only
when no higher level is pending; PRESENCE never waits (busy → blind → retry), the
user-driven levels wait out their budget. Wiring: `recognize.py` self-registers via
`--camera-priority` / `$APPLOCKER_CAMERA_PRIORITY` (unset = unarbitrated for tests);
daemon `face.rs` sets `app` for the app-gate; `hide_watch` sets `file` for reveals;
`watch_presence` takes PRESENCE directly at both camera sites (idle snapshot + 30-min
brightness peek). Confirmed: presence face-checks are idle-gated (only after
`--idle-after`); only the brightness peek opens while active. Arbiter unit-tested.
LOCKSCREEN reserved (screen-unlock face tier is a later step).

### [~] 10. Hidden files: re-hide timing
User: reveal works, but items stay visible after leaving/closing the file manager
(old default was 15-min timeout). HARD LIMIT: file managers don't hold a dir open
while showing it (they opendir→list→close, then watch), and there's no reliable
cross-FM "current directory" API — so inotify catches ENTERING a folder but not
sitting-in / leaving-to-an-unwatched folder. Also, re-hiding edits `.hidden` → the FM
re-lists → looks like re-entering (flicker trap). Implemented the feasible model:
watch each target's PARENT (reveal) AND the TARGET dir itself (being inside keeps it
shown); re-hide on session lock (instant) OR inactivity timeout (default 60s,
refreshed by folder/target opens); `--recheck-cooldown` (15s) after an auto-hide
suppresses the re-list from re-prompting. TODO/opts to discuss: tie re-hide to the
presence watcher's "you left" instead of a blind timeout; per-entry timeout in the UI.

## Done this session
- Broker replaces pkexec; PIN works for Settings changes.
- One-auth-per-window token; folder reveal + weakening a factor force fresh auth.
- Sudo autocomplete: encrypted root-side, face/PIN gated, lockout, Change-PIN UI.
- Presence needs a camera (not an enrolled face); face-unlock needs an enrolled face.
- Broker panic-isolated + self-healing unit; failure toasts explain + link journal.
