#!/bin/bash
# Offline updater for Kiosk Manager.
#
# Used by the dashboard's "Offline update" (Update page): upload a release .zip
# and click Run — the app runs THIS script as the service user (no sudo, no
# console needed). It also works from a console as root: `sudo bash update.sh`.
#
# It copies the new code into the install dir (preserving venv/ and the data/
# DB), refreshes Python deps, and restarts the service. No internet required.
#
# Why this works with only dashboard access:
#   - the service user OWNS the install dir, so it can copy files without sudo;
#   - `sudo systemctl restart kiosk-manager` is NOPASSWD-allowed in sudoers.
set -e

INSTALL_DIR="${KIOSK_INSTALL_DIR:-/opt/kiosk-manager}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[*] Offline update: $SRC_DIR -> $INSTALL_DIR"
[ -d "$INSTALL_DIR" ] || { echo "[!] $INSTALL_DIR not found"; exit 1; }

# Copy code into place — never touch the virtualenv, the DB, or git metadata.
if command -v rsync >/dev/null 2>&1; then
    rsync -a \
        --exclude '.git/' --exclude 'venv/' --exclude 'data/' --exclude 'update.sh' \
        "$SRC_DIR"/ "$INSTALL_DIR"/
else
    (cd "$SRC_DIR" && tar -cf - \
        --exclude='.git' --exclude='venv' --exclude='data' --exclude='update.sh' .) \
        | (cd "$INSTALL_DIR" && tar -xf -)
fi

# Refresh Python deps in the existing venv (a no-op when unchanged).
PIP="$INSTALL_DIR/venv/bin/pip"
[ -x "$PIP" ] && "$PIP" install -q -r "$INSTALL_DIR/requirements.txt" || true

# If we happen to be running as root (console path), hand ownership back to the
# service user. When run as that user (dashboard path) this is a harmless no-op.
if [ "$(id -u)" = "0" ]; then
    OWNER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo root)"
    chown -R "$OWNER":"$OWNER" "$INSTALL_DIR" 2>/dev/null || true
fi

NEW_VER="$(cat "$INSTALL_DIR/version.txt" 2>/dev/null || echo '?')"
echo "[OK] Files updated — now at version $NEW_VER"

# Apply the things that live OUTSIDE the install dir and therefore cannot
# arrive with a code update: the system clock, the USB auto-mount udev rule,
# and any certificates in certs/. Requiring a separate manual command meant it
# never got run on a real kiosk. Each step uses the passwordless sudo rules the
# installer grants, so no root login is needed.
#
# Never fatal: `set -e` is on, and a repair failing must not abort an update
# whose code changes have already been written to disk.
if [ -f "$INSTALL_DIR/apply-fixes.sh" ]; then
    echo "[*] Applying post-update fixes (clock, USB auto-mount, certificates)..."
    bash "$INSTALL_DIR/apply-fixes.sh" || \
        echo "[!] Some post-update fixes did not apply — see above. Update itself is fine."
fi

# Restart detached + delayed so this script (and the dashboard's live output)
# can finish before systemd SIGTERMs the service. `sudo systemctl restart
# kiosk-manager` is NOPASSWD-allowed, so it works without a console.
echo "[*] Restarting kiosk-manager in 3s..."
( sleep 3; sudo systemctl restart kiosk-manager ) >/dev/null 2>&1 &
