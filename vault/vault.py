#!/usr/bin/env python3
"""AppLocker encrypted vaults — the freeze-proof way to lock folders.

Instead of gating file opens with fanotify (which makes AppLocker a mandatory
checkpoint in front of *all* disk I/O and can wedge the whole machine), a locked
folder is a **gocryptfs encrypted vault**:

  * **cipher dir** — encrypted files on disk (`~/.config/applocker/vaults/<id>/
    cipher`). This is all that persists; its contents are ciphertext.
  * **mount point** — the folder you actually use (e.g. ~/Private). When the
    vault is *locked* it's just an empty directory; when *unlocked* it's a FUSE
    mount showing the decrypted files.

  lock   = `fusermount3 -u <mount>`   → contents vanish for everyone (system,
           file manager, indexers) — there is simply nothing to read. No gate,
           nothing that can freeze.
  unlock = `gocryptfs <cipher> <mount>` → mounts the decrypted view.

gocryptfs is **user FUSE — no root needed**, so this whole feature is userspace:
no daemon, no PAM, no systemd, nothing persistent beyond your own encrypted data.

Threat model (matches README): this defeats a *casual* snooper at your unlocked
session — a locked vault shows nothing. It is NOT protection against someone with
root / disk access, who can read the key file and mount it themselves. The key is
kept in a mode-0600 file the app releases only after a face/PIN check; wrapping it
with the PIN so the file alone is useless is a planned hardening (see TODO).

CLI:
  vault.py create <mount-path> [--name N]   # make a new empty vault there
  vault.py unlock <id|path>                 # mount (decrypt)
  vault.py lock   <id|path>                 # unmount (encrypt/hide)
  vault.py status [<id|path>]               # locked / unlocked
  vault.py list                             # all known vaults
  vault.py destroy <id|path> [--force]      # remove vault + its ciphertext
  vault.py --selftest                       # offline logic checks (no gocryptfs)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys


# ── locations ────────────────────────────────────────────────────────────────

def config_home() -> str:
    return os.environ.get(
        "APPLOCKER_CONFIG_HOME",
        os.path.expanduser("~/.config/applocker"),
    )


def vaults_root() -> str:
    return os.path.join(config_home(), "vaults")


def registry_path() -> str:
    return os.path.join(vaults_root(), "registry.json")


def standard_path() -> str:
    """The one KDE-Vaults-style default vault: ~/Private (override with
    $APPLOCKER_PRIVATE_DIR). File locking is off until this is created."""
    return os.environ.get(
        "APPLOCKER_PRIVATE_DIR", os.path.join(os.path.expanduser("~"), "Private"))


# ── registry (which vaults exist) ────────────────────────────────────────────

def load_registry() -> dict:
    try:
        with open(registry_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    os.makedirs(vaults_root(), exist_ok=True)
    tmp = registry_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, registry_path())


def resolve(reg: dict, key: str) -> str | None:
    """Find a vault id from an id or a mount path."""
    if key in reg:
        return key
    want = os.path.realpath(os.path.expanduser(key))
    for vid, v in reg.items():
        if os.path.realpath(v["mount"]) == want:
            return vid
    return None


# ── safety ───────────────────────────────────────────────────────────────────

# Never let a vault mount point sit on a system location — a stray unmount or a
# botched mount there must not shadow the OS.
SYSTEM_PREFIXES = (
    "/", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/usr", "/etc", "/var",
    "/opt", "/proc", "/sys", "/dev", "/run", "/root",
)


def is_safe_mount(path: str) -> tuple[bool, str]:
    p = os.path.realpath(os.path.expanduser(path))
    if p in (os.path.realpath(os.path.expanduser("~")), "/"):
        return False, "refusing to use your home root or / as a vault"
    for sysp in SYSTEM_PREFIXES:
        if p == sysp:
            return False, f"refusing a system path ({sysp})"
    # Must live under the user's home (personal files only).
    home = os.path.realpath(os.path.expanduser("~"))
    if not (p == home or p.startswith(home + os.sep)):
        return False, "vault mount must be inside your home directory"
    return True, ""


# ── gocryptfs plumbing ───────────────────────────────────────────────────────

def _keyfile(vdir: str) -> str:
    return os.path.join(vdir, "key")


def _extpass(vdir: str) -> list[str]:
    # gocryptfs runs this and reads the password from its stdout.
    return ["-extpass", "cat", "-extpass", _keyfile(vdir)]


def _fusermount() -> str | None:
    return shutil.which("fusermount3") or shutil.which("fusermount")


def _init_vault_dir():
    """Create a fresh vault dir with a random key and an initialised gocryptfs
    cipher. Returns (vid, vdir, cipher). Raises RuntimeError with the reason on
    failure (having already cleaned up the half-made dir)."""
    vid = secrets.token_hex(8)
    vdir = os.path.join(vaults_root(), vid)
    cipher = os.path.join(vdir, "cipher")
    os.makedirs(cipher, exist_ok=True)
    # Random 32-byte key in a mode-0600 file; gocryptfs reads it via -extpass.
    kf = _keyfile(vdir)
    fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
    init = subprocess.run(
        ["gocryptfs", "-init", *_extpass(vdir), cipher],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if init.returncode != 0:
        shutil.rmtree(vdir, ignore_errors=True)
        raise RuntimeError("gocryptfs init failed:\n" + init.stdout)
    return vid, vdir, cipher


def is_mounted(mount: str) -> bool:
    """True if `mount` is currently a gocryptfs (fuse) mount point."""
    mp = os.path.realpath(mount)
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                # field 4 = mount point; fs type appears after the " - " separator
                if len(parts) > 4 and os.path.realpath(_unescape(parts[4])) == mp:
                    if "fuse" in line:
                        return True
    except OSError:
        pass
    return False


# ── hiding the folder from the file manager (`.hidden`) ──────────────────────
# Dolphin, Nautilus & others read a `.hidden` file in a directory: each line is a
# name in *that* directory to omit from the listing. To hide ~/Private we add
# "Private" to ~/.hidden. This is cosmetic (any dotfile-showing view still sees
# it), matching how KDE Vaults tucks a closed vault out of sight.

def _hidden_file(path: str) -> str:
    return os.path.join(os.path.dirname(os.path.realpath(os.path.expanduser(path))),
                        ".hidden")


def _set_listing_hidden(path: str, hide: bool) -> None:
    name = os.path.basename(os.path.realpath(os.path.expanduser(path)))
    hf = _hidden_file(path)
    try:
        with open(hf) as f:
            names = [ln.rstrip("\n") for ln in f]
    except OSError:
        names = []
    present = name in names
    if hide and not present:
        names.append(name)
    elif not hide and present:
        names = [n for n in names if n != name]
    else:
        return  # already in the wanted state
    try:
        if names:
            with open(hf, "w") as f:
                f.write("\n".join(names) + "\n")
        elif os.path.exists(hf):
            os.remove(hf)  # don't leave an empty .hidden lying around
    except OSError:
        pass


def is_listing_hidden(path: str) -> bool:
    name = os.path.basename(os.path.realpath(os.path.expanduser(path)))
    try:
        with open(_hidden_file(path)) as f:
            return any(ln.rstrip("\n") == name for ln in f)
    except OSError:
        return False


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) + 1 and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


# ── operations ───────────────────────────────────────────────────────────────

def cmd_create(args) -> int:
    mount = os.path.realpath(os.path.expanduser(args.path))
    ok, why = is_safe_mount(mount)
    if not ok:
        print(f"vault: {why}", file=sys.stderr)
        return 1
    if os.path.exists(mount) and os.path.isdir(mount) and os.listdir(mount):
        print(f"vault: {mount} is not empty. For now, create an empty vault and "
              "move files in after unlocking (import of existing data comes later).",
              file=sys.stderr)
        return 1

    reg = load_registry()
    if resolve(reg, mount):
        print("vault: a vault already exists there.", file=sys.stderr)
        return 1

    os.makedirs(mount, exist_ok=True)
    try:
        vid, vdir, cipher = _init_vault_dir()
    except RuntimeError as e:
        print(f"vault: {e}", file=sys.stderr)
        return 1

    reg[vid] = {"name": args.name or os.path.basename(mount),
                "mount": mount, "cipher": cipher, "hide": bool(args.hide)}
    save_registry(reg)
    print(f"created vault '{reg[vid]['name']}' ({vid}) at {mount}")
    if args.unlock:
        return _mount(vdir, cipher, mount)
    print("It's locked (empty) now. Unlock with:  vault.py unlock", mount)
    return 0


def cmd_import(args) -> int:
    """Turn an EXISTING folder into an encrypted vault *in place*: its current
    contents are moved into the vault's ciphertext, so afterwards the folder is
    empty when locked and shows the decrypted files when unlocked. The data never
    leaves your home — it's just re-encrypted where it sits.

    Failure is atomic-ish: if anything goes wrong mid-move we move everything back
    and remove the half-made vault, so the folder is left as we found it."""
    mount = os.path.realpath(os.path.expanduser(args.path))
    ok, why = is_safe_mount(mount)
    if not ok:
        print(f"vault: {why}", file=sys.stderr)
        return 1
    if not os.path.isdir(mount):
        print(f"vault: {mount} is not a folder.", file=sys.stderr)
        return 1
    reg = load_registry()
    if resolve(reg, mount):
        print("vault: a vault already exists there.", file=sys.stderr)
        return 1

    try:
        vid, vdir, cipher = _init_vault_dir()
    except RuntimeError as e:
        print(f"vault: {e}", file=sys.stderr)
        return 1

    # Mount at a temp point, move the folder's contents INTO it (encrypting them),
    # then unmount — leaving `mount` empty on disk (contents now in ciphertext).
    tmp_mount = os.path.join(vdir, "import-mnt")
    os.makedirs(tmp_mount, exist_ok=True)
    r = subprocess.run(["gocryptfs", *_extpass(vdir), cipher, tmp_mount],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        shutil.rmtree(vdir, ignore_errors=True)
        print("vault: mount for import failed:\n" + r.stdout, file=sys.stderr)
        return 1

    moved = []
    try:
        for name in os.listdir(mount):
            shutil.move(os.path.join(mount, name), os.path.join(tmp_mount, name))
            moved.append(name)
    except OSError as e:
        # Roll back: move everything we managed to move back out, then tear down.
        for name in moved:
            try:
                shutil.move(os.path.join(tmp_mount, name), os.path.join(mount, name))
            except OSError:
                pass
        subprocess.run([_fusermount(), "-u", tmp_mount],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(vdir, ignore_errors=True)
        print(f"vault: couldn't move files into the vault ({e}). Nothing changed.",
              file=sys.stderr)
        return 1

    subprocess.run([_fusermount(), "-u", tmp_mount],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.rmdir(tmp_mount)
    except OSError:
        pass

    reg[vid] = {"name": args.name or os.path.basename(mount),
                "mount": mount, "cipher": cipher, "hide": bool(args.hide)}
    save_registry(reg)
    print(f"imported '{reg[vid]['name']}' ({vid}) — {len(moved)} item(s) encrypted")
    if args.unlock:  # leave it open so the user immediately sees their files back
        return _mount(vdir, cipher, mount)
    return 0


def _mount(vdir: str, cipher: str, mount: str) -> int:
    if is_mounted(mount):
        print("already unlocked.")
        return 0
    os.makedirs(mount, exist_ok=True)
    r = subprocess.run(["gocryptfs", *_extpass(vdir), cipher, mount],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        print("vault: unlock failed:\n" + r.stdout, file=sys.stderr)
        return 1
    print(f"unlocked → {mount}")
    return 0


def cmd_unlock(args) -> int:
    reg = load_registry()
    vid = resolve(reg, args.key)
    if not vid:
        print("vault: no such vault.", file=sys.stderr)
        return 1
    v = reg[vid]
    rc = _mount(os.path.join(vaults_root(), vid), v["cipher"], v["mount"])
    if rc == 0:
        _set_listing_hidden(v["mount"], False)  # visible while open, like KDE Vaults
    return rc


def cmd_lock(args) -> int:
    reg = load_registry()
    vid = resolve(reg, args.key)
    if not vid:
        print("vault: no such vault.", file=sys.stderr)
        return 1
    mount = reg[vid]["mount"]
    if not is_mounted(mount):
        print("already locked.")
        return 0
    fuser = _fusermount()
    r = subprocess.run([fuser, "-u", mount],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        print("vault: lock failed (files still open?):\n" + r.stdout, file=sys.stderr)
        return 1
    if reg[vid].get("hide"):
        _set_listing_hidden(mount, True)  # tuck the empty folder out of sight
    print(f"locked → {mount} is now empty/encrypted.")
    return 0


def cmd_hide(args) -> int:
    """Set (or clear, with --off) 'hide when locked' and apply it now."""
    reg = load_registry()
    vid = resolve(reg, args.key)
    if not vid:
        print("vault: no such vault.", file=sys.stderr)
        return 1
    hide = not args.off
    reg[vid]["hide"] = hide
    save_registry(reg)
    mount = reg[vid]["mount"]
    # Only actually hide while it's locked; an open vault stays visible.
    _set_listing_hidden(mount, hide and not is_mounted(mount))
    print(f"{'will hide' if hide else 'will show'} {mount} when locked.")
    return 0


def cmd_status(args) -> int:
    reg = load_registry()
    keys = [resolve(reg, args.key)] if args.key else list(reg)
    for vid in keys:
        if not vid:
            print("vault: no such vault.", file=sys.stderr)
            return 1
        v = reg[vid]
        state = "UNLOCKED" if is_mounted(v["mount"]) else "locked"
        print(f"{state:9} {v['name']:20} {v['mount']}  ({vid})")
    if not keys:
        print("(no vaults yet — create one with: vault.py create <path>)")
    return 0


def cmd_list(args) -> int:
    return cmd_status(argparse.Namespace(key=None))


def cmd_destroy(args) -> int:
    reg = load_registry()
    vid = resolve(reg, args.key)
    if not vid:
        print("vault: no such vault.", file=sys.stderr)
        return 1
    v = reg[vid]
    if is_mounted(v["mount"]):
        print("vault: unlock-mounted; lock it first.", file=sys.stderr)
        return 1
    if not args.force:
        print(f"vault: this deletes the encrypted contents of '{v['name']}' "
              f"permanently. Re-run with --force to confirm.", file=sys.stderr)
        return 1
    _set_listing_hidden(v["mount"], False)  # un-hide before removing
    shutil.rmtree(os.path.join(vaults_root(), vid), ignore_errors=True)
    # Leave the (now-empty) mount dir in place; remove if we made it and it's empty.
    try:
        if os.path.isdir(v["mount"]) and not os.listdir(v["mount"]):
            os.rmdir(v["mount"])
    except OSError:
        pass
    del reg[vid]
    save_registry(reg)
    print(f"destroyed vault '{v['name']}'.")
    return 0


# ── offline self-test (no gocryptfs / FUSE) ──────────────────────────────────

def _selftest() -> int:
    fails = []
    # path safety
    for bad in ("/", "/etc", "/usr", os.path.expanduser("~")):
        ok, _ = is_safe_mount(bad)
        if ok:
            fails.append(f"is_safe_mount allowed {bad}")
    ok, _ = is_safe_mount(os.path.expanduser("~/Private"))
    if not ok:
        fails.append("is_safe_mount rejected ~/Private")
    outside = "/tmp/not-home"
    if is_safe_mount(outside)[0]:
        fails.append("is_safe_mount allowed a path outside home")
    # octal unescape
    if _unescape("a\\040b") != "a b":
        fails.append("unescape \\040")
    # registry resolve
    reg = {"abcd": {"name": "x", "mount": "/home/u/P", "cipher": "c"}}
    if resolve(reg, "abcd") != "abcd":
        fails.append("resolve by id")
    if fails:
        print("vault self-test FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("vault self-test: all checks passed")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    p = argparse.ArgumentParser(prog="vault.py", description="AppLocker encrypted vaults")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="make a new empty encrypted vault")
    c.add_argument("path")
    c.add_argument("--name")
    c.add_argument("--unlock", action="store_true", help="mount it right after creating")
    c.add_argument("--hide", action="store_true", help="hide the folder while locked")
    c.set_defaults(fn=cmd_create)

    im = sub.add_parser("import", help="encrypt an EXISTING folder's contents in place")
    im.add_argument("path")
    im.add_argument("--name")
    im.add_argument("--unlock", action="store_true", help="leave it mounted after import")
    im.add_argument("--hide", action="store_true", help="hide the folder while locked")
    im.set_defaults(fn=cmd_import)

    h = sub.add_parser("hide", help="hide the folder in the file manager while locked")
    h.add_argument("key", help="vault id or mount path")
    h.add_argument("--off", action="store_true", help="stop hiding it")
    h.set_defaults(fn=cmd_hide)

    for name, fn, help_ in (("unlock", cmd_unlock, "mount (decrypt)"),
                            ("lock", cmd_lock, "unmount (hide/encrypt)"),
                            ("destroy", cmd_destroy, "delete a vault + its ciphertext")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("key", help="vault id or mount path")
        if name == "destroy":
            s.add_argument("--force", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("status", help="locked/unlocked")
    s.add_argument("key", nargs="?")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("list", help="all vaults")
    s.set_defaults(fn=cmd_list)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
