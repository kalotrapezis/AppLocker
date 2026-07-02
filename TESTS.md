# AppLocker — manual test checklist

A run-through to catch problems on real hardware. Tick each box; if one fails,
note what happened next to it. Ordered **safest first** — the PAM tiers at the
end can lock you out, so they have their own escape hatches.

Conventions:
- `applockerd` = the daemon binary (`daemon/target/release/applockerd`, or
  `/usr/lib/applocker/applockerd` once packaged). Config-changing subcommands
  need root: prefix with `sudo` (or `pkexec` from the GUI).
- Use a scratch config to avoid touching the real one while experimenting:
  `export APPLOCKER_CONFIG=/tmp/applocker-test/config` (the daemon creates it).
- "New terminal" matters for PAM tests — an open shell keeps its old auth.

---

## 0. Build & unit tests (no hardware)

- [ ] `cd daemon && cargo test` → all green (49 tests, no root/camera/display).
- [ ] `cd daemon && cargo build --release` → builds clean.
- [ ] `python3 face/attention.py --selftest` → "all checks passed".
- [ ] `python3 face/liveness.py --selftest` → passes.
- [ ] `python3 face/matcher.py --selftest` → passes.

## 1. Environment probe

- [ ] `python3 face/probe_env.py` → reports OpenCV version, cascades found,
      camera `/dev/video0` grabs a frame, and whether YuNet/SFace APIs exist.
- [ ] `ls ~/.config/applocker/models/` → the two ONNX files present. If not:
      `python3 face/fetch_models.py` (needs network) → downloads them.

## 2. Face enrollment & recognition

- [ ] `python3 face/enroll.py --name me` → captures ~8 samples, writes
      `~/.config/applocker/faces/me.face`. Window shows guided capture.
- [ ] `python3 face/recognize.py --no-liveness` → prints `match` when it's you,
      `no-match` for someone else / an empty frame.
- [ ] `python3 face/recognize.py` (liveness on) → follow the "slowly turn your
      head side to side" prompt; completes ✓ 2/2 then `match`.
- [ ] `python3 face/recognize.py --ui` → the guided window shows preview, chip,
      ticks and final verdict.
- [ ] **Spoof check:** hold up a *photo* of your face to `recognize.py` (with
      liveness) → must **not** match (the turn challenge fails on a flat photo).

## 2b. Session discovery + turning it on

The gate runs as **root** but shows its prompt in *your* session. Confirm the
daemon can find your session before relying on the service.

- [ ] `sudo applockerd session-probe` → prints your user, home, `DISPLAY`,
      `XAUTHORITY`, `XDG_RUNTIME_DIR`. If it says "no active graphical session",
      the prompt won't show — stop and note it.
- [ ] `sudo applocker on` → enables the gate service + sudo + screensaver PAM;
      `applocker status` shows `gate service: active`.
- [ ] `sudo applocker off` → stops the service and removes the PAM hooks;
      `applocker status` shows it inactive.

## 3. App gate (lock an app)

> With the service enabled (`applocker on`) the gate is always running. To test
> without the service, run it in your session: `sudo applockerd`.

- [ ] `sudo applockerd list-installed` → lists installed `.desktop` apps.
- [ ] `sudo applockerd lock-app gnome-calculator` (or another safe app) →
      confirms locked; `applockerd list-apps` shows it.
- [ ] Start the daemon gate: `sudo applockerd` (or the systemd unit). Launch the
      locked app from the menu → auth prompt appears **before** it opens.
- [ ] Correct PIN / sudo password (and/or face) → app launches.
- [ ] Cancel / wrong secret → app does **not** launch.
- [ ] Re-launch within the same session with `reauth = session` → no second
      prompt (cached). With `reauth = always` → prompts every time.
- [ ] `sudo applockerd unlock-app gnome-calculator` → launches freely again.
- [ ] **SIGHUP live-reload:** with the gate running, `lock-app` another app →
      it becomes gated without restarting the daemon.

### 3a. Flatpak gate

Flatpaks are matched by their install path (`…/flatpak/app/<app-id>/…`), so a
lock catches the real app binary even though the launcher runs `flatpak`.

- [ ] `sudo applockerd lock-app "Flatseal"` (or any installed flatpak) → locks
      with **no** "not gateable" note; `list-apps` shows it without a caveat.
- [ ] With the gate running, launch the flatpak from the menu → auth prompt
      appears before its window opens. Correct secret → it opens; cancel → it
      doesn't.
- [ ] Launch the same flatpak from a terminal (`flatpak run <app-id>`) → also
      gated (the path match doesn't care how it was started).
- [ ] A *different*, unlocked flatpak launches freely (no false prompt).
- [ ] `sudo applockerd unlock-app "Flatseal"` → launches freely again.

> If a flatpak launch is **not** intercepted, note it — it means the sandboxed
> exec event isn't reaching the gate on this kernel, which we'd handle
> differently. (Snap apps are stored but not gated yet — expected.)

## 4. File / folder gate

> ⚠️ Never lock a folder that contains the repo, the daemon, `~/.config/applocker`,
> or a system dir — `is_safe_to_lock` refuses these, but double-check the target.

- [ ] Make a throwaway dir: `mkdir ~/locktest && echo hi > ~/locktest/f.txt`.
- [ ] `sudo applockerd lock-folder ~/locktest` → confirms; `list-folders` shows it.
- [ ] Open `~/locktest/f.txt` in a file manager / `xdg-open` → auth prompt first.
- [ ] Correct secret → opens. Cancel → stays closed.
- [ ] `sudo applockerd unlock-folder ~/locktest` → opens freely again.
- [ ] Try `sudo applockerd lock-folder /etc` → **refused** (unsafe).

## 5. Presence watcher (lock when you leave)

Config: `sudo applockerd set-attention on`. Start it in your session:
`python3 face/watch_presence.py --force` (watch its stderr line for the idle
backend and cadence). **The camera light should be OFF except during a check.**

- [ ] While you type/move the mouse → camera stays off, no dim, no lock.
- [ ] Stop touching the PC. After `--idle-after` (60s default) it takes a
      snapshot (brief camera light). With you present → nothing happens; it
      re-checks every interval.
- [ ] **Walk away.** First empty snapshot → screen dims (translucent warning).
      Move the mouse → dim clears immediately (activity cancels it).
- [ ] Walk away and stay gone → after `--misses` (2) empty snapshots in a row,
      the session **locks** (`loginctl lock-session`).
- [ ] After locking, unlock the session → watcher resumes cleanly.
- [ ] **Look-down debounce:** glance down at the keyboard during a check → a
      single miss only dims; it does not lock on one bad frame.
- [ ] **Camera-busy fail-safe:** join a video call (camera in use), then go idle
      → watcher reports blind and **never locks** you while the camera is busy.

### 5a. Check interval setting

- [ ] `sudo applockerd set-attention-interval 5` → config shows
      `attention_interval = 5`. With the watcher running, the next gap between
      checks becomes ~5 min (no restart needed — it re-reads live).
- [ ] Settings GUI "Check every" dropdown offers 2 / 5 / 10 / 15 / 30 and
      changes the same value.
- [ ] Hand-edit an invalid value (e.g. `attention_interval = 7`) → daemon/GUI
      snaps it back to the default (2).

### 5b. AC-only setting (laptop)

- [ ] `sudo applockerd set-attention-ac-only on`. On **battery**, the watcher
      parks: camera stays off, no checks, no lock (stderr shows it's idle).
- [ ] Plug in → within a few seconds it resumes checking.
- [ ] Settings GUI "Only when plugged in" switch toggles the same key.
- [ ] On a **desktop** (no battery) with AC-only on → treated as always powered,
      watcher keeps working (not disabled).

### 5c. Live on/off

- [ ] With the watcher running, `sudo applockerd set-attention off` (or the GUI
      switch) → the watcher process **exits** on its next tick.

## 6. Settings GUI

- [ ] `python3 gui/settings.py` (dev: `APPLOCKER_NO_PKEXEC=1`) → opens, gated by
      an auth prompt.
- [ ] Theme matches Mint-Y (dark/light + accent) — native GTK widgets.
- [ ] Face / PIN / sudo / re-auth / attention controls reflect the config and
      writing them updates `/etc/applocker/config`.
- [ ] Fallback invariant: turning off both PIN and sudo bounces one back on with
      a toast.
- [ ] Add / delete face profiles (up to 5) works; list shows names + counts.

## 7. sudo PAM tier  ⚠️ LOCKOUT RISK

Escape hatch: **keep a root shell open** (`sudo -s` in another terminal) the
whole time, so you can undo the PAM edit.

The `applocker-pam` helper (installed by the .deb) does the edits safely —
backs each file up to `<file>.applocker.bak`, inserts our rule as the first auth
rule, and `disable` removes only our line. From the repo without the package,
see [pam/README.md](pam/README.md) for the manual edit.

- [ ] `python3 face/recognize.py` passes with liveness first (step 2).
- [ ] `sudo applocker-pam enable sudo` → prints "enabled" + backup path.
      `sudo applocker-pam status` shows `sudo ENABLED`.
- [ ] In a **new** terminal, `sudo true` → face challenge; on success no
      password; on failure/timeout → falls through to the password prompt.
- [ ] Wrong face / photo → falls through to password (never a hard deny).
- [ ] `sudo applocker-pam disable sudo` → `sudo` back to password-only.

## 8. Screensaver PAM tier  ⚠️

- [ ] `sudo applocker-pam enable screensaver`.
- [ ] Lock the screen, unlock → face challenge or password fallback.
- [ ] If it misbehaves, switch to a TTY (Ctrl-Alt-F3) and
      `sudo applocker-pam disable screensaver` — your session is still alive.

## 9. LightDM login PAM tier  ⚠️ HIGHEST RISK

- [ ] **Before** enabling, log a root shell into a TTY (Ctrl-Alt-F3) so a bad
      edit can be undone without a rescue disk.
- [ ] `sudo applocker-pam enable lightdm`.
- [ ] Log out → at the greeter, face challenge unlocks, or the password still
      works as fallback.
- [ ] Break-glass rehearsal: from the TTY root shell,
      `sudo applocker-pam disable lightdm` and confirm normal login returns.

## 10. Packaging (.deb)

- [ ] `packaging/build-deb.sh` → `dist/applocker_<ver>_<arch>.deb`, no errors.
- [ ] `sudo apt install ./dist/applocker_*.deb` → installs, pulls deps.
- [ ] `which applockerd` → `/usr/bin/applockerd` (symlink resolves); `applockerd
      config` runs.
- [ ] AppLocker appears in the applications menu; launching it opens the GUI
      (auth-gated).
- [ ] Log out/in → the presence watcher autostarts (only acts if attention is on).
- [ ] `sudo apt remove applocker` → clean removal; if any PAM tier was still
      enabled, the postrm warns to run `applocker-pam disable all` first.

---

## Not yet implemented (should currently NOT work / be absent)

- [ ] **Snap gating** — locking a snap app is stored but not enforced yet.
      Expected: no gate, with a note. (flatpak now works — see 3a)
- [ ] **Adaptive brightness** — no brightness control yet. (task #5, bonus)
