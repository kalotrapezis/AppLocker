# AppLocker — GTK4 / libadwaita port plan

**Status:** future work (not started). Tracked separately from `plan.md` because it's
a large, self-contained effort.

## Why now

We stayed on **GTK3** on purpose: the dev/target box was **Linux Mint on Xorg**, and
GTK4's Wayland-first assumptions (no server-side window stacking, `keep_above`
removed, layer-shell for overlays) made the lock-screen/overlay windows fragile on
X11. The next Mint release **moves to Wayland**, which removes that blocker — and the
current settings mockup is drawn for **GTK4 + libadwaita** anyway, so the port is
what makes the app look like the design instead of GTK3 + hand-rolled CSS.

## Goal

Port the **UI layer only** to GTK4 + libadwaita. The backend is untouched: the root
**broker** (`daemon/serve.rs`), `vault.py`, `hidelist.py`/`hide_watch.py`,
`watch_presence.py` state machine, `matcher.py`, and all the config I/O in
`gui/settings.py`'s module-level functions stay exactly as they are. This is a
view-layer rewrite, window by window.

## Target stack (decision)

- **GTK 4 + libadwaita 1** (`gir1.2-adw-1`). Min GTK 4.10 / Adw 1.4 (breakpoints,
  `Adw.NavigationSplitView` — both present on Ubuntu 24.04 / Mint's next base).
- **gtk4-layer-shell** (`gir1.2-gtk4layershell-0.1` / `libgtk4-layer-shell0`) for the
  always-on-top overlay windows on Wayland (see the lock-screen windows below).
- Packaging: add the above to the `.deb` `Depends`; drop `gir1.2-gtk-3.0` only after
  **every** window is ported (see Phase 5).

## Inventory — what actually has to change

| File | Lines | Toolkit today | Port? | Notes |
|---|---|---|---|---|
| `gui/settings.py` | ~1912 | GTK3 (+Gdk, CSS) | **yes — Phase 1** | biggest payoff; maps 1:1 to libadwaita |
| `gui/welcome.py` | ~494 | GTK3 | yes — Phase 2 | first-run flow, 1 modal dialog |
| `gui/enroll_window.py` | ~297 | GTK3 + GdkPixbuf | yes — Phase 2 | **live camera preview** → `Gtk.Picture`/`Gdk.Texture` |
| `gui/auth_prompt.py` | ~200 | GTK3 (+Gdk) | yes — Phase 3 | **lock-screen, safety-critical**, `keep_above` |
| `gui/unlock_feedback.py` | ~127 | GTK3 | yes — Phase 3 | undecorated top-level, `keep_above` |
| `face/watch_presence.py` `DimOverlay` | ~30 | GTK3 `POPUP` | yes — Phase 4 | fullscreen dim overlay; `Gdk.Screen`, `keep_above` |
| `gui/polkit_agent.py` | ~355 | **dbus + subprocess, no Gtk** | **no** | only spawns `auth_prompt`; nothing to port |

## Recurring GTK3 → GTK4 edits (the mechanical cheat-sheet)

These come up in every file — worth a shared pattern before starting:

- **App lifecycle:** `Gtk.main()` / `Gtk.main_quit` → `Gtk.Application` / `Adw.Application`
  with `app.run()` and `window.close()`. (1 per window; ~6 total.)
- **Visibility:** `show_all()` is gone — widgets are visible by default; use
  `set_visible(False)` to hide. (~13 in settings alone.)
- **Containers:** `container.add(child)` → `set_child(child)` for single-child widgets
  (Window, Frame, ScrolledWindow, ListBoxRow); `box.pack_start/​pack_end(w, exp, fill,
  pad)` → `box.append/​prepend(w)` + `w.set_hexpand/​set_vexpand/​set_halign`. (~105
  `pack_start`, ~50 `add` across the tree.)
- **Spacing:** `border_width` / `set_border_width` → `set_margin_top/​bottom/​start/​end`.
- **Modal dialogs — the big one:** `Gtk.Dialog.run()` is **removed** (no nested main
  loop). Every blocking `dlg.run()` becomes async: `Adw.MessageDialog` +
  `dialog.choose(..., callback)`, or a response-signal handler. ~10 in `settings.py`
  (`_ask_new_pin`, `_toast`, the confirms, the four ⚙ gear dialogs), 1 in `welcome`,
  1 in `enroll`. This is the largest behavioural change — the code currently reads
  return values inline; it must move to callbacks.
- **File pickers:** `Gtk.FileChooserDialog.run()` → `Gtk.FileDialog` (async) — used by
  the hidden-files "Add file/folder".
- **CSS:** `CssProvider.load_from_data(b"...")` takes a **`str`** in GTK4;
  `Gtk.StyleContext.add_provider_for_screen(Gdk.Screen…)` → `…_for_display(Gdk.Display…)`.
  (Most of our card/nav rounding becomes libadwaita's job — see below.)
- **Icons/buttons:** `Gtk.Button.new_from_icon_name(name, size)` → drop the size arg.
- **Screen geometry:** `Gdk.Screen.get_default().get_width/height` → `Gdk.Display` +
  `Gdk.Monitor.get_geometry()` (used by the presence `DimOverlay`).
- **Always-on-top:** `set_keep_above(True)` is gone in GTK4 — the overlay/lock windows
  must use **gtk4-layer-shell** (`LayerShell.set_layer(OVERLAY)`) on Wayland.

## libadwaita mapping for the settings mockup (the payoff)

The mockup was drawn for this — most of our hand-rolled sidebar+stack+CSS collapses
into native widgets that theme correctly and get the responsive/compact variant free:

- `Adw.ApplicationWindow` + **`Adw.NavigationSplitView`** → the sidebar + page stack
  (replaces `_build_sidebar` + `Gtk.Stack` + the `applocker-nav` CSS).
- **`Adw.PreferencesPage` / `Adw.PreferencesGroup`** → the four pages and the rounded
  cards (replaces `_card` + `_install_css` rounding — rounding is built in).
- **`Adw.ActionRow` / `Adw.SwitchRow` / `Adw.ComboRow` / `Adw.ExpanderRow`** → every
  "label + ⚙ + toggle" and "label + dropdown" row (replaces `_switch_row`,
  `_heading_row`, the manual re-ask/interval combos).
- **`Adw.ToastOverlay`** → replaces the modal `_toast` dialogs (non-blocking).
- **`Adw.MessageDialog`** → the confirmations (delete vault, forget sudo, change PIN).
- **`Adw.Breakpoint`** on the split view → the mockup's **Compact** variant (narrow
  window collapses the sidebar to a back-button flow) for free — the compact layout
  in the mockup JSON we skipped in the GTK3 build.
- The ⚙ gear dialogs → `Adw.PreferencesDialog`/`Adw.Dialog` (adaptive, async).

## Phased plan (do in order; each phase ships and is testable)

### [ ] Phase 0 — spike & scaffolding
- Add `gir1.2-adw-1` (+ layer-shell) to a dev env; write a throwaway 30-line
  `Adw.ApplicationWindow` and run it on **Mint-next / Wayland** to confirm the stack.
- Decide the shared app skeleton (one `Adw.Application` per entry point, or a small
  helper module `gui/adw_app.py`).
- Add the new deps to `packaging/build-deb.sh` `Depends` (keep GTK3 dep for now).

### [ ] Phase 1 — Settings (`settings.py`) — biggest win, lowest risk
- Rewrite the window with `Adw.NavigationSplitView` + `PreferencesPage`/`Group`/rows
  per the mapping above. Keep every module-level backend function and handler body.
- Convert the 4 gear dialogs + `_ask_new_pin` + confirms + `_toast` to async
  (`Adw.Dialog` / `Adw.MessageDialog` / `Adw.ToastOverlay`).
- Add the `Adw.Breakpoint` compact variant.
- **Not safety-critical** (opening settings needs auth but a bug here can't lock you
  out), so it's the right first target.

### [ ] Phase 2 — First-run flow (`welcome.py`, `enroll_window.py`)
- Port `welcome.py` (mostly boxes + 1 dialog).
- Port `enroll_window.py`; replace the per-frame `Gtk.Image.set_from_pixbuf` camera
  preview with `Gtk.Picture` + `Gdk.MemoryTexture` (numpy frame → texture). Verify the
  liveness head-turn prompts still update smoothly.

### [ ] Phase 3 — Lock-screen windows (`auth_prompt.py`, `unlock_feedback.py`) — CRITICAL
- Port last and **test in a VM snapshot first** — these run in the unlock path; a
  break here means "can't authenticate". Keep the GTK3 versions until the GTK4 ones
  are proven (the daemon can pick which to spawn behind an env flag during rollout).
- Use **gtk4-layer-shell** (`OVERLAY` layer, `set_keyboard_mode(EXCLUSIVE)`) to replace
  `set_keep_above` so the prompt sits above everything on Wayland.

### [ ] Phase 4 — Presence dim overlay (`watch_presence.py` `DimOverlay`)
- Replace the `Gtk.WindowType.POPUP` + `Gdk.Screen` overlay with a layer-shell
  fullscreen surface. Low risk — it's cosmetic; locking already works without it (this
  is the `temporary window without parent` Wayland warning seen in the 0.0.2 test).

### [ ] Phase 5 — Cleanup
- Remove all GTK3 code paths and the GTK3 fallback spawns.
- Drop `gir1.2-gtk-3.0` from the `.deb` `Depends`; bump min GTK4/Adw versions.
- CHANGELOG + a version bump; the sidebar `APP_VERSION` follows.

## Risks & test strategy

- **Lock-out risk** is concentrated in Phase 3. Never remove the GTK3 auth prompt
  until the GTK4 one is verified in a VM through a real PAM unlock. Gate the switch
  behind an env flag so a bad build falls back.
- **Async dialogs** are the main source of subtle regressions (return-value logic that
  moves to callbacks). Port the settings dialogs carefully and re-run the same manual
  checks we used for 0.0.2 (each gear dialog opens/edits/summarises; Apply/Revert).
- **Wayland-only assumptions**: layer-shell is Wayland-only. Since the whole reason for
  the port is Mint→Wayland, X11 support can be dropped — but confirm the target before
  removing the GTK3 fallback.
- Keep the "dev-safe: no persistence" rule — test the lock-screen/PAM pieces in a VM,
  never by editing the host's PAM/greeter.

## Definition of done

All six windows on GTK4 + libadwaita, matching the mockup (including the compact
breakpoint), running on Mint/Wayland; GTK3 dependency removed; lock-screen path
verified in a VM; `.deb` builds and installs clean.
