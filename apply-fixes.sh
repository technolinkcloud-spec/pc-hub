#!/bin/bash
# apply-fixes.sh — one-shot repair pass for an ALREADY-INSTALLED kiosk.
#
# The code fixes in a release arrive through update.sh / the dashboard's
# Offline Update. But three things live outside the install directory and
# therefore never travel with a code update:
#
#   1. the system clock  — a clock that is behind makes apt reject repository
#      signatures ("Not live until ...") and breaks TLS everywhere;
#   2. USB auto-mount    — a udev rule under /etc, so USB sticks appear in the
#      Offline Update file picker (needs root);
#   3. certificates      — anything dropped into ./certs/ is installed into
#      Chromium's NSS store with the trust flags that match what it is.
#
# Run it as the kiosk user after updating:
#     bash /opt/kiosk-manager/apply-fixes.sh
#
# Add sudo to also install the USB auto-mount rule, which needs root:
#     sudo bash /opt/kiosk-manager/apply-fixes.sh
#
# Safe to re-run — every step is idempotent.

set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

C_OK='\033[0;32m'; C_WARN='\033[1;33m'; C_ERR='\033[0;31m'; C_INFO='\033[0;36m'; C_OFF='\033[0m'
ok()   { printf "${C_OK}[ok]${C_OFF}   %s\n" "$*"; }
info() { printf "${C_INFO}[..]${C_OFF}   %s\n" "$*"; }
warn() { printf "${C_WARN}[!]${C_OFF}    %s\n" "$*"; }
err()  { printf "${C_ERR}[x]${C_OFF}    %s\n" "$*"; }

if [ "$(id -u)" = "0" ]; then SUDO=""; IS_ROOT=1; else SUDO="sudo -n"; IS_ROOT=0; fi

# The kiosk user owns the install dir; when run under sudo we must still act on
# THAT user's home, not root's, or the certificates land in a store Chrome
# never reads.
KIOSK_USER="$(stat -c '%U' "$DIR" 2>/dev/null || id -un)"
KIOSK_HOME="$(getent passwd "$KIOSK_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$KIOSK_HOME" ] || KIOSK_HOME="$HOME"

# Run a command as the kiosk user when we are root, plainly otherwise. A
# function rather than an array: expanding an empty array under `set -u` is an
# error on bash before 4.4.
run_as() {
    if [ "$IS_ROOT" = "1" ]; then sudo -u "$KIOSK_USER" "$@"; else "$@"; fi
}

echo "════════════════════════════════════════════════"
echo "  Kiosk Manager — post-update fixes"
echo "  install dir: $DIR"
echo "  kiosk user:  $KIOSK_USER ($KIOSK_HOME)"
echo "════════════════════════════════════════════════"

# ── 1. Clock ─────────────────────────────────────────────────────────────
echo
info "1/3  System clock"
if command -v timedatectl >/dev/null 2>&1; then
    if $SUDO timedatectl set-ntp true 2>/dev/null; then
        $SUDO systemctl enable --now systemd-timesyncd 2>/dev/null || true
        sleep 2
        ok "NTP enabled — clock is now $(date)"
        timedatectl 2>/dev/null | grep -iE 'synchronized|time zone' || true
    else
        warn "Could not enable NTP automatically. Run: sudo timedatectl set-ntp true"
    fi
else
    warn "timedatectl not present — skipping clock sync"
fi

# ── 2. USB auto-mount (root only) ────────────────────────────────────────
echo
info "2/3  USB auto-mount"
if [ -f "$DIR/usb-automount.sh" ]; then
    # usb-automount.sh handles privileges itself: as root directly, otherwise
    # through the passwordless sudo the installer grants for tee/udevadm.
    if bash "$DIR/usb-automount.sh"; then
        ok "USB auto-mount configured"
    else
        warn "USB auto-mount could not be configured — see the message above"
        [ "$IS_ROOT" = "1" ] || \
            warn "Running once as root will fix it:  sudo bash $DIR/apply-fixes.sh"
    fi
else
    warn "usb-automount.sh missing from $DIR — skipping"
fi

# ── 3. Bundled certificates ──────────────────────────────────────────────
echo
info "3/3  Certificates from $DIR/certs"
shopt -s nullglob
CERTS=("$DIR"/certs/*.crt "$DIR"/certs/*.pem "$DIR"/certs/*.cer)
shopt -u nullglob

if [ "${#CERTS[@]}" -eq 0 ]; then
    info "No certificates bundled — nothing to install."
elif ! command -v certutil >/dev/null 2>&1; then
    warn "certutil not found. Install it with: sudo apt install libnss3-tools"
else
    NSSDB="$KIOSK_HOME/.pki/nssdb"
    # certutil runs as the kiosk user (see run_as) so the database stays theirs.
    if [ ! -d "$NSSDB" ]; then
        run_as mkdir -p "$NSSDB"
        run_as certutil -d "sql:$NSSDB" -N --empty-password >/dev/null 2>&1 || true
    fi

    for cert in "${CERTS[@]}"; do
        nick="$(basename "$cert")"; nick="${nick%.*}"

        # A certificate authority is trusted as an anchor (CT,C,C). A plain
        # server certificate has basicConstraints CA:FALSE and can never be an
        # anchor — Chrome rejects that — so it is trusted as a peer (P,,).
        if openssl x509 -in "$cert" -noout -ext basicConstraints 2>/dev/null \
             | grep -qi 'CA:TRUE'; then
            trust='CT,C,C'; kind='authority'
        else
            trust='P,,';    kind='server certificate'
        fi

        # Replace any previous copy so re-running cannot pile up duplicates.
        run_as certutil -d "sql:$NSSDB" -D -n "$nick" >/dev/null 2>&1 || true
        if run_as certutil -d "sql:$NSSDB" -A -t "$trust" -n "$nick" -i "$cert" 2>/dev/null; then
            ok "$nick — $kind, trust $trust"
        else
            err "$nick — certutil failed"
        fi

        if ! openssl x509 -in "$cert" -noout -checkend 7776000 >/dev/null 2>&1; then
            warn "  ^ expires within 90 days ($(openssl x509 -in "$cert" -noout -enddate | cut -d= -f2))"
        fi
    done
fi

echo
echo "════════════════════════════════════════════════"
ok "Done. Restart Chrome so it re-reads the certificate store:"
echo "     pkill -f chromium"
echo "════════════════════════════════════════════════"
