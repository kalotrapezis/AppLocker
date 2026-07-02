# Packaging AppLocker as a .deb

Builds a single `applocker_<ver>_<arch>.deb` that installs the flat runtime
layout the code expects.

## Build

```bash
packaging/build-deb.sh        # → dist/applocker_0.1.0_amd64.deb
```

Needs `cargo` and `dpkg-deb` (no root — uses `--root-owner-group`). Compiles the
daemon and PAM module in release mode, then stages:

| Path | What |
|------|------|
| `/usr/lib/applocker/applockerd` | the daemon (gate + CLI) |
| `/usr/lib/applocker/*.py` | all face + GUI scripts, flattened |
| `/usr/bin/applockerd` | symlink → the daemon (on PATH for `pkexec`) |
| `/usr/bin/applocker-pam` | safe PAM enable/disable helper |
| `/lib/<triplet>/security/pam_applocker.so` | the PAM module |
| `/lib/systemd/system/applockerd.service` | gate unit (**installed disabled**) |
| `/etc/xdg/autostart/applocker-watcher.desktop` | presence watcher on login |
| `/usr/share/applications/applocker-settings.desktop` | menu entry for the GUI |

## Install / remove

```bash
sudo apt install ./dist/applocker_0.1.0_amd64.deb   # pulls deps
sudo apt remove applocker                            # or `purge` to drop /etc/applocker
```

## After install (nothing is enforced until you opt in)

1. **Face models + enrollment** (per user, needs network once):
   ```bash
   python3 /usr/lib/applocker/fetch_models.py
   python3 /usr/lib/applocker/enroll.py --name me
   ```
2. **Lock apps/folders** and run the gate in your session:
   ```bash
   sudo applockerd lock-app "Calculator"
   sudo applockerd            # needs your DISPLAY, so run it in your session
   ```
3. **Presence watcher** — enable it in the AppLocker settings GUI (or
   `sudo applockerd set-attention on`); the autostart entry launches it on
   later logins.
4. **Face login** (optional, reversible):
   ```bash
   sudo applocker-pam enable sudo          # test in a new terminal…
   sudo applocker-pam enable screensaver   # …then the screensaver…
   sudo applocker-pam enable lightdm       # …then login (keep a root TTY open!)
   sudo applocker-pam status               # see what's active
   sudo applocker-pam disable all          # undo everything
   ```

## Known caveats

- **`applockerd.service` ships disabled.** As a bare system service the gate has
  no user `DISPLAY`, so its auth prompt can't render yet (a known gap — see the
  top-level README). Run the gate inside your session for now.
- **Models aren't shipped** (git-LFS / network) — `fetch_models.py` pulls them.
- **`.face` enrollment data is per-user** under `~/.config/applocker/` and is
  never packaged.
