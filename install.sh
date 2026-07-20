#!/bin/bash
# ============================================================
#  Kiosk Manager - Unified Setup Script
#  Installs the web dashboard + kiosk display (X11/Openbox/Chrome)
#  Supports: Ubuntu/Debian, Fedora/RHEL/CentOS, Arch Linux
#  Run as root or with sudo: sudo bash install.sh
# ============================================================

set -e

# ── Ensure sbin paths are in PATH (minimal installs may lack them) ──
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()    { echo -e "${GREEN}[+]${NC} $1"; }
warn()   { echo -e "${YELLOW}[!]${NC} $1"; }
error()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info()   { echo -e "${CYAN}[i]${NC} $1"; }
header() { echo -e "\n${BLUE}══════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}══════════════════════════════════════${NC}"; }

# ── Must run as root ─────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    error "Please run as root: sudo bash install.sh"
fi

# ── Pick the kiosk user ──────────────────────────────────────
# Resolution order:
#   1. KIOSK_USER env var (explicit override)
#   2. SUDO_USER (when invoked via `sudo bash install.sh` as a normal user)
#   3. DEFAULT_KIOSK_USER fallback (created if missing)
# Root is rejected — Debian disables root tty1 autologin by default, so the
# kiosk would never start.
DEFAULT_KIOSK_USER="technolink"
REAL_USER="${KIOSK_USER:-${SUDO_USER:-}}"
if [ -z "$REAL_USER" ] || [ "$REAL_USER" = "root" ]; then
    REAL_USER="$DEFAULT_KIOSK_USER"
fi
if [ "$REAL_USER" = "root" ]; then
    error "Refusing to set up kiosk for root — root cannot autologin on tty1."
fi

# Create the kiosk user if it doesn't exist (minimal Debian may not have one).
# Use an empty password so tty1 autologin works; the user can set a password
# later with 'sudo passwd <user>'. Add to sudo for fleet maintenance.
if ! id "$REAL_USER" &>/dev/null; then
    log "Creating user '$REAL_USER'..."
    useradd -m -s /bin/bash "$REAL_USER"
    passwd -d "$REAL_USER" >/dev/null 2>&1 || true
fi
if ! id -nG "$REAL_USER" | tr ' ' '\n' | grep -qx sudo; then
    usermod -aG sudo "$REAL_USER" 2>/dev/null || usermod -aG wheel "$REAL_USER" 2>/dev/null || true
fi

REAL_HOME=$(eval echo "~$REAL_USER")
if [ ! -d "$REAL_HOME" ]; then
    error "User '$REAL_USER' has no home directory ($REAL_HOME)."
fi

# ── Config ───────────────────────────────────────────────────
INSTALL_DIR="/opt/kiosk-manager"
SERVICE_USER="$REAL_USER"
PORT=80
KIOSK_URL="https://www.google.com"

# ══════════════════════════════════════════════════════════════
#  STEP 0: Detect system
# ══════════════════════════════════════════════════════════════
header "Detecting system"

DISTRO="unknown"
PKG_MGR="none"
DISPLAY_SERVER="none"

# Detect distro
if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO=$(echo "$ID" | tr '[:upper:]' '[:lower:]')
fi

# Detect package manager
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
fi

# Detect display server (informational + chooses which packages to install)
DISPLAY_SERVER="none"
if [ -n "$WAYLAND_DISPLAY" ] || [ "$XDG_SESSION_TYPE" = "wayland" ]; then
    DISPLAY_SERVER="wayland"
elif [ -n "$DISPLAY" ] || [ "$XDG_SESSION_TYPE" = "x11" ]; then
    DISPLAY_SERVER="x11"
elif systemctl is-active display-manager &>/dev/null 2>&1; then
    DISPLAY_SERVER="x11"
fi

# This is a kiosk installer — kiosk display setup is the default.
# Set SKIP_KIOSK_DISPLAY=1 if you're installing the dashboard on a machine
# that should keep its own desktop (e.g., a dev workstation).
HEADLESS=true
if [ "${SKIP_KIOSK_DISPLAY:-0}" = "1" ]; then
    HEADLESS=false
    warn "SKIP_KIOSK_DISPLAY=1 — kiosk display setup will be skipped"
fi

# Kiosk display setup runs on headless OR if .xinitrc already exists (re-install)
TOTAL_STEPS=7
if [ "$HEADLESS" = true ] || [ -f "$REAL_HOME/.xinitrc" ]; then
    TOTAL_STEPS=8
fi

log "Distro:          $DISTRO (${PRETTY_NAME:-$DISTRO})"
log "Package manager: $PKG_MGR"
log "Display server:  $DISPLAY_SERVER"
if [ "$HEADLESS" = true ]; then
    log "Kiosk mode:      ${GREEN}ON${NC} — will install X11 + Openbox + Chrome kiosk (SKIP_KIOSK_DISPLAY=1 to opt out)"
else
    log "Kiosk mode:      ${YELLOW}OFF${NC} (SKIP_KIOSK_DISPLAY=1 was set)"
fi
log "Installing as:   $SERVICE_USER"
log "Install dir:     $INSTALL_DIR"
log "Port:            $PORT"
echo ""

[ "$PKG_MGR" = "none" ] && error "No supported package manager found (apt/dnf/yum/pacman)"

# ══════════════════════════════════════════════════════════════
#  STEP 1: Install system packages
# ══════════════════════════════════════════════════════════════
header "Step 1/$TOTAL_STEPS — Installing system packages"

install_packages() {
    case "$PKG_MGR" in
        apt)
            apt-get update -qq
            # Core: sudo, python stack, git, sqlite, curl, certs (minimal Debian
            # may lack some of these), python-is-python3 for scripts that use
            # bare `python`.
            apt-get install -y \
                sudo ca-certificates \
                python3 python3-pip python3-venv python-is-python3 \
                git sqlite3 curl wget unzip procps iproute2
            apt-get install -y network-manager 2>/dev/null || true
            apt-get install -y systemd-timesyncd libnss3-tools 2>/dev/null || true
            # Networking + audio tools (audio for kiosk media, ethtool for WoL).
            apt-get install -y ethtool net-tools 2>/dev/null || true
            apt-get install -y alsa-utils pulseaudio pavucontrol 2>/dev/null || true
            if [ "$HEADLESS" = true ]; then
                # CLI system → install X11 + Openbox + browser + kiosk tools.
                # Install in separate commands so one missing package can't
                # silently skip the rest (xrandr comes from x11-xserver-utils
                # and is required for the Display page to work).
                log "Installing X11, Openbox, Chromium, and kiosk tools..."
                apt-get install -y xorg openbox || true
                apt-get install -y x11-xserver-utils || true
                apt-get install -y scrot || true
                apt-get install -y chromium 2>/dev/null || \
                    apt-get install -y chromium-browser 2>/dev/null || true
            else
                # Already has a display
                if [ "$DISPLAY_SERVER" = "wayland" ]; then
                    apt-get install -y grim 2>/dev/null || apt-get install -y gnome-screenshot 2>/dev/null || true
                    apt-get install -y wlr-randr 2>/dev/null || true
                else
                    apt-get install -y scrot 2>/dev/null || apt-get install -y gnome-screenshot 2>/dev/null || true
                    apt-get install -y x11-xserver-utils 2>/dev/null || true
                fi
                apt-get install -y chromium-browser 2>/dev/null || apt-get install -y chromium 2>/dev/null || true
            fi
            ;;
        dnf|yum)
            $PKG_MGR install -y sudo python3 python3-pip git sqlite curl
            $PKG_MGR install -y NetworkManager 2>/dev/null || true
            $PKG_MGR install -y systemd-timesyncd nss-tools 2>/dev/null || true
            if [ "$HEADLESS" = true ]; then
                log "Installing X11, Openbox, Chromium, and kiosk tools..."
                $PKG_MGR install -y xorg-x11-server-Xorg xorg-x11-xinit openbox 2>/dev/null || true
                $PKG_MGR install -y scrot xrandr 2>/dev/null || true
                $PKG_MGR install -y chromium 2>/dev/null || true
            else
                if [ "$DISPLAY_SERVER" = "wayland" ]; then
                    $PKG_MGR install -y grim 2>/dev/null || $PKG_MGR install -y gnome-screenshot 2>/dev/null || true
                    $PKG_MGR install -y wlr-randr 2>/dev/null || true
                else
                    $PKG_MGR install -y scrot 2>/dev/null || $PKG_MGR install -y gnome-screenshot 2>/dev/null || true
                    $PKG_MGR install -y xrandr 2>/dev/null || $PKG_MGR install -y xorg-x11-server-utils 2>/dev/null || true
                fi
                $PKG_MGR install -y chromium 2>/dev/null || $PKG_MGR install -y chromium-browser 2>/dev/null || true
            fi
            ;;
        pacman)
            pacman -Sy --noconfirm sudo python python-pip git sqlite curl
            pacman -S --noconfirm networkmanager 2>/dev/null || true
            pacman -S --noconfirm nss 2>/dev/null || true
            if [ "$HEADLESS" = true ]; then
                log "Installing X11, Openbox, Chromium, and kiosk tools..."
                pacman -S --noconfirm xorg-server xorg-xinit openbox 2>/dev/null || true
                pacman -S --noconfirm scrot xorg-xrandr 2>/dev/null || true
                pacman -S --noconfirm chromium 2>/dev/null || true
            else
                if [ "$DISPLAY_SERVER" = "wayland" ]; then
                    pacman -S --noconfirm grim 2>/dev/null || true
                    pacman -S --noconfirm wlr-randr 2>/dev/null || true
                else
                    pacman -S --noconfirm scrot 2>/dev/null || true
                    pacman -S --noconfirm xorg-xrandr 2>/dev/null || true
                fi
                pacman -S --noconfirm chromium 2>/dev/null || true
            fi
            ;;
    esac
}

install_packages

# Verify critical tools
command -v python3 &>/dev/null || error "python3 not found after install"
command -v git &>/dev/null || error "git not found after install"

# Find browser
BROWSER=""
for b in chromium chromium-browser google-chrome google-chrome-stable; do
    if command -v "$b" &>/dev/null; then
        BROWSER="$b"
        break
    fi
done
if [ -n "$BROWSER" ]; then
    log "Browser: $BROWSER"
else
    warn "No Chromium/Chrome browser found — install manually for kiosk mode"
fi

log "System packages installed"

# ══════════════════════════════════════════════════════════════
#  STEP 2: Copy project files
# ══════════════════════════════════════════════════════════════
header "Step 2/$TOTAL_STEPS — Copying project files"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" = "$INSTALL_DIR" ]; then
    log "Already running from $INSTALL_DIR — skipping copy"
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "Directory $INSTALL_DIR exists — backing up to ${INSTALL_DIR}.bak"
        mv "$INSTALL_DIR" "${INSTALL_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    fi
    cp -r "$SCRIPT_DIR" "$INSTALL_DIR"
    log "Files copied to $INSTALL_DIR"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ══════════════════════════════════════════════════════════════
#  STEP 3: Python virtualenv + dependencies
# ══════════════════════════════════════════════════════════════
header "Step 3/$TOTAL_STEPS — Setting up Python virtualenv"

sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet gunicorn
log "Python dependencies installed (including gunicorn)"

# ══════════════════════════════════════════════════════════════
#  STEP 4: Create data directory
# ══════════════════════════════════════════════════════════════
header "Step 4/$TOTAL_STEPS — Creating data directory"
mkdir -p "$INSTALL_DIR/data/screenshots"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/data"
log "Data directory ready"

# ══════════════════════════════════════════════════════════════
#  STEP 5: Auto-detect sudoers (dynamic binary paths)
# ══════════════════════════════════════════════════════════════
header "Step 5/$TOTAL_STEPS — Configuring sudoers & USB auto-mount"

SUDOERS_FILE="/etc/sudoers.d/kiosk-manager"
echo "# Kiosk Manager sudoers — auto-generated $(date)" > "$SUDOERS_FILE"
echo "Defaults:$SERVICE_USER !requiretty" >> "$SUDOERS_FILE"
echo "" >> "$SUDOERS_FILE"

add_sudoers_entry() {
    local bin_path
    bin_path=$(command -v "$1" 2>/dev/null || true)
    if [ -n "$bin_path" ]; then
        if [ -n "$2" ]; then
            echo "$SERVICE_USER ALL=(ALL) NOPASSWD: $bin_path $2" >> "$SUDOERS_FILE"
            info "  sudoers: $bin_path $2"
        else
            echo "$SERVICE_USER ALL=(ALL) NOPASSWD: $bin_path" >> "$SUDOERS_FILE"
            info "  sudoers: $bin_path (any args)"
        fi
    fi
}

add_sudoers_entry reboot ""
add_sudoers_entry poweroff ""
add_sudoers_entry systemctl "reboot"
add_sudoers_entry systemctl "poweroff"
add_sudoers_entry systemctl "restart kiosk-manager"
add_sudoers_entry nmcli ""
add_sudoers_entry timedatectl ""
add_sudoers_entry hostnamectl ""

# Network config: ifupdown support
add_sudoers_entry ifdown ""
add_sudoers_entry ifup ""

# Wake-on-LAN configuration
add_sudoers_entry ethtool ""

# File writing: tee and cp for /etc/network/interfaces, /etc/environment
add_sudoers_entry tee ""
add_sudoers_entry cp ""

# Offline USB updates: mount/umount a flash drive so usb-update.sh can read the
# release .zip on kiosks with no internet — all as the non-root service user.
add_sudoers_entry mount ""
add_sudoers_entry umount ""

# Display sudoers entries (always add since we install X11 on headless too)
add_sudoers_entry xrandr ""
add_sudoers_entry wlr-randr ""
add_sudoers_entry scrot ""
add_sudoers_entry grim ""
add_sudoers_entry gnome-screenshot ""

for alt_bin in /sbin/reboot /usr/sbin/reboot; do
    if [ -x "$alt_bin" ]; then
        echo "$SERVICE_USER ALL=(ALL) NOPASSWD: $alt_bin" >> "$SUDOERS_FILE"
    fi
done
for alt_bin in /sbin/poweroff /usr/sbin/poweroff; do
    if [ -x "$alt_bin" ]; then
        echo "$SERVICE_USER ALL=(ALL) NOPASSWD: $alt_bin" >> "$SUDOERS_FILE"
    fi
done

chmod 440 "$SUDOERS_FILE"

VISUDO_BIN=$(command -v visudo 2>/dev/null || echo "/usr/sbin/visudo")
if [ -x "$VISUDO_BIN" ]; then
    if $VISUDO_BIN -cf "$SUDOERS_FILE" &>/dev/null; then
        log "Sudoers configured at $SUDOERS_FILE"
    else
        error "Sudoers file has syntax errors! Check $SUDOERS_FILE"
    fi
else
    warn "visudo not found — skipping syntax check (sudoers file written to $SUDOERS_FILE)"
fi

# ── USB auto-mount ───────────────────────────────────────────
# A bare X session has no udisks2/gvfs, so nothing mounts a flash drive when
# it's plugged in — and Chrome's file picker can only browse MOUNTED
# filesystems, so the dashboard's Offline Update dialog showed no USB at all.
# usb-automount.sh installs a udev rule that fixes that. Non-fatal: a kiosk
# without it still updates from a console via usb-update.sh.
if [ -f "$INSTALL_DIR/usb-automount.sh" ]; then
    if bash "$INSTALL_DIR/usb-automount.sh"; then
        log "USB auto-mount configured — sticks appear under /media/"
    else
        warn "USB auto-mount could not be configured (see message above)"
    fi
else
    warn "usb-automount.sh not found in $INSTALL_DIR — skipping USB auto-mount"
fi

# ══════════════════════════════════════════════════════════════
#  STEP 6: systemd services
# ══════════════════════════════════════════════════════════════
header "Step 6/$TOTAL_STEPS — Installing systemd service"

SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# For headless systems, we set DISPLAY=:0 since we install X11
KIOSK_DISPLAY_SERVER="${DISPLAY_SERVER}"
if [ "$HEADLESS" = true ]; then
    KIOSK_DISPLAY_SERVER="x11"
fi

cat > /etc/systemd/system/kiosk-manager.service <<EOF
[Unit]
Description=Kiosk Manager Dashboard
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - wsgi:app
Restart=always
RestartSec=5
# Allow non-root $SERVICE_USER to bind privileged ports (e.g. :80).
# Do NOT also set CapabilityBoundingSet here — that would strip CAP_SETUID/
# CAP_SETGID from sudo invocations and break every privileged action.
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u $SERVICE_USER)
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u $SERVICE_USER)/bus
Environment=KIOSK_SECRET_KEY=$SECRET_KEY
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_SESSION_TYPE=$KIOSK_DISPLAY_SERVER
Environment=XAUTHORITY=$REAL_HOME/.Xauthority

[Install]
WantedBy=multi-user.target
EOF

# Resolution persistence service (only for systems with an existing display manager)
if [ "$HEADLESS" = false ] && [ -f "$INSTALL_DIR/deploy/apply-resolution.sh" ]; then
    chmod +x "$INSTALL_DIR/deploy/apply-resolution.sh"
    cat > /etc/systemd/system/kiosk-resolution.service <<EOF
[Unit]
Description=Apply saved display resolution on boot
After=display-manager.service

[Service]
Type=oneshot
User=$SERVICE_USER
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_SESSION_TYPE=$DISPLAY_SERVER
Environment=XAUTHORITY=$REAL_HOME/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u $SERVICE_USER)
ExecStart=$INSTALL_DIR/deploy/apply-resolution.sh
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
EOF
    systemctl enable kiosk-resolution 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable kiosk-manager
systemctl start kiosk-manager
log "systemd services installed and started"

# ══════════════════════════════════════════════════════════════
#  STEP 7: Firewall
# ══════════════════════════════════════════════════════════════
header "Step 7/$TOTAL_STEPS — Firewall"
if command -v ufw &>/dev/null; then
    ufw allow "$PORT"/tcp 2>/dev/null || true
    log "ufw: port $PORT opened"
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="$PORT/tcp" 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    log "firewalld: port $PORT opened"
else
    warn "No firewall tool found — open port $PORT manually if needed"
fi

# ══════════════════════════════════════════════════════════════
#  STEP 8: Kiosk Display Setup
#  Runs on first install (headless) AND on re-installs where
#  .xinitrc already exists to keep it up-to-date.
# ══════════════════════════════════════════════════════════════
KIOSK_SETUP=false
if [ "$HEADLESS" = true ]; then
    KIOSK_SETUP=true
elif [ -f "$REAL_HOME/.xinitrc" ]; then
    KIOSK_SETUP=true
    log "Existing .xinitrc detected — will update kiosk display config"
fi

if [ "$KIOSK_SETUP" = true ]; then
    header "Step 8/$TOTAL_STEPS — Kiosk Display Setup (X11 + Chrome auto-launch)"

    [ -z "$BROWSER" ] && error "No browser found — cannot set up kiosk display"

    # ── 8a: Auto-login on tty1 (only on first headless setup) ─
    if [ "$HEADLESS" = true ]; then
        log "Configuring auto-login on tty1..."
        mkdir -p /etc/systemd/system/getty@tty1.service.d
        cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $SERVICE_USER --noclear %I \$TERM
EOF
        log "Auto-login configured for $SERVICE_USER on tty1"
    fi

    # ── 8b: Create/update .xinitrc ───────────────────────────
    log "Creating .xinitrc..."
    cat > "$REAL_HOME/.xinitrc" <<'XINITRC'
#!/bin/bash
# Kiosk X11 startup — auto-generated by install.sh
LOGFILE="$HOME/.kiosk-x11.log"
exec >> "$LOGFILE" 2>&1
echo "=== Kiosk X11 starting at $(date) ==="

# Crash guard
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

# Disable screen saver and power management
xset s off
xset -dpms
xset s noblank

# Hide cursor at boot (persistent process keeps XFixes connection alive)
python3 /opt/kiosk-manager/hide_cursor.py &

# Start openbox window manager
openbox-session &
WM_PID=$!
sleep 2

# Build Chrome flags
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

# Running as root requires --no-sandbox; --test-type suppresses the warning bar
if [ "$(id -u)" = "0" ]; then
    CHROME_FLAGS+=(--no-sandbox --test-type)
fi

# GPU flags: ONLY disable the GPU inside a virtual machine. On real hardware
# this pair (especially --disable-software-rasterizer) leaves Chrome with no
# renderer at all, so it paints once and exits ~2s after launch — and the
# relaunch loop below turns that into an unrecoverable "loading page keeps
# restarting" lockout. A real PC should use its GPU.
if systemd-detect-virt --quiet 2>/dev/null; then
    CHROME_FLAGS+=(--disable-gpu --disable-software-rasterizer)
fi

# Always start with the loading page
LOADING_URL="http://localhost:__PORT__/kiosk/loading"

# Launch Chrome in a loop
while true; do
    # Clean crash state to prevent "Restore pages?" dialog
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
    # If Chrome died almost immediately it is crash-looping. Record the time and
    # back off 30s instead of relaunching every 2s, so the screen stays usable
    # and reachable (you can still tap the logo / switch to a console) instead
    # of hard-locking on a ~2s reload forever.
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

    # Save default kiosk URL
    echo "$KIOSK_URL" > "$REAL_HOME/.kiosk-url"
    chown "$SERVICE_USER":"$SERVICE_USER" "$REAL_HOME/.kiosk-url"

    log "Created $REAL_HOME/.xinitrc with $BROWSER"

    # ── 8c: Auto-start X on login ────────────────────────────
    log "Configuring auto-startx..."
    BASH_PROFILE="$REAL_HOME/.bash_profile"

    # Remove any existing kiosk auto-start block
    if [ -f "$BASH_PROFILE" ]; then
        sed -i '/# Auto-start X11 on tty1/,/^fi$/d' "$BASH_PROFILE" 2>/dev/null || true
        sed -i '/exec startx/d' "$BASH_PROFILE" 2>/dev/null || true
        sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$BASH_PROFILE" 2>/dev/null || true
    fi

    cat >> "$BASH_PROFILE" <<'PROFILE'

# Auto-start X11 on tty1 (kiosk mode)
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
PROFILE

    chown "$SERVICE_USER":"$SERVICE_USER" "$BASH_PROFILE"
    log "Auto-startx configured in $BASH_PROFILE"

    # ── 8d: Set boot target ──────────────────────────────────
    systemctl set-default multi-user.target
    systemctl daemon-reload
    log "Kiosk display setup complete"
fi

# ══════════════════════════════════════════════════════════════
#  DONE — Print summary
# ══════════════════════════════════════════════════════════════
MACHINE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       Kiosk Manager installed successfully!      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}System detected:${NC}"
echo -e "    Distro:   ${BLUE}${PRETTY_NAME:-$DISTRO}${NC}"
if [ "$KIOSK_SETUP" = true ]; then
    echo -e "    Display:  ${BLUE}X11 (kiosk mode)${NC}"
    echo -e "    Browser:  ${BLUE}${BROWSER}${NC}"
    echo -e "    Mode:     ${BLUE}Kiosk — Chrome auto-launches on boot${NC}"
else
    echo -e "    Display:  ${BLUE}$DISPLAY_SERVER${NC}"
fi
echo -e "    Packages: ${BLUE}$PKG_MGR${NC}"
echo ""
echo -e "  ${CYAN}Access:${NC}"
echo -e "    Local:    ${BLUE}http://localhost:${PORT}${NC}"
echo -e "    Network:  ${BLUE}http://${MACHINE_IP}:${PORT}${NC}"
echo ""
echo -e "  ${CYAN}Manage:${NC}"
echo -e "    Status:   ${YELLOW}sudo systemctl status kiosk-manager${NC}"
echo -e "    Logs:     ${YELLOW}sudo journalctl -u kiosk-manager -f${NC}"
echo -e "    Restart:  ${YELLOW}sudo systemctl restart kiosk-manager${NC}"
echo ""
if [ "$KIOSK_SETUP" = true ]; then
    echo -e "  ${CYAN}Kiosk Display:${NC}"
    echo -e "    - Auto-login as ${BLUE}$SERVICE_USER${NC} on tty1"
    echo -e "    - Chrome opens loading page → redirects to kiosk URL"
    echo -e "    - Kiosk URL: ${BLUE}$KIOSK_URL${NC}"
    echo -e "    - Remote debugging: ${BLUE}http://${MACHINE_IP}:9222${NC}"
    echo ""
    echo -e "  ${CYAN}Next step:${NC}"
    echo -e "    ${YELLOW}sudo reboot${NC}"
    echo ""
    echo -e "  ${CYAN}Tips:${NC}"
    echo -e "    - Press ${YELLOW}Ctrl+Alt+F2${NC} to switch to a second terminal"
    echo -e "    - Press ${YELLOW}Ctrl+Alt+F1${NC} to switch back to kiosk display"
    echo -e "    - Press ${YELLOW}F11${NC} to exit fullscreen in Chrome"
    echo ""
fi
