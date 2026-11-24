# pam_applocker — face login (LightDM, screensaver, sudo)

A PAM module that tries **face + head-turn liveness** first and falls through
to the normal password on any failure. Installed as `auth sufficient`, it can
*add* a way in but never remove one — a broken camera degrades to the password
prompt, nothing worse.

The recognizer is the same one the app-gate uses (`face/recognize.py`), run
with `APPLOCKER_FACE_LIVENESS=1`: you must complete the random turn-left/right
challenge, so a photo of you can't log in.

## Install (order matters — safest first)

Build and place the module and scripts:

```bash
cd pam && cargo build --release
sudo mkdir -p /usr/lib/applocker
sudo cp ../face/*.py /usr/lib/applocker/
sudo cp target/release/libpam_applocker.so /lib/x86_64-linux-gnu/security/pam_applocker.so
```

**0. Before touching PAM**, confirm the recognizer passes with liveness, as your user:

```bash
python3 face/recognize.py   # follow the turn prompts; must print `match`
```

**1. sudo first** (easiest to revert). Open a **root shell and keep it open**
(`sudo -s` in another terminal) so you can undo anything. Then add as the
FIRST auth line of `/etc/pam.d/sudo`:

```
auth sufficient pam_applocker.so
```

Test `sudo true` in a new terminal: camera challenge → success, or fall
through to the password. Remove the line from the root shell if anything is
weird.

**2. Screensaver**: same line, first in `/etc/pam.d/cinnamon-screensaver`.
Lock the screen and unlock. Your session is still alive if it fails — switch
to a TTY (Ctrl-Alt-F3) and remove the line if needed.

**3. LightDM last**: same line, first in `/etc/pam.d/lightdm`. Before
rebooting/logging out, **log a root shell into a TTY** (Ctrl-Alt-F3) so a bad
line can be removed without a rescue disk.

## Module options

```
auth sufficient pam_applocker.so script=/usr/lib/applocker/recognize.py timeout=20
```

- `script=` — recognizer path (default `/usr/lib/applocker/recognize.py`)
- `timeout=` — hard kill for the child, seconds (5–120, default 20)

## Behaviour details

- Only ever succeeds on the literal `match` from the recognizer; timeout,
  crash, missing enrollment, unknown/odd user names (and root) all return
  `PAM_AUTH_ERR` → next module.
- Looks for profiles in `/home/<user>/.config/applocker/faces/` (+ the legacy
  `owner.face`), models in `.../models` — the same layout the app-gate uses.
- Liveness config note: the turn challenge needs the YuNet model
  (`face_detection_yunet_2022mar.onnx`) — run `python3 face/fetch_models.py`.
