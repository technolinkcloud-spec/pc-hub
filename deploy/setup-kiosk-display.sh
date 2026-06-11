#!/bin/bash
# Standalone version of install.sh Step 8 — sets up X11 + Chrome auto-launch,
# tty1 autologin, and switches default target to multi-user.target.
#
# Run when install.sh skipped the kiosk display setup (e.g. because the system
# was detected as non-headless or already had a display manager).
#
# Usage: sudo bash deploy/setup-kiosk-display.sh [username]
#   If username is omitted, uses $SUDO_USER, or fails if running as plain root.

set -e

if [ "$EUID" -ne 0 ]; then
    echo "[!] Must run as root: sudo bash $0 [username]"
    exit 1
fi

# ── Pick the kiosk user ───────────────────────────────────────
SERVICE_USER="${1:-${SUDO_USER:-}}"
if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
    echo "[!] Refusing to configure autologin for root."
    echo "    Usage: sudo bash $0 <username>"
    echo "    Example: sudo bash $0 technolink"
    exit 1
fi

REAL_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6 || true)
if [ -z "$REAL_HOME" ] || [ ! -d "$REAL_HOME" ]; then
    echo "[!] No home directory found for user '$SERVICE_USER'"
    exit 1
fi

INSTALL_DIR="/opt/kiosk-manager"
PORT="${PORT:-80}"

# ── Find a browser, install if missing ────────────────────────
BROWSER=""
for b in chromium chromium-browser google-chrome google-chrome-stable; do
    if command -v "$b" &>/dev/null; then
        BROWSER="$b"
        break
    fi
done
if [ -z "$BROWSER" ] && command -v apt-get &>/dev/null; then
    echo "[+] No browser found — installing chromium..."
    apt-get update -qq
    apt-get install -y chromium 2>/dev/null || apt-get install -y chromium-browser 2>/dev/null || true
    for b in chromium chromium-browser; do
        command -v "$b" &>/dev/null && BROWSER="$b" && break
    done
fi
[ -z "$BROWSER" ] && { echo "[!] No browser available — install chromium first"; exit 1; }

# ── Install X11 + openbox if missing ──────────────────────────
if ! command -v startx &>/dev/null || ! command -v openbox-session &>/dev/null; then
    if command -v apt-get &>/dev/null; then
        echo "[+] Installing xorg + openbox..."
        apt-get install -y xorg openbox x11-xserver-utils 2>/dev/null || true
    fi
fi

echo "[i] Kiosk user: $SERVICE_USER"
echo "[i] Home:       $REAL_HOME"
echo "[i] Browser:    $BROWSER"
echo "[i] Port:       $PORT"

# ── 1. Auto-login on tty1 ─────────────────────────────────────
echo "[+] Configuring autologin on tty1"
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $SERVICE_USER --noclear %I \$TERM
EOF

# ── 2. .xinitrc ───────────────────────────────────────────────
echo "[+] Writing $REAL_HOME/.xinitrc"
cat > "$REAL_HOME/.xinitrc" <<'XINITRC'
#!/bin/bash
LOGFILE="$HOME/.kiosk-x11.log"
exec >> "$LOGFILE" 2>&1
echo "=== Kiosk X11 starting at $(date) ==="

CRASH_FILE="$HOME/.kiosk-last-crash"
if [ -f "$CRASH_FILE" ]; then
    LAST_CRASH=$(cat "$CRASH_FILE")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_CRASH))
    if [ "$DIFF" -lt 10 ]; then
        echo "Crash loop detected (last crash ${DIFF}s ago). Sleeping 30s..."
        sleep 30
    fi
fi

xset s off
xset -dpms
xset s noblank

python3 /opt/kiosk-manager/hide_cursor.py &

openbox-session &
sleep 2

CHROME_FLAGS=(
    --start-fullscreen
    --noerrdialogs
    --disable-infobars
    --no-first-run
    --disable-session-crashed-bubble
    --hide-crash-restore-bubble
    --disable-features=TranslateUI
    --disable-translate
    --disable-component-update
    --no-default-browser-check
    --disable-default-apps
    --autoplay-policy=no-user-gesture-required
    --check-for-update-interval=31536000
    --remote-debugging-port=9222
    --remote-debugging-address=0.0.0.0
    --remote-allow-origins=*
)

if [ "$(id -u)" = "0" ]; then
    CHROME_FLAGS+=(--no-sandbox --test-type)
fi

# GPU flags: ONLY disable the GPU inside a VM. On real hardware this pair
# leaves Chrome with no renderer and it exits ~2s after launch (the relaunch
# loop then becomes a "loading page keeps restarting" lockout). A real PC
# should use its GPU.
if systemd-detect-virt --quiet 2>/dev/null; then
    CHROME_FLAGS+=(--disable-gpu --disable-software-rasterizer)
fi

LOADING_URL="http://localhost:__PORT__/kiosk/loading"

while true; do
    CHROME_PREFS="$HOME/.config/chromium/Default/Preferences"
    if [ -f "$CHROME_PREFS" ]; then
        sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$CHROME_PREFS" 2>/dev/null || true
        sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$CHROME_PREFS" 2>/dev/null || true
    fi

    echo "Launching: __BROWSER__ ${CHROME_FLAGS[*]} $LOADING_URL"
    START=$(date +%s)
    __BROWSER__ "${CHROME_FLAGS[@]}" "$LOADING_URL"
    RETCODE=$?
    RUNTIME=$(( $(date +%s) - START ))
    echo "Chrome exited with code $RETCODE at $(date) (ran ${RUNTIME}s)"
    # Crash-loop backoff: if Chrome died in <10s, wait 30s instead of relaunching
    # every 2s, so the kiosk stays reachable instead of hard-locking.
    if [ "$RUNTIME" -lt 10 ]; then
        date +%s > "$CRASH_FILE"
        echo "Chrome died in <10s — backing off 30s to stay recoverable"
        sleep 30
    else
        sleep 2
    fi
done
XINITRC

sed -i "s|__BROWSER__|$BROWSER|g" "$REAL_HOME/.xinitrc"
sed -i "s|__PORT__|$PORT|g" "$REAL_HOME/.xinitrc"
chmod +x "$REAL_HOME/.xinitrc"
chown "$SERVICE_USER":"$SERVICE_USER" "$REAL_HOME/.xinitrc"

# ── 3. .bash_profile auto-startx ──────────────────────────────
BP="$REAL_HOME/.bash_profile"
touch "$BP"
sed -i '/# Auto-start X11 on tty1/,/^fi$/d' "$BP" 2>/dev/null || true
sed -i '/exec startx/d' "$BP" 2>/dev/null || true
cat >> "$BP" <<'PROFILE'

# Auto-start X11 on tty1 (kiosk mode)
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
PROFILE
chown "$SERVICE_USER":"$SERVICE_USER" "$BP"
echo "[+] $BP updated"

# ── 4. Disable display manager if present (we want tty1) ──────
for dm in lightdm gdm gdm3 sddm xdm lxdm; do
    if systemctl is-enabled "$dm" &>/dev/null; then
        echo "[+] Disabling display manager: $dm"
        systemctl disable "$dm" 2>/dev/null || true
    fi
done

# ── 5. Boot target ────────────────────────────────────────────
echo "[+] Switching default target to multi-user.target"
systemctl set-default multi-user.target
systemctl daemon-reload

echo
echo "[✓] Kiosk display setup complete."
echo "    Reboot to start: sudo reboot"
