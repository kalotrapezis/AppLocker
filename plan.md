# AppLocker — working plan

Single source of truth for outstanding work. Claude follows this **autonomously**:
pick the top unchecked task, do it, test it, tick it, move on. The user only
interrupts to say "stop" / "continue at <time>". No questions unless a task is
truly ambiguous.

Target env: **Kubuntu / KDE / Wayland**. Test build is dev-mode (gate can't freeze).
Version bumps land in `dist/applocker_0.0.1-<letter>_amd64.deb`.

> **Future — GTK4 / libadwaita port:** see [`plan-gtk4.md`](plan-gtk4.md). Phased
> view-layer rewrite (backend untouched) that makes the UI match the design mockup.
> Unblocked by Mint moving to Wayland. This is the chosen direction over a Qt port
> (the old Qt idea was vetoed — GTK already follows the KDE theme; GTK4 keeps that).
>
> **A web-based UI was considered and rejected (2026-11-23).** It would mean
> shipping a browser engine (Electron/webview) plus a localhost server for a
> settings window and a PIN dialog — ~100 MB of runtime, a new attack surface on
> the one dialog that gates everything, no KDE theme/accent following, no system
> font, and a prompt that can't reliably stay modal and keep-above over a Wayland
> lock screen. GTK4/libadwaita gets the same visual freedom with none of that.

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

### [x] 0h. 0.0.10 — ROOT CAUSE of "it opens even when auth fails" FIXED
The confirmation existed and was never rendered. `Feedback::ok()` sent `ok` then
`reap()`d, and reap drops the child's stdin — that EOF quit the window instantly,
racing its own 700ms timer. So every successful face unlock was silent: the app
just opened, which is exactly what "the lock did nothing" looks like.
- Daemon waits (OK_LINGER 1400ms) instead of reaping; window also ignores an EOF
  once the ✓ owns the shutdown. Either side alone fixes it; both are in.
- Tick visible 700ms → 1200ms (a flash after a 2s scan was missable).
- MEASURED: window lifetime 0.18s before (GTK startup only, nothing drawn) →
  1.38s after. Control: EOF with no tick still exits in 0.18s, so nothing
  lingers.
- [ ] STILL SILENT: the fingerprint tier (runs before any window exists) and the
  BROKER's face path — serve.rs calls face::build() directly and never wraps it
  in FaceWithFeedback, so Settings/sudo-release face unlocks show nothing. Same
  class of bug, different path. Wiring the broker to the feedback window is the
  next real UX win.

### [x] 0g. 0.0.9 — upgrades apply without a reboot
`postinst` try-restarted only the broker, so with the gate on an upgrade left
the OLD binary enforcing until reboot (a fix would look like a no-op). Now both.
No-op when the gate is off, so it never enables enforcement.
State after the user disabled the gate and rebooted (2026-11-24): gate
inactive+disabled, broker active, watcher AUTOSTARTED on its own (pid 3005) with
the lock-screen face unlock live — no manual start needed any more.

### [x] 0e. GATE ON — Viber mystery SOLVED (my diagnosis was wrong, twice)
Journal evidence (Nov 20-23, gate was running): 7 Viber launches, ALL
`allow (auth passed)` in 1-3s. No DENY, no fail-open, no second instance.
=> The cause was **face passing silently in ~2s**: the app opened with no dialog
and that looked like a bypass. NOT the watchdog (0.0.3), NOT the AppImage mount
(0.0.5) — both were real code holes, neither was this symptom.
- Verified 07:20:07 with the gate on: cancelled prompt → `DENY (auth failed)`,
  no viber process, no /tmp/.mount_viber* → the lock holds.
- Confirmed live that the real binary runs as
  `/tmp/.mount_viber.nBOMAE/usr/bin/Viber` — the path the pre-0.0.5 gate could
  never match. The hole was real even though it wasn't the complaint.
- Liveness passes in ~2s from ordinary movement. Treat it as a speed bump.
- [ ] THE REAL FIX for the original complaint: show a brief "Face recognised"
  confirmation on an allow, so a silent pass doesn't read as "no lock at all".

### [ ] 0f. FINGERPRINT READER IS BROKEN ON THIS MACHINE (driver, not us)
fprintd/libfprint during a real attempt:
  `Read failed: device was disconnected, aborting`
  `Error; expected 8 bytes but got 8000, failing`
  `Device reported an error during verify: Init: expected 8 bytes but got 8000`
  `Driver reported a match providing a scanned print that is not matching any
   in the gallery`   ← libfprint rejecting a match the DRIVER claimed
Chipsailing CS9711 (press). Also why `sudo` times out at the reader.
- [x] A failed finger no longer spends a password attempt (0.0.8).
- [x] Reader PAM timeout 10s → 6s.
- [ ] DECIDE: keep fingerprint as the first factor on THIS box? With this driver
  it mostly buys 6s of silence before the dialog. Off switch:
  `Environment=APPLOCKER_FP=0` in a drop-in for applockerd.service.
- [ ] Face is OFF in policy and no PIN is set, so the only reliable factor right
  now is the account password. `applockerd set-face on` / `set-pin` restore the
  fast paths (face was matching 7/7 in ~2s).

### [x] 0d. INSTALLED + VERIFIED ON METAL (0.0.5, 2026-11-24 07:09)
- `dpkg -i` said "previously unselected package" → AppLocker was genuinely NOT
  installed any more. The "never lets go" symptom was leftover STATE (PAM lines,
  .hidden markers, user data), not a live install. 0.0.5's prerm fixes that.
- Lock-screen face unlock verified on the INSTALLED build: locked 07:09:49 →
  "face + liveness matched at the lock screen — session unlocked".
- The watcher only autostarts at LOGIN; started by hand this time with
  `setsid python3 /usr/lib/applocker/watch_presence.py &`.
- DO NOT run `applocker-pam enable fingerprint` on this box: Ubuntu's
  pam-auth-update ALREADY has `pam_fprintd` in /etc/pam.d/common-auth
  (max-tries=3 timeout=15), which /etc/pam.d/kde @includes — so the greeter
  already offers the reader, and our tier would stack a second 10s wait in
  front of it. `enable_one` now detects this and refuses.
- [ ] ENFORCEMENT IS STILL OFF: `applockerd.service` is disabled/inactive, so
  locked apps (Viber, Zen, Steam) are NOT gated. Nothing about the Viber fixes
  can be confirmed until `sudo systemctl enable --now applockerd.service`.

### [x] 0c. 0.0.5 — AppImage inner exec, fingerprint-first, trust window
- User's hypothesis about Viber was RIGHT about the mechanism: an AppImage execs
  its real binary from its own FUSE mount, a path the lock never matched. Gate
  now resolves an exec inside any `fuse.*` mount to its mount SOURCE and matches
  that (`matches_appimage_source`; source is usually the bare filename). Unit-
  tested. A stale `viber.appimage` mount was found live in /tmp — exactly the
  leftover that lets a second instance run unchecked.
- Could NOT reproduce the open-after-failed-auth live: Viber only stays up while
  the launching shell lives, and the gate isn't enabled on this host. The 0.0.3
  watchdog fix and this one are independent; both were real.
  - [ ] TO CONFIRM with the gate on: lock Viber, fail the PIN 3×, and check the
    journal for `DENY (auth failed)` plus no second `allow` line for a
    `/tmp/.mount_*` path.
- Fingerprint is now the first factor everywhere (gate + broker get-sudo), dialog
  leads with it. Order: fingerprint → face → PIN → password.
- Camera duty cycle: only while the lock screen is LIT (DRM dpms, sysfs), 6 fast
  tries then 30s until the screen blanks and wakes again.
  - [ ] UNVERIFIED on this hardware: that dpms flips to Off when Plasma blanks
    the locked screen (it read `On` while awake, which is all I could check with
    the user at the keyboard). Check with `cat
    /sys/class/drm/card1/card1-eDP-1/dpms` while the lock screen is dark. If it
    stays On, the 6-tries backoff still bounds the camera to a look every 30s.
- Trust window: 20 min after an accepted fingerprint (stamp
  `/run/applocker/last-fingerprint`, tmpfs) or a connected phone from
  `~/.config/applocker/trusted-bt` → glance without liveness. Greeter-side
  fprintd does not stamp (not our code path).

### [x] 0b. 0.0.4 — "skip the confirmation on login": no button anywhere
The greeter's Unlock button cannot be removed by a PAM module (plan 2f's open
question, "why a button, can't it just enter"). Answered by not using PAM for
this path at all: while locked, `watch_presence.py` runs the identity check and
calls `loginctl unlock-session` on a match; kscreenlocker dismisses itself on
logind's Unlock. VERIFIED LIVE on Plasma 6.6/Wayland (lock → face + turn → in).
- No root, no PAM, no lock-out risk: a failure leaves the normal greeter.
- On via the existing lock-screen face toggle OR `touch
  ~/.config/applocker/lockscreen-unlock` (no root). Liveness kept.
- 5s cadence for 180s after the lock, then 30s; skipped when the session isn't
  active, and on battery when "pause on battery" is set.
- Fingerprint now runs BEFORE the prompt (no button to click), and
  `applocker-pam fingerprint` can put pam_fprintd in KDE's stack so the greeter
  itself says "touch the sensor" — sandbox-tested, NOT enabled (login PAM edit;
  run it yourself with a root shell open).
- [ ] VERIFY after installing 0.0.3/0.0.4: the autostart only starts the watcher
  at LOGIN, so after `dpkg -i` either log out/in or run
  `python3 /usr/lib/applocker/watch_presence.py &` once.
- [ ] Open: no on-greeter feedback for the turn challenge (same gap as 2f). With
  the camera path this matters more — you get no hint that it wants a head-turn.
  Options: notify via PAM is unavailable here (we're outside PAM), so either
  drop liveness for this tier (user call) or draw nothing and rely on habit.

### [x] 0. 0.0.3 — fail-open hole, bystanders, fingerprint, clean uninstall
Four user reports, all root-caused and fixed (see CHANGELOG 0.0.3):
- Gate fail-open watchdog covered the whole auth routine → a locked app opened
  on *failed* auth once face+PIN crossed 30s. Watchdog now stops when a dialog
  appears (`AuthMsg::Prompting`, `wait_for_verdict`, unit-tested in main.rs).
- Unlock cache was not wiped by a Plasma lock (KDE sets `LockedHint`, never
  calls `Session.Lock`). Listener matches both now.
- YuNet picked the highest-confidence face, not the nearest → a bystander beat
  the owner. Now largest box above a 0.7 score floor (`face/engine.py`).
- Uninstall left PAM lines, `.hidden` markers, mounted vaults and running
  helpers. `prerm` cleans all four; `postrm` explains the per-user data it
  deliberately keeps.
- NEW: fingerprint tier via `pam_fprintd` (`/etc/pam.d/applocker-fprintd`,
  `Method::Fingerprint`, "Fingerprint" button in the prompt).
- [ ] VERIFY on metal: (a) lock Viber, fail the PIN 3× → it must stay shut;
  (b) a second person in frame must not stop your own face unlocking;
  (c) tap the reader at the prompt → unlocks; (d) `apt remove applocker` →
  no unlock button on the lock screen, folders visible again, no leftover
  `/usr/lib/applocker` processes.
- [ ] Fingerprint polish: an on-screen "touch the reader" state (needs the
  prompt to talk to the daemon over the socket, not one stdout line), and a
  Settings toggle. Today it is auto-offered whenever a finger is enrolled.


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

### [~] 2f. Lock-screen face unlock (KDE) — TOGGLE SHIPPED (0.0.1-ad)
User asked for a Settings toggle. Done: "Also unlock the screen lock with my face"
switch in the Face section. Wiring, all `auth sufficient` (password ALWAYS still
works — can't lock out):
- `applocker-pam` gained a `screenlock` tier → `/etc/pam.d/kde` (KDE kscreenlocker's
  PAM service). It MATERIALISES `/etc/pam.d/kde` from the vendor `/usr/lib/pam.d/kde`
  when absent (this host had none), inserts our rule first, and on disable removes it
  — deleting the file if it's back to vendor-identical. Sandbox-tested.
- PAM line carries `priority=lockscreen`; `pam_applocker.so` gained a `priority=` arg
  → sets `APPLOCKER_CAMERA_PRIORITY` so the unlock face check preempts the desktop
  helpers (top of the camera ladder). Runs with liveness ON (login tier).
- Broker op `pam-screenlock on|off` runs `applocker-pam` as root (serve.rs); settings
  `set_screenlock()` prefers the broker (PIN works), falls back to `pkexec`.
- Toggle needs an enrolled face; reflects real /etc/pam.d/kde state; force-auths.
VERIFIED ON METAL (user): lock-screen face unlock WORKS.
Follow-ups shipped 0.0.1-ae:
- Grouped Settings section "Unlock these with your face" with toggles: Lock screen,
  Terminal sudo, Graphical (app) password prompts (polkit). Moved the lockscreen
  toggle out of the Face section (user hadn't found it).
- New `uisudo` tier → /etc/pam.d/polkit-1 (vendor-materialised); broker op generalised
  to `pam <tier> on|off` (whitelist sudo|uisudo|screenlock).
GRAPHICAL POLKIT PROMPTS CAN'T USE PAM FACE (proven, 0.0.1-af): `polkit-agent-helper@.service`
runs with `PrivateDevices=yes` (no /dev/video), `DevicePolicy=strict DeviceAllow=/dev/null`,
and `ProtectHome=yes` (no /home → no faces/models). So our PAM module runs recognize in a
jail with no camera and no faces → always falls through to the password (adds ~1-2s latency).
Removed the `uisudo` tier entirely (do NOT weaken polkit's sandbox to force it). The section
now only offers Lock screen + Terminal sudo. Graphical prompts must use the polkit AGENT
(`gui/polkit_agent.py`, already written, --takeover) which runs in-session with camera
access; its model is "type the password once per session, then face auto-submits it." NEXT:
wire a toggle + autostart for the agent (caveat: replaces KDE's agent for the session).

STILL OPEN (user wants, deferred): on-greeter LIVENESS FEEDBACK. Liveness is KEPT
(don't reduce security — user was explicit). Terminal shows turn-prompts (stderr);
lock screen / polkit have none. Plan: forward recognize.py's challenge lines to the
greeter as PAM_TEXT_INFO messages via the pam_conv (works on Wayland lock, no window).
The `--ui` guided window won't show above a Wayland locker, so PAM messages is the way.
Also open: "why a button, can't it just enter" — the KDE greeter always draws its
unlock button; our sufficient module unlocks on match regardless. Nothing to fix
unless we later suppress the greeter field.

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
