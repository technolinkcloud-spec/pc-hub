#!/bin/bash
# usb-update.sh — Apply an offline Kiosk Manager update from a USB flash drive.
#
# For kiosks with NO internet access. Workflow for the operator:
#   1. Copy a release zip (kiosk-manager-*-offline.zip) onto ANY USB stick.
#   2. Plug the stick into the kiosk PC.
#   3. Switch to a console:  Ctrl+Alt+F2   (Ctrl+Alt+F1 goes back to the kiosk)
#   4. Log in as the kiosk user (empty password → just press Enter).
#   5. Run:  bash /opt/kiosk-manager/usb-update.sh
#
# It finds the stick, mounts it READ-ONLY, locates the update zip, copies it to
# local disk, unmounts (so the stick can be pulled), extracts it, and runs the
# bundled update.sh — exactly like the dashboard's "Offline update", but from a
# console instead of a browser.
#
# Runs as the NON-ROOT kiosk user. No root login and no password are needed:
# mounting uses the passwordless `sudo mount`/`sudo umount` granted by the
# installer's sudoers rules. If those rules are missing (an older install), see
# the "bootstrap" note printed on failure.

set -uo pipefail

INSTALL_DIR="${KIOSK_INSTALL_DIR:-/opt/kiosk-manager}"
ZIP_GLOB='kiosk-manager-*-offline.zip'

C_INFO='\033[0;36m'; C_OK='\033[0;32m'; C_ERR='\033[0;31m'; C_OFF='\033[0m'
log() { printf "${C_INFO}[usb-update]${C_OFF} %s\n" "$*"; }
ok()  { printf "${C_OK}[usb-update]${C_OFF} %s\n" "$*"; }
err() { printf "${C_ERR}[usb-update]${C_OFF} %s\n" "$*" >&2; }
die() { err "$*"; exit 1; }

# Privilege: mount needs root. As the kiosk user we go through passwordless sudo.
if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo -n"; fi

MP=""          # temp mountpoint (under /tmp, user-owned)
WORK=""        # temp work dir for the extracted update
cleanup() {
    if [ -n "$MP" ] && mountpoint -q "$MP" 2>/dev/null; then
        $SUDO umount "$MP" 2>/dev/null || true
    fi
    [ -n "$MP" ] && rmdir "$MP" 2>/dev/null || true
    [ -n "$WORK" ] && rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

[ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR not found — is Kiosk Manager installed here?"

# ── 1. Enumerate candidate USB partitions ───────────────────────────────
# Removable (RM=1) or USB-transport (TRAN=usb) partitions, full device paths.
# (Plain read loop, not mapfile, so this works on any bash and stays safe
#  under `set -u` even when nothing is found.)
PARTS=()
while IFS= read -r dev; do
    [ -n "$dev" ] && PARTS+=("$dev")
done < <(lsblk -rpno NAME,TYPE,RM,TRAN 2>/dev/null | \
         awk '$2=="part" && ($3=="1" || $4=="usb") {print $1}')

[ "${#PARTS[@]}" -gt 0 ] || die "No USB flash drive detected. Plug the stick in, wait a few seconds, then re-run. (Check with: lsblk)"

log "Found ${#PARTS[@]} removable partition(s): ${PARTS[*]}"

# ── 2. Mount each in turn and look for the update zip ────────────────────
ZIP=""
for DEV in "${PARTS[@]}"; do
    MP="$(mktemp -d /tmp/kiosk-usb.XXXXXX)" || die "Could not create a temp mountpoint."
    log "Mounting $DEV (read-only)…"
    if ! $SUDO mount -o ro "$DEV" "$MP" 2>/tmp/kiosk-usb-mount.err; then
        if grep -qiE 'password is required|a password is required|not allowed' /tmp/kiosk-usb-mount.err 2>/dev/null; then
            rm -f /tmp/kiosk-usb-mount.err
            die $'sudo cannot mount without a password on this box (older install).\n'\
"           One-time fix — run this once as the kiosk user, then re-run usb-update.sh:\n"\
"             echo \"\$USER ALL=(ALL) NOPASSWD: \$(command -v mount), \$(command -v umount)\" | sudo tee /etc/sudoers.d/kiosk-manager-usb"
        fi
        rm -f /tmp/kiosk-usb-mount.err
        err "Could not mount $DEV (unsupported filesystem? try FAT32/exFAT). Skipping."
        rmdir "$MP" 2>/dev/null || true; MP=""
        continue
    fi
    rm -f /tmp/kiosk-usb-mount.err

    # Look for the release zip at the top level or one directory down.
    FOUND="$(find "$MP" -maxdepth 2 -type f -iname "$ZIP_GLOB" 2>/dev/null | head -n1)"
    if [ -z "$FOUND" ]; then
        FOUND="$(find "$MP" -maxdepth 2 -type f -iname '*.zip' 2>/dev/null | head -n1)"
    fi

    if [ -n "$FOUND" ]; then
        log "Found update package: ${FOUND#$MP/}"
        WORK="$(mktemp -d)" || die "Could not create a temp work dir."
        cp "$FOUND" "$WORK/update.zip" || die "Failed to copy the zip off the stick."
        ZIP="$WORK/update.zip"
        # Unmount immediately so the operator can pull the stick right away.
        $SUDO umount "$MP" 2>/dev/null || true
        rmdir "$MP" 2>/dev/null || true; MP=""
        break
    fi

    log "No update zip on $DEV."
    $SUDO umount "$MP" 2>/dev/null || true
    rmdir "$MP" 2>/dev/null || true; MP=""
done

[ -n "$ZIP" ] || die "No '$ZIP_GLOB' found on any USB stick. Copy the release zip to the stick and try again."

# ── 3. Extract the package ───────────────────────────────────────────────
EXTRACT="$WORK/extract"
mkdir -p "$EXTRACT"
log "Extracting…"
if command -v unzip >/dev/null 2>&1; then
    unzip -q "$ZIP" -d "$EXTRACT" || die "Failed to extract the zip (unzip)."
elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$ZIP" "$EXTRACT" || die "Failed to extract the zip (python3)."
else
    die "Neither 'unzip' nor 'python3' is available to extract the zip."
fi

# ── 4. Locate update.sh (top level or one dir down, like the dashboard) ──
SCRIPT="$EXTRACT/update.sh"
if [ ! -f "$SCRIPT" ]; then
    SCRIPT="$(find "$EXTRACT" -maxdepth 2 -type f -name update.sh 2>/dev/null | head -n1)"
fi
[ -n "$SCRIPT" ] && [ -f "$SCRIPT" ] || die "No update.sh inside the package — is this a valid release zip?"

# ── 5. Run the update (as the current non-root user, same as the dashboard) ──
ok "Package ready. Running update…"
echo "────────────────────────────────────────────────────────────"
chmod +x "$SCRIPT" 2>/dev/null || true
if bash "$SCRIPT"; then
    echo "────────────────────────────────────────────────────────────"
    ok "Update applied. The service will restart shortly."
    ok "Press Ctrl+Alt+F1 to return to the kiosk display."
else
    rc=$?
    echo "────────────────────────────────────────────────────────────"
    die "update.sh failed (exit $rc). Nothing was left mounted; fix the error and re-run."
fi
