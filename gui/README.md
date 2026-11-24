# AppLocker GUI

Thin Python/GTK clients to the daemon. Per the portability rule (see the
top-level README), this is the **only** desktop-specific surface — the KDE port
reskins these in Qt and the daemon does not change.

## `settings.py` — the settings window

The window from the design sketch: manage locked apps and folders, add a face,
and set the auth policy (face / PIN / sudo / re-auth). A **thin client** — it
reads the daemon's world-readable config files and shells out to `applockerd` for
every change; it never decides security itself.

- **Theming:** built from native GTK widgets, so it follows the active **Mint-Y**
  theme (light/dark + accent) automatically — no hardcoded colours. (Same
  approach as your 11snap: let the theme flow through. 11snap extracts raw colours
  only because it custom-draws a canvas; a widget form doesn't need to.)
- **Tamper protection:** opening it runs `applockerd authorize` first — no auth,
  no window. Otherwise a student could just open it and remove the locks or enrol
  their own face.
- **Privilege:** reads are unprivileged; writes go through `pkexec applockerd …`
  (set `$APPLOCKER_NO_PKEXEC=1` with user-writable `APPLOCKER_*` paths for dev).

```bash
python3 gui/settings.py
```

Needs a running desktop session; the data layer (config/apps/folders/installed)
is verified against real daemon output.

## `auth_prompt.py` — the fallback auth prompt

Collects a PIN or the sudo password and writes the result to **stdout**. It makes
no security decision; the root daemon verifies whatever comes back (PIN against
the hash file, password against PAM). Keeping the secret on a pipe — never on
argv, never echoed on screen — is the whole point.

### Contract (the daemon depends on this — keep it stable)

Invocation:

```bash
auth_prompt.py --app "gnome-calculator" --methods pin,sudo [--error "hint"]
```

- `--methods` — comma list of offered fallbacks (`pin`, `sudo`); ≥1 always present.
- `--error` — red hint shown from the previous failed attempt.

Output — exactly one line on stdout, then exit:

```text
pin\t<secret>     # user chose PIN         (exit 0)
sudo\t<secret>    # user chose password    (exit 0)
cancel            # cancelled / closed     (exit 1)
```

Any other exit (nonzero with no line) is treated as a cancel.

### Requirements

`python3-gi` with GTK 3 (`gi.require_version("Gtk", "3.0")`). Present on a stock
Linux Mint / Cinnamon install.

### Try it standalone

```bash
python3 auth_prompt.py --app "Test App" --methods pin,sudo
# type something, hit Unlock → prints e.g.  pin<TAB>1234
```
