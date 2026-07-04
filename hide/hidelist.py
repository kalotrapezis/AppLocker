#!/usr/bin/env python3
"""AppLocker hidden files & folders — the light, freeze-proof hide-in-place tier.

This is the *weaker, convenient* sibling of the encrypted Private folder (see
vault/vault.py). It does **not** move, mount, or encrypt anything — trying to
encrypt a normal folder (e.g. Documents) once wedged the whole machine, so we
never go near that again. All this does is add or remove a name from the
directory's `.hidden` file:

    Dolphin, Nautilus & other file managers read a `.hidden` file in a folder —
    each line is the name of an entry in *that* folder to omit from the listing.

So to hide `~/Videos/private/` we add the line `private` to `~/Videos/.hidden`;
to reveal it we take that line back out. The file itself never moves and stays
fully readable from a terminal — this only tucks it out of sight in the file
manager. That's the whole mechanism, on purpose.

Model:
  * A registry of the entries you put under AppLocker's control lives in
    `~/.config/applocker/hidden.json` (just a list of absolute paths). It's how
    we know which items to re-hide after a reveal — the `.hidden` files
    themselves remain the source of truth for what's hidden *right now*.
  * `hide` / `reveal` flip one entry; `hide-all` / `reveal-all` flip every
    registered entry at once (reveal-all is what the GUI calls after a face/PIN
    check; hide-all re-tucks them when you walk away or lock).

No root, no daemon, no FUSE, nothing persistent beyond a small JSON list and the
`.hidden` files the file manager already understands.

CLI:
  hidelist.py add <path>        # manage <path> and hide it now
  hidelist.py forget <path>     # reveal it and stop managing it
  hidelist.py hide <path>       # hide one managed entry
  hidelist.py reveal <path>     # reveal one managed entry
  hidelist.py toggle <path>     # flip one managed entry
  hidelist.py hide-all          # hide every managed entry (walk-away / lock)
  hidelist.py reveal-all        # reveal every managed entry (after auth)
  hidelist.py list              # managed entries + hidden/shown state
  hidelist.py status <path>     # hidden / shown for one path
  hidelist.py --selftest        # offline logic checks (uses a temp dir)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


# ── locations ────────────────────────────────────────────────────────────────

def config_home() -> str:
    return os.environ.get(
        "APPLOCKER_CONFIG_HOME", os.path.expanduser("~/.config/applocker"))


def registry_path() -> str:
    return os.environ.get(
        "APPLOCKER_HIDDEN_REGISTRY", os.path.join(config_home(), "hidden.json"))


def _abspath(path: str) -> str:
    """Absolute, ~-expanded path WITHOUT resolving symlinks — we hide the name
    the user picked in the folder they picked, not a symlink's target elsewhere."""
    return os.path.abspath(os.path.expanduser(path))


# ── safety ───────────────────────────────────────────────────────────────────
# Hiding is cosmetic, but a stray `.hidden` line in a system dir is still noise
# we don't want to write. Keep it to the user's own files, and never touch
# AppLocker's own config or the encrypted-vault machinery.

def is_safe(path: str) -> tuple[bool, str]:
    p = _abspath(path)
    home = _abspath("~")
    if p == home or p == "/":
        return False, "refusing to hide your home root or /"
    if not p.startswith(home + os.sep):
        return False, "only files inside your home directory can be hidden"
    if os.path.basename(p) == ".hidden":
        return False, "the .hidden file itself can't be hidden"
    cfg = _abspath(config_home())
    if p == cfg or p.startswith(cfg + os.sep):
        return False, "AppLocker's own config can't be hidden"
    return True, ""


# ── the `.hidden` file (one per containing directory) ─────────────────────────

def hidden_file_for(path: str) -> str:
    """The `.hidden` that governs `path` — it lives in `path`'s *parent* dir and
    lists names within that dir (including `path`'s own basename)."""
    return os.path.join(os.path.dirname(_abspath(path)), ".hidden")


def _read_names(hf: str) -> list[str]:
    try:
        with open(hf) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


def _write_names(hf: str, names: list[str]) -> None:
    # Drop an empty .hidden rather than leave a stray file behind.
    try:
        if names:
            tmp = hf + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(names) + "\n")
            os.replace(tmp, hf)
        elif os.path.exists(hf):
            os.remove(hf)
    except OSError as e:
        print(f"hidelist: couldn't update {hf}: {e}", file=sys.stderr)


def is_hidden(path: str) -> bool:
    name = os.path.basename(_abspath(path))
    return name in _read_names(hidden_file_for(path))


def set_hidden(path: str, hide: bool) -> bool:
    """Add (hide) or remove (reveal) this path's basename in its parent `.hidden`.
    Returns True if the on-disk state now matches `hide`. Idempotent."""
    name = os.path.basename(_abspath(path))
    hf = hidden_file_for(path)
    names = _read_names(hf)
    present = name in names
    if hide and not present:
        names.append(name)
    elif not hide and present:
        names = [n for n in names if n != name]
    else:
        return True  # already in the wanted state
    _write_names(hf, names)
    return is_hidden(path) == hide


# ── registry (which entries AppLocker manages) ───────────────────────────────

def load_registry() -> list[str]:
    try:
        with open(registry_path()) as f:
            data = json.load(f)
        entries = data.get("entries", []) if isinstance(data, dict) else data
        # De-dupe while preserving order; keep only well-formed strings.
        seen, out = set(), []
        for e in entries:
            if isinstance(e, str):
                a = _abspath(e)
                if a not in seen:
                    seen.add(a)
                    out.append(a)
        return out
    except (OSError, ValueError, TypeError):
        return []


def save_registry(entries: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(registry_path()), exist_ok=True)
        tmp = registry_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"entries": entries}, f, indent=2)
        os.replace(tmp, registry_path())
    except OSError as e:
        print(f"hidelist: couldn't save the registry: {e}", file=sys.stderr)


def register(path: str) -> None:
    entries = load_registry()
    a = _abspath(path)
    if a not in entries:
        entries.append(a)
        save_registry(entries)


def unregister(path: str) -> None:
    a = _abspath(path)
    entries = [e for e in load_registry() if e != a]
    save_registry(entries)


# ── operations ───────────────────────────────────────────────────────────────

def cmd_add(args) -> int:
    path = _abspath(args.path)
    ok, why = is_safe(path)
    if not ok:
        print(f"hidelist: {why}", file=sys.stderr)
        return 1
    if not os.path.exists(path):
        print(f"hidelist: {path} doesn't exist.", file=sys.stderr)
        return 1
    register(path)
    set_hidden(path, True)
    print(f"managing and hiding: {path}")
    return 0


def cmd_forget(args) -> int:
    path = _abspath(args.path)
    set_hidden(path, False)  # always leave it visible when we stop managing it
    unregister(path)
    print(f"revealed and no longer managing: {path}")
    return 0


def _require_managed(path: str) -> bool:
    if _abspath(path) not in load_registry():
        print(f"hidelist: {_abspath(path)} isn't managed (add it first).",
              file=sys.stderr)
        return False
    return True


def cmd_hide(args) -> int:
    if not _require_managed(args.path):
        return 1
    ok = set_hidden(args.path, True)
    print(f"hidden: {_abspath(args.path)}")
    return 0 if ok else 1


def cmd_reveal(args) -> int:
    if not _require_managed(args.path):
        return 1
    ok = set_hidden(args.path, False)
    print(f"revealed: {_abspath(args.path)}")
    return 0 if ok else 1


def cmd_toggle(args) -> int:
    if not _require_managed(args.path):
        return 1
    now_hidden = not is_hidden(args.path)
    set_hidden(args.path, now_hidden)
    print(f"{'hidden' if now_hidden else 'revealed'}: {_abspath(args.path)}")
    return 0


def _apply_all(hide: bool) -> int:
    entries = load_registry()
    if not entries:
        print("(nothing managed yet — add one with: hidelist.py add <path>)")
        return 0
    failed = 0
    for e in entries:
        if not set_hidden(e, hide):
            failed += 1
    verb = "hidden" if hide else "revealed"
    print(f"{verb} {len(entries) - failed}/{len(entries)} managed entr"
          f"{'y' if len(entries) == 1 else 'ies'}.")
    return 1 if failed else 0


def cmd_hide_all(args) -> int:
    return _apply_all(True)


def cmd_reveal_all(args) -> int:
    return _apply_all(False)


def cmd_list(args) -> int:
    entries = load_registry()
    if not entries:
        print("(nothing managed yet — add one with: hidelist.py add <path>)")
        return 0
    for e in entries:
        state = "hidden " if is_hidden(e) else "shown  "
        gone = "" if os.path.exists(e) else "  (missing on disk)"
        print(f"{state} {e}{gone}")
    return 0


def cmd_status(args) -> int:
    print("hidden" if is_hidden(args.path) else "shown")
    return 0


# ── offline self-test (real temp dirs, no file manager needed) ────────────────

def _selftest() -> int:
    import tempfile

    fails = []
    with tempfile.TemporaryDirectory() as home:
        os.environ["APPLOCKER_CONFIG_HOME"] = os.path.join(home, ".config/applocker")
        os.environ["APPLOCKER_HIDDEN_REGISTRY"] = os.path.join(home, "reg.json")
        # Pretend $HOME is the temp dir so is_safe() accepts paths under it.
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home

        try:
            folder = os.path.join(home, "Videos", "private")
            os.makedirs(folder)
            afile = os.path.join(home, "Docs")
            os.makedirs(afile)
            secret = os.path.join(afile, "secret.pdf")
            open(secret, "w").close()

            # add → registered + hidden, and the .hidden line is correct
            cmd_add(argparse.Namespace(path=folder))
            if not is_hidden(folder):
                fails.append("add didn't hide the folder")
            if _read_names(os.path.join(home, "Videos", ".hidden")) != ["private"]:
                fails.append(".hidden content wrong after add")
            if _abspath(folder) not in load_registry():
                fails.append("add didn't register")

            # reveal / hide round-trip
            cmd_add(argparse.Namespace(path=secret))
            set_hidden(secret, False)
            if is_hidden(secret):
                fails.append("reveal left it hidden")
            if os.path.exists(os.path.join(afile, ".hidden")):
                fails.append("emptied .hidden not removed")
            set_hidden(secret, True)
            if not is_hidden(secret):
                fails.append("re-hide failed")

            # hide-all / reveal-all across both entries
            _apply_all(False)
            if is_hidden(folder) or is_hidden(secret):
                fails.append("reveal-all left something hidden")
            _apply_all(True)
            if not (is_hidden(folder) and is_hidden(secret)):
                fails.append("hide-all missed something")

            # the file never moved (hide is cosmetic only)
            if not os.path.exists(secret):
                fails.append("hiding moved/removed the file (must not!)")

            # forget → revealed + unregistered
            cmd_forget(argparse.Namespace(path=folder))
            if is_hidden(folder) or _abspath(folder) in load_registry():
                fails.append("forget didn't fully release the folder")

            # safety refusals
            for bad in (home, "~", os.path.join(home, ".config/applocker/x"),
                        "/etc/passwd"):
                if is_safe(bad)[0]:
                    fails.append(f"is_safe allowed {bad}")
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home

    if fails:
        print("hidelist self-test FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("hidelist self-test: all checks passed")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    p = argparse.ArgumentParser(
        prog="hidelist.py", description="AppLocker hide-in-place (.hidden) manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, help_ in (
        ("add", cmd_add, "manage a path and hide it now"),
        ("forget", cmd_forget, "reveal it and stop managing it"),
        ("hide", cmd_hide, "hide one managed entry"),
        ("reveal", cmd_reveal, "reveal one managed entry"),
        ("toggle", cmd_toggle, "flip one managed entry"),
        ("status", cmd_status, "hidden/shown for one path"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("path")
        s.set_defaults(fn=fn)

    for name, fn, help_ in (
        ("hide-all", cmd_hide_all, "hide every managed entry (walk-away / lock)"),
        ("reveal-all", cmd_reveal_all, "reveal every managed entry (after auth)"),
        ("list", cmd_list, "managed entries + state"),
    ):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
