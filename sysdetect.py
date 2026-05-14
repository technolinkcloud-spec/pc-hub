"""
System Detection Module
Auto-detects OS, display server, and available tools at startup.
All routes import from here instead of hardcoding binary paths.
"""

import os
import platform
import shutil
import subprocess


class SystemInfo:
    """Cached system detection — computed once at startup."""

    def __init__(self):
        self._cache = {}
        self._detect()

    def _detect(self):
        self._cache['os'] = platform.system().lower()           # 'linux', 'darwin'
        self._cache['arch'] = platform.machine()                 # 'x86_64', 'aarch64'
        self._cache['distro'] = self._detect_distro()
        self._cache['display_server'] = self._detect_display()
        self._cache['init_system'] = self._detect_init()
        self._cache['bins'] = self._detect_binaries()
        self._cache['net_backend'] = self._detect_net_backend()

    # ── OS / Distro ──────────────────────────────────────────

    def _detect_distro(self):
        """Return distro id like 'ubuntu', 'debian', 'fedora', 'arch', or 'unknown'."""
        if self.is_macos:
            return 'macos'
        try:
            with open('/etc/os-release') as f:
                for line in f:
                    if line.startswith('ID='):
                        return line.strip().split('=')[1].strip('"').lower()
        except FileNotFoundError:
            pass
        return 'unknown'

    # ── Display server ───────────────────────────────────────

    def _detect_display(self):
        """Detect X11, Wayland, or none."""
        xdg = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if xdg == 'wayland':
            return 'wayland'
        if xdg == 'x11':
            return 'x11'
        if os.environ.get('WAYLAND_DISPLAY'):
            return 'wayland'
        if os.environ.get('DISPLAY'):
            return 'x11'
        return 'none'

    # ── Init system ──────────────────────────────────────────

    def _detect_init(self):
        if shutil.which('systemctl'):
            return 'systemd'
        if os.path.exists('/sbin/openrc'):
            return 'openrc'
        return 'unknown'

    # ── Network backend ──────────────────────────────────────

    def _detect_net_backend(self):
        """Detect which network manager is available."""
        if shutil.which('nmcli'):
            return 'networkmanager'
        if shutil.which('netplan'):
            return 'netplan'
        if os.path.exists('/etc/network/interfaces'):
            return 'ifupdown'
        if self.is_macos:
            return 'macos'
        return 'none'

    # ── Binary detection ─────────────────────────────────────

    def _detect_binaries(self):
        """Find paths for all tools we might need."""
        tools = [
            # browsers
            'chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable',
            # display
            'xrandr', 'wlr-randr', 'gnome-randr',
            # screenshot
            'scrot', 'gnome-screenshot', 'grim', 'screencapture',
            # network
            'ip', 'nmcli', 'netplan', 'ifconfig', 'networksetup',
            'hostnamectl', 'hostname',
            # system
            'reboot', 'poweroff', 'shutdown', 'systemctl',
            'free', 'df', 'grep', 'cat', 'uname', 'uptime', 'lsb_release',
            # time
            'timedatectl', 'date',
            # diagnostics
            'ping', 'pgrep', 'pkill',
            # general
            'sudo', 'git',
        ]
        return {t: shutil.which(t) for t in tools if shutil.which(t)}

    # ── Convenience properties ───────────────────────────────

    @property
    def is_linux(self):
        return self._cache['os'] == 'linux'

    @property
    def is_macos(self):
        return self._cache['os'] == 'darwin'

    @property
    def has_display(self):
        """True if a display server (X11/Wayland) is available."""
        return self.display_server in ('x11', 'wayland') or self.is_macos

    @property
    def is_headless(self):
        """True if running without a display server (server/CLI only)."""
        return not self.has_display

    @property
    def distro(self):
        return self._cache['distro']

    @property
    def display_server(self):
        return self._cache['display_server']

    @property
    def init_system(self):
        return self._cache['init_system']

    @property
    def net_backend(self):
        return self._cache['net_backend']

    def has(self, binary):
        """Check if a binary is available."""
        return binary in self._cache['bins']

    def bin(self, binary):
        """Get full path to a binary, or None."""
        return self._cache['bins'].get(binary)

    # ── High-level tool selectors ────────────────────────────

    def get_browser(self):
        """Return the best available browser binary path."""
        for name in ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable']:
            if self.has(name):
                return self.bin(name)
        return None

    def get_screenshot_cmd(self, output_path):
        """Return a command list to capture a screenshot to output_path."""
        env = os.environ.copy()
        env['DISPLAY'] = env.get('DISPLAY', ':0')

        if self.display_server == 'wayland' and self.has('grim'):
            return [self.bin('grim'), output_path], env

        if self.has('scrot'):
            return [self.bin('scrot'), output_path], env

        if self.has('gnome-screenshot'):
            return [self.bin('gnome-screenshot'), '-f', output_path], env

        if self.is_macos and self.has('screencapture'):
            return [self.bin('screencapture'), '-x', output_path], env

        return None, env

    def get_display_cmd(self):
        """Return the display resolution tool and type."""
        if self.display_server == 'wayland':
            if self.has('wlr-randr'):
                return self.bin('wlr-randr'), 'wlr-randr'
            if self.has('gnome-randr'):
                return self.bin('gnome-randr'), 'gnome-randr'
        if self.has('xrandr'):
            return self.bin('xrandr'), 'xrandr'
        return None, None

    def get_reboot_cmd(self):
        """Return the reboot command list."""
        if self.has('systemctl'):
            return ['sudo', self.bin('systemctl'), 'reboot']
        if self.has('reboot'):
            return ['sudo', self.bin('reboot')]
        if self.is_macos:
            return ['sudo', 'shutdown', '-r', 'now']
        return None

    def get_shutdown_cmd(self):
        """Return the shutdown command list."""
        if self.has('systemctl'):
            return ['sudo', self.bin('systemctl'), 'poweroff']
        if self.has('poweroff'):
            return ['sudo', self.bin('poweroff')]
        if self.is_macos:
            return ['sudo', 'shutdown', '-h', 'now']
        return None

    def get_ping_cmd(self, host, count=4):
        """Return ping command — flags differ between Linux and macOS."""
        ping_bin = self.bin('ping') or 'ping'
        return [ping_bin, '-c', str(count), host]

    def get_memory_info(self):
        """Return memory info dict, cross-platform."""
        if self.is_linux and self.has('free'):
            try:
                r = subprocess.run([self.bin('free'), '-h'], capture_output=True, text=True, timeout=5)
                lines = r.stdout.strip().split('\n')
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 3:
                        return {'total': parts[1], 'used': parts[2], 'free': parts[3] if len(parts) > 3 else 'N/A'}
            except Exception:
                pass
        if self.is_macos:
            try:
                r = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5)
                total_bytes = int(r.stdout.strip())
                total_gb = round(total_bytes / (1024**3), 1)
                return {'total': f'{total_gb}G', 'used': 'N/A', 'free': 'N/A'}
            except Exception:
                pass
        return {}

    def get_disk_info(self, path='/'):
        """Return disk info dict, cross-platform."""
        if self.has('df'):
            try:
                r = subprocess.run([self.bin('df'), '-h', path], capture_output=True, text=True, timeout=5)
                lines = r.stdout.strip().split('\n')
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 5:
                        return {'total': parts[1], 'used': parts[2], 'free': parts[3], 'percent': parts[4]}
            except Exception:
                pass
        return {}

    def get_cpu_cores(self):
        """Return CPU core count, cross-platform."""
        try:
            return str(os.cpu_count() or 'N/A')
        except Exception:
            return 'N/A'

    def get_load_average(self):
        """Return load average string, cross-platform."""
        try:
            load = os.getloadavg()
            return f'{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}'
        except (OSError, AttributeError):
            return 'N/A'

    def get_primary_ip(self):
        """Get primary IP address, cross-platform."""
        if self.is_linux:
            try:
                r = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
                ips = r.stdout.strip().split()
                if ips:
                    return ips[0]
            except Exception:
                pass
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return 'N/A'

    def get_hostname(self):
        """Get hostname, cross-platform."""
        try:
            return platform.node() or 'N/A'
        except Exception:
            return 'N/A'

    def get_os_string(self):
        """Return a human-readable OS string."""
        if self.is_linux:
            try:
                r = subprocess.run(['lsb_release', '-d', '-s'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            except Exception:
                pass
            try:
                with open('/etc/os-release') as f:
                    for line in f:
                        if line.startswith('PRETTY_NAME='):
                            return line.strip().split('=', 1)[1].strip('"')
            except Exception:
                pass
        return platform.platform()

    def get_uptime(self):
        """Return uptime string, cross-platform."""
        if self.is_linux:
            try:
                r = subprocess.run(['uptime', '-p'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:
                pass
        if self.is_macos:
            try:
                r = subprocess.run(['uptime'], capture_output=True, text=True, timeout=5)
                return r.stdout.strip()
            except Exception:
                pass
        return 'N/A'

    def get_kernel(self):
        """Return kernel version."""
        return platform.release()

    def get_env_with_display(self):
        """Return env dict with DISPLAY and XAUTHORITY discovered from the live
        X session. Falls back to the service's own env if X isn't running yet."""
        env = os.environ.copy()
        x_display, x_auth, w_display, xdg_runtime = _read_x_session_env()
        if x_display:
            env['DISPLAY'] = x_display
        else:
            env.setdefault('DISPLAY', ':0')
        if x_auth and os.path.exists(x_auth):
            env['XAUTHORITY'] = x_auth
        elif 'XAUTHORITY' not in env or not os.path.exists(env['XAUTHORITY']):
            # Probe common locations
            uid = os.getuid()
            candidates = [
                os.path.expanduser('~/.Xauthority'),
                f'/run/user/{uid}/gdm/Xauthority',
                f'/tmp/.Xauthority-{uid}',
            ]
            # Also probe the kiosk user's home (when service runs as them)
            try:
                import pwd
                for entry in pwd.getpwall():
                    if 1000 <= entry.pw_uid < 65000:
                        candidates.append(os.path.join(entry.pw_dir, '.Xauthority'))
            except Exception:
                pass
            for c in candidates:
                if c and os.path.exists(c):
                    env['XAUTHORITY'] = c
                    break
        if w_display:
            env['WAYLAND_DISPLAY'] = w_display
        elif self.display_server == 'wayland':
            env.setdefault('WAYLAND_DISPLAY', 'wayland-0')
        if xdg_runtime:
            env['XDG_RUNTIME_DIR'] = xdg_runtime
        elif 'XDG_RUNTIME_DIR' not in env:
            uid = os.getuid()
            xdg_dir = f'/run/user/{uid}'
            if os.path.isdir(xdg_dir):
                env['XDG_RUNTIME_DIR'] = xdg_dir
        return env

    def summary(self):
        """Return a summary dict for diagnostics / dashboard."""
        return {
            'os': self._cache['os'],
            'distro': self.distro,
            'arch': self._cache['arch'],
            'display_server': self.display_server,
            'has_display': self.has_display,
            'init_system': self.init_system,
            'net_backend': self.net_backend,
            'browser': self.get_browser() or 'not found',
            'available_tools': list(self._cache['bins'].keys()),
        }


# ── X session discovery ──────────────────────────────────────

def _read_x_session_env():
    """Find the running X/Wayland session's env by reading /proc/<pid>/environ.
    Returns (DISPLAY, XAUTHORITY, WAYLAND_DISPLAY, XDG_RUNTIME_DIR), any of
    which may be None if not discoverable.
    """
    display = xauth = wayland = xdg_runtime = None
    if not os.path.isdir('/proc'):
        return display, xauth, wayland, xdg_runtime
    # Look at common display server process names. Walk /proc cheaply.
    targets = ('Xorg', 'Xwayland', 'gnome-shell', 'startplasma',
               'sway', 'weston', 'mutter', 'kwin_wayland', 'kwin_x11',
               'openbox')
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            comm_path = f'/proc/{pid}/comm'
            try:
                with open(comm_path) as f:
                    comm = f.read().strip()
            except (FileNotFoundError, PermissionError):
                continue
            if comm not in targets:
                continue
            env_path = f'/proc/{pid}/environ'
            try:
                with open(env_path, 'rb') as f:
                    raw = f.read()
            except (FileNotFoundError, PermissionError):
                continue
            for chunk in raw.split(b'\0'):
                if b'=' not in chunk:
                    continue
                key, _, val = chunk.partition(b'=')
                try:
                    k = key.decode('ascii', errors='replace')
                    v = val.decode('utf-8', errors='replace')
                except Exception:
                    continue
                if k == 'DISPLAY' and not display:
                    display = v
                elif k == 'XAUTHORITY' and not xauth:
                    xauth = v
                elif k == 'WAYLAND_DISPLAY' and not wayland:
                    wayland = v
                elif k == 'XDG_RUNTIME_DIR' and not xdg_runtime:
                    xdg_runtime = v
            if display and xauth:
                break
    except Exception:
        pass
    return display, xauth, wayland, xdg_runtime


# ── Singleton ────────────────────────────────────────────────
_instance = None

def get_sys():
    """Get the cached SystemInfo singleton."""
    global _instance
    if _instance is None:
        _instance = SystemInfo()
    return _instance
