# AppLocker

Android-style app & folder locking for Linux Mint (Cinnamon), with face unlock
on a plain webcam. Lock chosen apps and folders behind a face scan, a PIN, or
your sudo password — checked *before* the app is allowed to launch.

Built for Linux Mint / Cinnamon first, with a planned port to Kubuntu / KDE.
The portability rule below is what makes that port cheap.

## Threat model (read this first)

This is **deliberately not high security**. The assumption is *casual local
access* — a partner, kid, colleague, or guest poking at an unlocked laptop — not
a determined attacker. Anyone with root, a live USB, or physical disk access
wins, and that is accepted. The goal is the Android lock-screen experience, not
disk encryption. For real at-rest protection, locked folders should later sit on
top of `gocryptfs`/`fscrypt`; AppLocker only gates *access while the system is
running*.

A plain RGB webcam (no IR) can be fooled by a photo. We raise that bar in
software with a liveness challenge (blink / head-turn), never claiming it is
spoof-proof.

## Architecture

Three runtime pieces plus two windows. The split exists so the **logic** lives
in DE-agnostic code and only the **look** is desktop-specific.

```
                    ┌──────────────────────────────────────┐
                    │            applockerd (Rust)          │  ← root, systemd
                    │                                       │
   execve ───────►  │  exec gate    (fanotify EXEC_PERM)    │
   file open ─────► │  file gate    (fanotify OPEN_PERM)    │
   logind Lock ───► │  unlock-cache (wipe on lock/reboot)   │
                    │  auth routine (face×3 → PIN → sudo)   │
                    └───────┬───────────────────┬───────────┘
                            │ D-Bus             │ spawns
                            ▼                   ▼
                  ┌──────────────────┐   ┌──────────────────┐
                  │  GUI (Python/GTK)│   │ face pipeline    │
                  │  - settings      │   │ (Python/OpenCV/  │
                  │  - enrollment    │   │  MediaPipe)      │
                  └──────────────────┘   └──────────────────┘
```

| Component | Language | Role |
|-----------|----------|------|
| `applockerd` | **Rust** | Privileged daemon: fanotify exec/file gate, unlock-cache, auth orchestration, D-Bus. Runs as root via systemd. |
| face pipeline | **Python** | Webcam capture, enrollment (multi-angle embeddings), recognition, liveness. |
| GUI | **Python + GTK** | Settings window + enrollment window. Thin client to the daemon. |

### The portability rule

Never call Cinnamon-, KWin-, or cinnamon-screensaver-specific APIs. Use only the
layers that are byte-for-byte identical on Cinnamon and KDE:

- **systemd-logind / `loginctl`** — lock the session, and subscribe to
  `Lock`/`Unlock`/prepare-for-shutdown signals. This is how apps re-lock when the
  PC is locked or rebooted.
- **PAM** — the "PIN or sudo password" fallback auth.
- **fanotify** — kernel API, fully DE-independent.
- **XDG `.desktop`** — enumerating installed apps for the `+` picker.
- **D-Bus** — GUI ↔ daemon IPC.

Porting to KDE = reskin the GUI in Qt. The daemon does not change.

## Two detection tiers (the attention feature)

| Tier | When | Question | Cost |
|------|------|----------|------|
| **Recognition** | at unlock / on return | "is this *me*?" (128-d embedding match) | heavy |
| **Presence** | continuously, while unlocked | "is *a* face still there?" | light |

While unlocked we only run the cheap presence tier. When presence is lost for a
configurable grace period (with frame debounce, so looking down doesn't lock
you), the daemon locks the session **and wipes the unlock cache**, so every
locked app needs auth again. If the attention feature is off, the cache is wiped
on session-lock and reboot instead.

### Auth routine (one routine, used everywhere)

Triggered both by launching a locked app and by returning after an
attention-lock:

```
try face  ─┐
try face   ├─ up to 3×, 0.5s apart  ── any match ─► ALLOW
try face  ─┘
   └─ all 3 fail ─► prompt PIN / sudo password ─► ALLOW / DENY
```

At least one fallback (PIN or sudo) is **always** enabled and cannot be turned
off — otherwise a failed camera locks you out of your own machine.

## Problems we expect down the line

- **Interpreted scripts** — `python secret.py` execs `/usr/bin/python`, not the
  script, so exec-gating sees the interpreter. Scripts must be gated via the
  file-gate, not the exec-gate.
- **fanotify mark scope** — `FAN_MARK_FILESYSTEM` covers one filesystem. A
  separate `/home` partition, flatpaks, snaps, and AppImages live elsewhere and
  need their own marks. Flatpak/snap apps also don't exec a simple binary path.
- **Camera contention** — the attention watcher holds the webcam, so video calls
  can't. Need a single camera-owner service and auto-pause when another app
  wants the camera.
- **Lock-out recovery** — bad light, beard, broken camera. Mandatory fallback +
  a documented recovery path (boot, stop the service).
- **The daemon waits on userspace** — every `execve` on the system pauses until
  `applockerd` answers. A hang in the daemon hangs the machine. Needs a fail-open
  watchdog and a hard timeout default.
- **Self-gating deadlock** — the daemon and its helpers must never be blocked by
  their own gate.
- **Settings tamper** — the settings window must itself require auth to change
  locks, or it's trivially bypassed.
- **Wayland** — Cinnamon is X11 today; KDE may be Wayland. Screen-lock via
  logind is fine, but any future overlay/grab work differs.

## Status

Steps 1–2 done (`daemon/` + `gui/`). The exec-gate blocks a launch before it
opens, and launching a locked app now runs the real **auth routine**: face(stub)
→ PIN / sudo-password prompt, allowing only on success and caching the unlock.

```bash
cd daemon
cargo build --release
cargo test                                    # 17 tests, no root/camera/display

# set a PIN, then test the whole prompt+PIN+PAM flow with no fanotify:
APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd set-pin
APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd auth-test firefox

# or gate a real app (root):
sudo APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd gnome-calculator
```

Face is an **opt-in convenience** — the secure, always-available default is
PIN and/or the sudo password, chosen with a live-editable policy:

```bash
applockerd set-face off        # PIN/sudo only, no camera in the loop (default)
applockerd set-face on         # try face first, fall back to PIN/sudo
applockerd set-fallback sudo   # e.g. sudo password only
applockerd config              # show current policy
```

At least one fallback is always enabled, so you can't lock yourself out. Step 3
(the face pipeline, `face/`) is in progress: the liveness challenge and the
matcher are built and self-tested; the OpenCV engine + camera glue await an
on-hardware run. Until face is enabled *and* enrolled, the daemon uses the
`NoFace` stub and goes straight to the fallback. See
[daemon/README.md](daemon/README.md) and [face/README.md](face/README.md).

## Roadmap

1. ✅ Exec-gate spike — block one app.
2. ✅ Auth routine + a real GTK auth prompt (PIN + sudo via PAM).
   Persistent locked-apps list from installed `.desktop` files
   (`lock-app`/`unlock-app`/`list-installed`), live-reloaded on SIGHUP — replaces
   the substring placeholder. Unwraps `sh -c` launchers and refuses to gate a
   bare shell/interpreter. Flatpak/Snap stored but not yet enforced.
3. 🚧 Face pipeline on the webcam (enrollment + match + **liveness required**) —
   `face/`. Pure-logic core (liveness state machine, matcher) is built and
   self-tested; the OpenCV engine + camera glue need on-hardware run after
   `sudo apt install python3-opencv python3-numpy opencv-data`. Wires into the
   daemon via the `FaceVerifier` seam (opt-in: `APPLOCKER_FACE=1`).
4. ✅ File-gate for locked folders (the "files & folders" half of the fence) —
   `folderlist.rs`. `lock-folder`/`unlock-folder`/`list-folders`; gates
   `FAN_OPEN_PERM` on opens under a locked folder via the same async auth path,
   canonical-prefix match, refuses system roots, enabled only when folders are
   locked. Same self-gating/perf caveats as the app-gate (documented).
5. 🚧 Attention watcher + unlock-cache wipe + logind lock integration. Presence
   tier (low-sensitivity "is *a* face there?") dims the screen after ~3s away and
   locks after ~10s; returning cancels. The watcher just calls
   `loginctl lock-session`; the daemon reacts to logind's `Lock` signal by wiping
   the unlock cache — so manual lock, lid-close, and attention-lock all re-lock
   apps through one DE-neutral path. Re-auth is `once per session` (until a lock)
   or `every launch`, per `set-reauth`. The attention state machine
   (`face/attention.py`) and the cache policy (`gate::CachePolicy`) are built and
   self-tested; the camera watcher, screen-dim overlay, and logind subscription
   need on-hardware wiring. **Open problem:** the watcher holds the webcam, so it
   must auto-pause when a video call wants the camera.
6. 🚧 Settings & enrollment windows. `gui/settings.py` (GTK3, Mint-Y-themed,
   auth-gated on open) manages locked apps/folders and the auth policy as a thin
   client over the `applockerd` CLI (`--porcelain` for machine-readable lists).
   Data layer verified; interactive display + `pkexec` privilege wiring need a
   desktop session. D-Bus transport (replacing the CLI shell-out) is later.
7. **Face-at-login**: a `pam_applocker.so` PAM module (LightDM + screen-unlock +
   sudo), on top of the working face pipeline. Then packaging (`systemd` unit,
   installer) and the KDE/Qt GUI port.
