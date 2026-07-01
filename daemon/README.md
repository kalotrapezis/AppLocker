# applockerd — steps 1–2: exec-gate + auth routine

The privileged Rust daemon. Step 1 proved AppLocker's riskiest assumption — that
we can **deny an `execve` before the program runs**, system-wide, via fanotify
`FAN_OPEN_EXEC_PERM`. Step 2 turns that raw DENY into the real **auth routine**:
launching a locked app now runs face(stub) → PIN / sudo-password prompt, and
only then allows or denies.

## Layout

| File | Role |
|------|------|
| `src/main.rs` | binary: fanotify event loop, the async gate, subcommand dispatch (`set-pin`, `auth-test`) |
| `src/lib.rs` | the DE-agnostic, unit-tested library below |
| `src/crypto.rs` | std-only SHA-256 / HMAC-SHA256 / PBKDF2 (no crates.io access), pinned to RFC test vectors |
| `src/pin.rs` | the PIN fallback: salted PBKDF2 hash file (`0600`), set + verify |
| `src/pam.rs` | the sudo-password fallback: libpam loaded at runtime via `dlopen` |
| `src/auth.rs` | the auth routine itself, written against tiny traits so it unit-tests with fakes |
| `src/gate.rs` | GUI-spawning prompter + the in-memory unlock cache |

Everything reusable lives in the library and is tested with **`cargo test`** — no
root, camera, or display required (17 tests, incl. crypto known-answer vectors
and the full auth-routine decision table).

```bash
cargo build --release
cargo test
```

## Choosing your auth mode (policy)

Face is a *convenience* you opt into; **PIN and/or the sudo password are the
secure default** and always work. The policy lives in `/etc/applocker/config`
(override with `$APPLOCKER_CONFIG`) and can be changed live — no daemon restart:

```bash
applockerd config                 # show the current policy
applockerd set-face off           # PIN/sudo only (the default) — no camera
applockerd set-face on            # try face first, then fall back to PIN/sudo
applockerd set-fallback both      # offer PIN and sudo password  (default)
applockerd set-fallback sudo      # sudo password only
applockerd set-fallback pin       # PIN only
```

At least one fallback is **always** enabled — you can't lock yourself out by
disabling everything, even with face on. Turning face off means a broken camera
or bad lighting is never in the loop. `set-face on` still needs enrollment
(`face/enroll.py`) before face actually runs; until then the routine quietly uses
PIN/sudo. `$APPLOCKER_FACE=1|0` overrides the policy for a one-off test.

## Try it

### 1. Set a PIN (no root needed if you override the path)

```bash
# real path (/etc/applocker/pin) needs root:
sudo ./target/release/applockerd set-pin
# or test to a scratch file:
APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd set-pin
```

### 2. Test the whole auth routine without fanotify

`auth-test` runs face(stub) → prompt → PIN/PAM once and prints `ALLOWED` /
`DENIED`. This is the easy path — no root, no locked app, and it uses your own
login session so the GTK prompt and PAM both work:

```bash
APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd auth-test firefox
```

The GTK window offers **PIN** and **Password** (your sudo/login password). PIN is
checked against the hash file; the password is checked by the daemon via PAM
(service `sudo`) — never by the GUI, so a tampered prompt can't fake a pass.

### 3. Choose which apps to lock

The gate reads a persisted locked list (`/etc/applocker/locked-apps`, override
`$APPLOCKER_LOCKED_APPS`) built from your installed `.desktop` files:

```bash
applockerd list-installed         # every installed app + its match key
applockerd lock-app steam         # lock by name or desktop id
applockerd lock-app calculator
applockerd list-apps              # what's currently locked
applockerd unlock-app steam
```

`lock-app` resolves the app's real binary — it unwraps `sh -c` launchers (Steam's
`sh -c '… steam …'` becomes `steam`) and **refuses** to lock a bare shell or
interpreter (so you can't accidentally gate `/bin/sh`). Flatpak/Snap apps are
stored but not yet enforced (every flatpak execs `flatpak`, so a binary path
can't tell them apart — a later step).

### 4. Lock folders (the file-gate)

The other half of the fence: opening anything under a locked folder requires auth
(then it's cached until the next lock, like apps).

```bash
applockerd lock-folder ~/Documents    # by path; must be an existing directory
applockerd list-folders
applockerd unlock-folder Documents    # by name or path
```

`lock-folder` stores the canonical path and **refuses system roots** (`/`, `/etc`,
`/usr`, …) — a file-gate there would prompt on nearly every process, including the
daemon's own reads. Matching is by canonical path prefix (component-wise, so
`~/Docs` never matches `~/Docs2`).

### 5. Run the gate (needs root)

```bash
sudo APPLOCKER_PIN_FILE=/tmp/applocker-pin ./target/release/applockerd
```

Launch a locked app (or open a file under a locked folder): instead of proceeding,
the auth prompt appears. Pass it and it's allowed and stays unlocked per your
`reauth` policy; fail/cancel and it's denied. `lock-*`/`unlock-*` send the running
daemon `SIGHUP` so both lists reload live — no restart. (`applockerd <name>` still
gates one ad-hoc app for the run.)

> **File-gate cost:** locking a folder turns on `FAN_OPEN_PERM` for the whole
> filesystem, so *every* file open is checked (matched inline and allowed unless
> under a locked folder). This can add latency; it's only enabled while at least
> one folder is locked. Interpreted-script and separate-filesystem caveats from
> the app-gate apply here too.

> **GUI-from-root caveat:** under plain `sudo` the daemon doesn't inherit your
> `DISPLAY`/`XAUTHORITY`, so the prompt may not appear in gate mode yet. Until
> the logind-session integration (step 5), export them for the daemon, e.g.
> `sudo -E ...` from your graphical session. `auth-test` (run as yourself) has no
> such issue and is the recommended way to see the prompt today.

## How the gate avoids deadlocking itself

Spawning `python3` for the prompt is *itself* an exec, so if the daemon blocked
its event loop waiting for the prompt it could never allow that python to
start — the classic self-gating deadlock. Instead, a locked exec is handled
**asynchronously**: the blocked event's fd is handed to a worker thread that runs
the (interactive, slow) auth routine, while the main loop keeps answering every
other exec — including the prompt's python. Each held event still gets exactly
one reply; the writes are serialised with a mutex.

## Caveats (still a spike)

- **Root only** for the gate (fanotify permission events need `CAP_SYS_ADMIN`).
- **Root filesystem only.** Apps on a separate `/home`, flatpaks, snaps, and
  AppImages live on other filesystems and need their own marks. Test with
  something in `/usr/bin`.
- **No timeout / fail-open watchdog yet.** A *hang* in the daemon would stall
  execs. Tracked in the top-level README.
- **Unlock cache is in-memory only** — wiped on daemon restart, not yet on
  session-lock/reboot (that's step 5, with logind + the attention watcher).
- **Substring match** is still a placeholder for the real locked-list lookup.

If launching the target blocks with no output, check `dmesg` and that the kernel
is ≥ 5.0 (yours is 6.17 — fine).
