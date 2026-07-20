#!/bin/bash
# make-offline-zip.sh — Build the offline update package.
#
# Produces  kiosk-manager-<version>-offline.zip  in the repo root: a self-
# contained bundle of everything update.sh needs, minus the virtualenv, the
# database, git metadata, and Python caches. Upload it via the dashboard's
# "Offline update", or copy it to a USB stick and apply it with usb-update.sh.
#
# Usage:  bash deploy/make-offline-zip.sh
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root

VER="$(cat version.txt)"
OUT="kiosk-manager-${VER}-offline.zip"

command -v zip >/dev/null || { echo "[!] 'zip' is not installed"; exit 1; }

rm -f "$OUT"
zip -r "$OUT" \
    app.py wsgi.py config.py database.py auth_utils.py sysdetect.py hide_cursor.py \
    requirements.txt version.txt README.md .gitignore \
    install.sh update.sh usb-update.sh usb-automount.sh apply-fixes.sh \
    routes static templates deploy certs \
    -x '*/__pycache__/*' '*.pyc' '*/.DS_Store' '.DS_Store' '*/make-offline-zip.sh' \
    >/dev/null

echo "[OK] Built $OUT"
unzip -l "$OUT" | tail -n 1
unzip -l "$OUT" | grep -E 'usb-update.sh|usb-automount.sh|apply-fixes.sh|update.sh|install.sh|version.txt|certs/'
