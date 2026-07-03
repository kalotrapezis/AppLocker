#!/bin/bash
# Build the AppLocker .deb. Compiles the daemon + PAM module in release mode,
# stages everything into the installed flat layout, and wraps it with dpkg-deb.
#
#   packaging/build-deb.sh            # → dist/applocker_<ver>_<arch>.deb
#
# No root needed (uses --root-owner-group). Requires: cargo, dpkg-deb.
set -euo pipefail

# Version is a fixed base + a testing-round letter that bumps every build:
# 0.0.1-a, 0.0.1-b, ...  Pass an explicit letter to rebuild a round:
#   packaging/build-deb.sh c
VERSION_BASE="0.0.1"
ROUND_FILE="$(dirname "$0")/.build-round"
if [ "${1:-}" != "" ]; then
	LETTER="$1"
else
	n=0
	[ -f "$ROUND_FILE" ] && n="$(cat "$ROUND_FILE")"
	n=$((n + 1))
	echo "$n" > "$ROUND_FILE"
	# 1->a, 26->z, 27->aa (bijective base-26)
	LETTER="$(awk -v n="$n" 'BEGIN{s="";while(n>0){n--;r=n%26;s=sprintf("%c",97+r) s;n=int(n/26)}print s}')"
fi
VERSION="${VERSION_BASE}-${LETTER}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
TRIPLET="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || cc -print-multiarch 2>/dev/null || echo x86_64-linux-gnu)"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> building release binaries"
( cd "$REPO/daemon" && cargo build --release --quiet )
( cd "$REPO/pam"    && cargo build --release --quiet )

echo "==> staging into $STAGE"
LIB="$STAGE/usr/lib/applocker"
install -d "$LIB" \
	"$STAGE/usr/bin" \
	"$STAGE/lib/$TRIPLET/security" \
	"$STAGE/lib/systemd/system" \
	"$STAGE/etc/xdg/autostart" \
	"$STAGE/etc/applocker" \
	"$STAGE/usr/share/applications" \
	"$STAGE/usr/share/doc/applocker" \
	"$STAGE/DEBIAN"

# Daemon + all Python helpers, flattened (matches the code's installed-layout
# fallbacks: /usr/lib/applocker/<script>.py).
install -m 0755 "$REPO/daemon/target/release/applockerd" "$LIB/applockerd"
for py in "$REPO"/face/*.py "$REPO"/gui/*.py "$REPO"/vault/*.py; do
	install -m 0644 "$py" "$LIB/"
done

# PAM module into the multiarch security dir.
install -m 0644 "$REPO/pam/target/release/libpam_applocker.so" \
	"$STAGE/lib/$TRIPLET/security/pam_applocker.so"

# CLI entry point on PATH (so `pkexec applockerd …` and the GUI work).
ln -s ../lib/applocker/applockerd "$STAGE/usr/bin/applockerd"
install -m 0755 "$REPO/packaging/bin/applocker-pam" "$STAGE/usr/bin/applocker-pam"
install -m 0755 "$REPO/packaging/bin/applocker" "$STAGE/usr/bin/applocker"
install -m 0755 "$REPO/packaging/bin/applocker-test-scope" "$STAGE/usr/bin/applocker-test-scope"

# DEV MODE marker: while this file exists the daemon refuses to gate all of / (so
# the app-gate can't freeze the machine); the app-gate only runs when explicitly
# scoped to a sandbox mount. Remove it to allow the real system-wide gate.
#
# Set APPLOCKER_RELEASE=1 to build a REAL-GATE package (no marker) — for a VM,
# never the host.  e.g.  APPLOCKER_RELEASE=1 packaging/build-deb.sh vm1
if [ "${APPLOCKER_RELEASE:-}" = "1" ]; then
	echo "==> RELEASE build: system-wide gate ENABLED (no dev-mode marker) — VM only!"
else
	cat > "$STAGE/etc/applocker/dev-mode" <<'EOF'
AppLocker dev mode. While this file exists, `applockerd gate` will NOT mark the
whole filesystem (the input-freeze is impossible). Test the app-gate safely with
  sudo applocker-test-scope up
  sudo applocker-test-scope gate
Delete this file to enable the real, system-wide gate.
EOF
	chmod 0644 "$STAGE/etc/applocker/dev-mode"
fi

# System integration (service ships disabled; autostart is per-user & self-gating).
install -m 0644 "$REPO/packaging/systemd/applockerd.service" "$STAGE/lib/systemd/system/"
install -m 0644 "$REPO/packaging/autostart/applocker-watcher.desktop" "$STAGE/etc/xdg/autostart/"
install -m 0644 "$REPO/packaging/applocker-settings.desktop" "$STAGE/usr/share/applications/"
install -m 0644 "$REPO/README.md" "$STAGE/usr/share/doc/applocker/README.md"
install -m 0644 "$REPO/TESTS.md"  "$STAGE/usr/share/doc/applocker/TESTS.md"

INSTALLED_KB="$(du -ks "$STAGE" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: applocker
Version: $VERSION
Section: admin
Priority: optional
Architecture: $ARCH
Depends: python3, python3-opencv, python3-numpy, python3-gi, gir1.2-gtk-3.0, libpam0g, libxss1, pkexec, systemd, gocryptfs, fuse3
Recommends: v4l-utils
Installed-Size: $INSTALLED_KB
Maintainer: AppLocker <kalotrapezis@gmail.com>
Description: Android-style app & folder locking for Linux, with face unlock
 Gates locked apps and folders behind a face / PIN / sudo prompt via fanotify,
 auto-locks the session when you walk away (idle-triggered camera snapshots),
 and can add face+liveness login to sudo, the screensaver and LightDM.
 Threat model is casual local access, not high security.
EOF

# postinst — no PAM edits, no service auto-enabled. The dev build additionally
# bars the gate from marking all of / (via /etc/applocker/dev-mode).
if [ "${APPLOCKER_RELEASE:-}" = "1" ]; then
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ -x /bin/systemctl ] || [ -x /usr/bin/systemctl ]; then
	systemctl daemon-reload >/dev/null 2>&1 || true
fi
cat <<'MSG'

AppLocker installed — RELEASE build (real system-wide gate, exec-only).
*** Intended for a throwaway VM. Snapshot it first. *** Enforcement is OFF
until you enable it.

  1. Set a PIN (no webcam in a VM, so face won't run):
       sudo applockerd set-pin
  2. Lock an app (deb / flatpak / AppImage) from Settings, or:
       sudo applockerd lock-app "<name>"
  3. Turn the GATE ON — this is JUST the app-launch gate, no PAM:
       sudo systemctl enable --now applockerd.service
     Launch the app -> it should prompt for the PIN first.
     Undo:  sudo systemctl disable --now applockerd.service

The gate is exec-only (folders use encrypted vaults, not fanotify), so it can't
freeze on file opens. A hung launch fails open after APPLOCKER_GATE_TIMEOUT (30s).

Do NOT run `applocker on` or `applocker-pam` yet: those edit /etc/pam.d (login /
sudo / screensaver) and can lock you out — that's a separate, later step, done
only in a VM with a root shell open. See /usr/share/doc/applocker/TESTS.md.

MSG
exit 0
EOF
else
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ -x /bin/systemctl ] || [ -x /usr/bin/systemctl ]; then
	systemctl daemon-reload >/dev/null 2>&1 || true
fi
cat <<'MSG'

AppLocker installed in DEV MODE — safe to test, cannot freeze the machine.
(While /etc/applocker/dev-mode exists, the app-gate refuses to gate all of /.)

Try these — all userspace, nothing persists across a reboot:

  1. First-run wizard (models + enrollment):
       python3 /usr/lib/applocker/welcome.py
  2. Private folder (encrypted vault) — open Settings, "Private folder":
       python3 /usr/lib/applocker/settings.py
  3. App-gate in the safe sandbox (never touches real apps):
       sudo applocker-test-scope up
       sudo applocker-test-scope gate     # gates only /tmp/applocker-test; Ctrl-C stops
       # then in another terminal:  /tmp/applocker-test/bin/lockme   (it prompts)
       sudo applocker-test-scope down

Do NOT run `applocker on` yet — it's for the system-wide gate/PAM, which is
still deferred. Remove /etc/applocker/dev-mode only when you deliberately want
the real gate. See /usr/share/doc/applocker/TESTS.md.

MSG
exit 0
EOF
fi

# prerm — turn off the service if the admin enabled it.
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
	if [ -x /bin/systemctl ] || [ -x /usr/bin/systemctl ]; then
		systemctl disable --now applockerd.service >/dev/null 2>&1 || true
	fi
fi
exit 0
EOF

# postrm — remind about PAM lines we never auto-edited.
cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "purge" ] || [ "$1" = "remove" ]; then
	if grep -lq pam_applocker.so /etc/pam.d/* 2>/dev/null; then
		echo "note: pam_applocker.so is still referenced in /etc/pam.d — run" >&2
		echo "      'applocker-pam disable all' BEFORE removing, or edit those files." >&2
	fi
	[ "$1" = "purge" ] && rm -rf /etc/applocker
fi
exit 0
EOF

# Track the dev-mode marker as a conffile so deleting it (to enable the real
# gate) is remembered across upgrades instead of being silently restored.
if [ "${APPLOCKER_RELEASE:-}" != "1" ]; then
	printf '/etc/applocker/dev-mode\n' > "$STAGE/DEBIAN/conffiles"
fi

chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

mkdir -p "$REPO/dist"
OUT="$REPO/dist/applocker_${VERSION}_${ARCH}.deb"
echo "==> building $OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "built: $OUT"
dpkg-deb --info "$OUT" | sed -n '1,12p'
