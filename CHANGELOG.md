# Changelog

Newest first. `0.0.1-<letter>` entries were testing rounds leading up to the first
real release, **0.0.1**.

## 0.0.26 — a Fingerprint page, with per-place switches

- **New "Fingerprint" page** in the sidebar, between Security and Locked apps.
  The reader's state, the passes you have enrolled, and **Add a pass… /
  Test (5 presses) / Remove all** — the enrolment that used to be a CLI script,
  in the app. The ⚙ on the Security card jumps here.
- **"Unlock these with your finger"**: one switch each for **Locked apps**,
  **Private files** and **Lock screen**. The first two write the per-tier policy
  from 0.0.25; the lock-screen one adds `pam_fprintd` to KDE's own login stack
  through the broker, which is what makes the **greeter** offer the reader —
  the thing that was missing.
- The lock-screen switch reads its true state from `/etc/pam.d/kde` and
  `common-auth`, so it reflects what the system actually does rather than what
  AppLocker wishes.

### Still CLI-only
Face, PIN and password per tier, and the `both` cross-check, are set with
`applockerd set-methods <tier> <list>`; the page only exposes the fingerprint
half of that matrix so far.

## 0.0.25 — apps, lock screen and files get their own methods

### Added
- **Per-tier authentication.** Each of the three places AppLocker guards now
  has its own method set, configured apart:

```
applockerd set-methods apps       fingerprint,face,pin,password
applockerd set-methods lockscreen fingerprint,face,both
applockerd set-methods files      fingerprint,face,both,pin
```

  `both` means the two biometrics must agree — and liveness drops in that mode
  on purpose: a second factor is better evidence than a head turn, and asking
  for both at once is what makes people switch it off.
- Written to `/etc/applocker/config` as `apps_methods`, `lockscreen_methods`
  and `files_methods`. **An older config keeps behaving exactly as before**:
  tiers with no key inherit the global switches (and the lock screen never
  inherits the PIN — the greeter takes an account password).
- The gate and `auth-test` run the **apps** tier: its methods, its cross-check,
  its liveness.
- The watcher runs the **lock screen** tier. With `both`, the camera stands
  down unless a fingerprint was accepted recently — that stamp *is* the second
  factor, since the camera cannot ask the reader itself. (A fingerprint taken
  by the greeter's own PAM does not stamp, so only app/sudo unlocks open that
  window.)

### Fixed
- **Stopping the service now stops the lock-screen face unlock too.** The
  watcher is a user-session process and kept unlocking the screen after the
  gate was stopped, which reads as "I turned AppLocker off and it is still
  doing things". It checks `applockerd.service` (cached 10s) and stands down
  with it; if it cannot tell, it stays on rather than silently disabling a
  security feature.
- **A forgotten prompt no longer wedges the broker.** It serves one request at
  a time, so an unanswered dialog blocked every later Settings action until the
  window was closed — which is exactly what happened when Settings refused to
  open with "broker down". Dialogs are now closed and treated as cancelled
  after 180s.

## 0.0.24 — fingerprint enrolment inside Settings, and cards that grey out

### Added
- **A ⚙ dialog on the Fingerprint card**, mirroring the face one: the passes
  you have enrolled, **Add a pass…** with live progress ("3 presses accepted —
  keep going"), **Test (5 presses)** which reports how many matched, and
  **Remove all** behind a confirmation. Each pass goes into its own fprintd
  slot, labelled by how to hold the finger — flat, rolled left, rolled right —
  because a press sensor only sees one patch of skin per template.
- The card summarises itself: "2 passes enrolled — add more with ⚙ if it
  misreads."

### Changed
- **Hardware that isn't there is greyed out.** No fingerprint reader → the
  switch and ⚙ are disabled with "No fingerprint reader detected". No camera →
  Face unlock is disabled with "No camera detected". Previously both offered
  switches that could only ever fail.
- The reader probe runs **off the UI thread** (fprintd can take seconds to
  answer on a flaky device), so Settings opens instantly and the card enables
  itself a moment later rather than freezing.

## 0.0.23 — `applocker-fp-enroll`: more passes of the same finger

"I don't know why, now it's like it's a different finger." Because there is
exactly **one** template: `fprintd-enroll` fills a single finger slot, and a
press-type sensor only photographs a small patch of skin — press at a slightly
different angle and it is, as far as the matcher is concerned, a different
finger. Phones take a dozen-plus captures across the fingertip.

New `applocker-fp-enroll` spends three slots on the **same** finger, held
differently each round (flat, rolled left, rolled right). `pam_fprintd` matches
against every enrolled slot, so any of the three opens a lock.

```
applocker-fp-enroll            # add passes
applocker-fp-enroll --reset    # wipe first, then enrol
applocker-fp-enroll --check    # 5 presses, reports how many matched
```

`--check` is the honest part: it tells you whether enrolment actually helped on
this hardware, or whether the USB connection is the problem after all. Four
matches out of five or better is healthy.

Not reinventing libfprint, deliberately: the driver decides how many presses an
enrolment takes, and a reader that fabricates matches does so below any
template store we could write. More slots is the real lever available to us.

## 0.0.22 — the gate covers every filesystem, not just `/`

The hole behind everything else: **`/tmp` is tmpfs, a different filesystem, and
the gate only marked `/`.** No fanotify event was generated there at all, so
copying a locked app to `/tmp` and running it walked past the lock, the
cross-check and the content matching alike. Found while testing the rename
defence — the copy ran, and the journal had nothing to say about it.

- Every local filesystem is marked now (one mark per device, not per mount):
  ext2/3/4, xfs, btrfs, f2fs, zfs, vfat, exfat, ntfs3, **tmpfs**, squashfs
  (snaps), udf.
- **New mounts are gated as they appear** — a USB stick plugged in later is
  covered, via `poll` on `/proc/self/mountinfo` with a 5s backstop re-scan.
- Deliberately skipped, each for a reason: pseudo filesystems (nothing
  executable worth gating), **network** mounts (answering a permission event
  across an unresponsive server is a hang), and **FUSE** (our handler resolves
  the event path through /proc, and a FUSE daemon blocked behind our own
  pending event is a deadlock — locked AppImages are already matched by their
  mount *source* instead).
- One filesystem failing to mark is logged and skipped, not fatal.

## 0.0.21 — a dead reader stops costing you six seconds per launch

"If face is off and fingerprint is disconnected go to sudo." It already did —
but only after waiting at the reader first, every single launch. With face off
that meant six seconds of nothing before the password box appeared.

A reader that proves unusable (PAM can't run it, or libfprint disowns its
match) is now stamped at `/run/applocker/fingerprint-unusable` and treated as
**absent for 60 seconds**. So the first failure costs the wait; the next
launches go straight to the prompt. tmpfs, so a reboot forgets, and a reader
that starts behaving is picked up again a minute later with no restart.

With face off and the reader in that state, an unlock is now just the password
box, immediately — which is the behaviour asked for.

## 0.0.20 — with face off, stop pretending to look for a face

Reported: "I have face off and it still checks face and finger." The camera was
genuinely not running — `matching against N profile(s)` never appears in the
journal with face off — but the feedback window still played "Looking for you…"
and then a red ✕, because 0.0.14 wrapped the verifier in `FaceWithFeedback`
unconditionally. With face disabled that verifier is a stub, so the window was
narrating a check that did not exist, and the ✕ looked like a failed face scan
holding the unlock up.

The wrapper is now only used when face is actually live. With face off and the
reader on, the window shows the reader and nothing else.

## 0.0.19 — a broken reader steps aside instead of locking you out

Reported straight after 0.0.16 went in: "cross check does not work for apps, it
says no match". It wasn't timing — it was my logic. In cross-check mode face
only ran **if the finger passed**, and this reader either fabricates matches
(vetoed) or fails outright, so face never ran at all and every locked app
refused.

A factor that has *dropped out* is not a factor that said *no*. The fingerprint
check now reports three outcomes instead of two:

| reader says | meaning | result |
|---|---|---|
| accepted | working, it's you | face must agree too |
| rejected | working, wrong finger | both required → refused |
| **unusable** | PAM error, disconnect, or a match libfprint disowns | **not counted**: face alone decides, and the PIN prompt still appears if face fails too |

So "if only one is enabled then we keep the one" now also covers a factor that
has effectively dropped out — however it dropped out. The strict refusal is
kept for a reader that is genuinely working and genuinely says no.

## 0.0.18 — renaming an AppImage no longer unlocks it

`cp viber.appimage game.appimage` used to run unlocked: AppImages were matched
by path. They are now also matched by **content**, so the same program opens the
same lock whatever it is called.

- Fingerprint is `size` + SHA-256 of the **first 1 MiB**, not the whole image:
  hashing 200 MB per launch would be absurd, and a copy is byte-identical, so a
  prefix plus the exact size tells real images apart perfectly well. This is
  theft-of-a-name protection, not collision resistance — say so plainly.
- **4.9 ms** measured on the real 199 MB Viber image, then cached per
  (path, size, mtime), so a relaunch costs nothing and an edited image is
  re-read automatically.
- Only files that really are AppImages are ever fingerprinted (ELF with
  `AI\x02` at offset 8, an 11-byte read), so ordinary binaries pay nothing.
- Checked last, after path, flatpak app-id and FUSE mount source.

Verified on the real file: a 200 MB copy under a different name produced an
identical fingerprint, and an unrelated AppImage did not.

## 0.0.17 — an ignored prompt is a "no", not a "yes" after five minutes

The purpose of the locks turns out to be keeping a child out of certain apps.
Against that, the enemy is not cleverness — it is **patience**. And 0.0.3's
prompt grace handed it over: once a dialog was on screen the deadline became
five minutes, and the timeout still **allowed**. Launch a locked app, walk away,
come back in five minutes, and it had opened itself.

- A timeout **after** a prompt was shown now denies, and logs `DENY (prompt
  ignored for 300s)`. Being asked and not answering is an answer.
- A timeout **before** anyone could be asked still fails open, which is what
  that watchdog was for: a hung recognizer or no display, where nobody is being
  given the choice at all.

### Known gap for this threat model (not fixed yet)
An AppImage is locked by its **path**, plus its mount-source basename since
0.0.5. So copying `viber.appimage` somewhere else is still caught, but
**renaming it is not** — `cp viber.appimage ~/game.appimage` runs unlocked.
Closing that properly means matching on content (a hash of the image, cached by
inode+mtime) rather than a name. Worth doing if the child is old enough to think
of it.

## 0.0.16 — with both biometrics on, they are cross-checked

Asked for directly: "if fingerprint and face is on, cross reference". So when
**both** are enabled *and* usable, neither opens a lock on its own — the reader
and the camera must agree. A photo without the finger, or a lifted print
without the face, now gets the PIN/password prompt instead of the app.

- Applies to the app gate, `auth-test`, the broker's `authenticate()` and the
  sudo-password release — every path that runs the routine.
- **Degrades, never blocks**: the rule only switches on when both factors are
  live. No reader attached, no finger enrolled, or face off → the remaining
  factor works alone exactly as before.
- **A failed cross-check closes the app, full stop** — no PIN/password prompt
  to get round it. Both on means both must be right.
- The refusal is shown, not silent: ✕ plus "Face and fingerprint must both
  match" for 2.2s. A locked app that just fails to open, with nothing on
  screen, is the exact complaint this whole thread started from.
- **One deliberate exception:** the broker's paths — Settings and the
  sudo-password release — still fall through to the prompt. They are how you
  reach the switch that turns a factor off, so a dead camera or a yanked reader
  must never strand you there. The CLI escape is `sudo applockerd
  set-fingerprint off`.
- Settings says so under Fingerprint unlock, so the stricter behaviour isn't a
  surprise.

Worth knowing on this machine: with a reader that drops off mid-read, "both must
agree" means locked apps will refuse more often. Turning Fingerprint off in
Settings returns you to face alone.

## 0.0.15 — a Fingerprint switch in Settings, above Face unlock

- **Fingerprint unlock** is now a card in Security & unlock, sitting above Face
  unlock because that is the order the factors are tried in: reader, then face,
  then PIN/password. Backed by a real policy key (`fingerprint = on|off` in
  `/etc/applocker/config`) and `applockerd set-fingerprint on|off`, so the CLI,
  the GUI and the daemon all read the same switch.
- No "enrol first" guard like face has: availability is re-checked at every
  unlock, so leaving it on with no reader attached costs nothing.
- The Settings window stamped its own version by hand and had drifted to
  **v0.0.2** while 0.0.14 was installed. `build-deb.sh` now writes the real
  version into the installed copy at package time.

## 0.0.14 — the reader now says it is waiting

The fingerprint tier was finished except for the part that makes it usable: it
blocked at the reader for several seconds **with nothing on screen**, and the
dialog's Fingerprint button closed the window before PAM even started waiting.
A fast factor that gives no sign it is listening just looks broken.

- The feedback window learns a `finger` state: ☝ **"Touch the fingerprint
  reader"**, shown for the whole PAM wait, followed by the same green ✓ as a
  face match when the touch is accepted.
- New `FallbackWithFeedback` decorator (same shape as `FaceWithFeedback` and
  `ClosingPrompter`) drives it. It only narrates — a unit test pins that it
  never alters a verdict, errors included.
- The window is now spawned when **either** face or the reader is available; it
  used to appear only for face, which is why the reader tier was always silent.
- Wired into all four auth paths: the gate's worker, `auth-test`, the broker's
  `authenticate()` and the sudo-password release. In the release the reader is
  now tried *after* the window exists, not before.

## 0.0.13 — fingerprint is back on, with a veto instead of a ban

0.0.12 disabled the tier outright. That was the safe reflex, but the wrong
shape: what failed was not "the reader is missing" — the fallback to face/PIN
already handled that — it was the reader **lying**. And libfprint *knows* when
it lies; PAM just doesn't pass it on:

```
Driver reported a match providing a scanned print that is not matching any in the gallery.
```

So `verify_fingerprint` now asks fprintd's log after every PAM success, and
throws the success away if that line (or its `fp_print_equal` twin) appeared in
the last minute. A plain `device was disconnected` does **not** veto anything —
only a disowned match does. Unit-tested on both.

With that guard, the tier is on by default again (`APPLOCKER_FP=0` disables),
which restores the three behaviours asked for:
- reader absent or nothing enrolled → tier skipped, straight to face or PIN;
- reader plugged back in → used again at the next unlock, no restart;
- reader lying → success vetoed, falls back to face or PIN.

**Limit, stated plainly:** this only protects AppLocker's own prompts. The
system's fingerprint PAM — `sudo`, polkit, the KDE greeter — cannot be given
this veto from here. If the reader is trusted enough for those, it is trusted
with `pam-auth-update --enable fprintd`; if it is not, leave them off.

Not implemented, deliberately: binding to a specific reader ("same device").
The bypass came from the *same* device, present and enrolled, so identity
wouldn't have caught it — and it would add a lock-out mode of its own after any
legitimate hardware change. Say the word if you want it anyway.

## 0.0.12 — the fingerprint tier is OFF by default (it produced a real bypass)

### Security — this is the actual "it opens even when auth fails"
At 14:29 on 2026-11-24, three locked Zen Browser launches were allowed with **no
finger, no face, no dialog and no password**:

```
14:29:13 auth   pid=28983  Zen Browser (prompting)
14:29:15 allow (auth passed)  pid=28983  /usr/bin/bwrap      (3 of these in 3s)
```

`fprintd`, at those same three seconds:

```
14:29:15 Driver reported a match providing a scanned print that is not matching any in the gallery.   (x3)
```

Three bogus driver "matches" → three `auth passed`. No camera ran (no
`matching against N profile(s)` lines), no dialog appeared, and the user
confirmed no finger was on the reader. A flaky libfprint driver (Chipsailing
CS9711) handed PAM a success nobody asked for — and PAM success is all this code
can see from the inside.

**The fingerprint tier is opt-in now** (`APPLOCKER_FP=1`), not opt-out. A lock
must fail safe, and a factor that can approve itself does not get to be on by
default. This reverses "make fingerprint the default", on evidence gathered
after that decision. One env var turns it back on where the reader is trusted.

### Not only AppLocker — check the system stack
`pam_fprintd` also sits in `/etc/pam.d/common-auth` (Ubuntu's
`pam-auth-update`), which `sudo`, `polkit-1` and the KDE lock screen include
with `[success=2 default=ignore]`. A spurious driver success there *skips*
`pam_unix` and authenticates. The same bogus match that opened a locked app can
grant `sudo` or dismiss the lock screen. Nothing here can fix that: disable the
fingerprint profile system-wide (`sudo pam-auth-update`, untick Fingerprint)
until the driver is fixed.

### How availability is probed (asked, so recorded)
Re-checked on **every** auth run, never cached: `APPLOCKER_FP=1`, the PAM
service file exists, and `timeout 3 fprintd-list $USER` exits 0 with at least
one `- #<n>: <finger>` line. Therefore:
- **reader unplugged** → `fprintd-list` fails → tier skipped: no button, no
  wait. A hung fprintd is bounded by that `timeout 3`.
- **plugged in later** → the next unlock sees it, no restart needed.
- **connected but nothing enrolled** → still unavailable (no `- #` line).
- plugging in mid-run only affects the dialog's button, not the pre-prompt tier
  that has already run.

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

### Also fixed
- **The sudo-password release had no feedback window.** The broker's main
  `authenticate()` (Settings changes) always had one — my first note here said
  otherwise, which was wrong — but `get_sudo()`, the path the polkit agent uses
  to release your stored sudo password, ran its own face loop with no window at
  all. It now uses the same `FaceWithFeedback` + `ClosingPrompter` pair.

### Still silent (follow-up, noted in plan.md)
- The fingerprint tier shows nothing at all — it runs before any window exists,
  so a touch-to-unlock is instant and unconfirmed. Needs the window spawned
  before the reader tier, which means threading the handle into the `Fallback`
  trait.

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
