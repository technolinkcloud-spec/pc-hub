import subprocess
import signal
import os
import re
import json
import threading
import time
import logging
import urllib.request
import urllib.error
import websocket as ws_client
from flask import Blueprint, render_template, request, jsonify, make_response
from auth_utils import login_required
from database import get_setting, set_setting
from config import BIND_PORT

logger = logging.getLogger(__name__)
from sysdetect import get_sys

kiosk_bp = Blueprint('kiosk', __name__)

_chromium_process = None
_watchdog_thread = None
_watchdog_running = False


def _find_chromium():
    """Find chromium binary via sysdetect."""
    return get_sys().get_browser() or 'chromium-browser'


def _is_url_reachable(url, timeout=5):
    """Check if a URL is reachable."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _get_kiosk_pid():
    """Get the PID of the running Chromium kiosk process."""
    global _chromium_process
    if _chromium_process and _chromium_process.poll() is None:
        return _chromium_process.pid
    # Search for any chromium/chrome process (xinitrc uses --start-fullscreen)
    for pattern in ['chromium', 'chrome']:
        try:
            result = subprocess.run(
                ['pgrep', '-f', pattern],
                capture_output=True, text=True
            )
            pids = result.stdout.strip().split('\n')
            if pids and pids[0]:
                return int(pids[0])
        except Exception:
            pass
    return None


def _xinit_supervisor_present():
    """True when ~/.xinitrc is supervising Chrome (the installer's startx model).

    That loop relaunches Chrome every time it exits, so this app must never
    launch its own instance alongside it. Chromium refuses a second instance on
    the same profile: it hands the URL to the already-running browser
    ("Opening in existing browser session") and exits 0 immediately. The loop
    reads that instant exit as a crash, backs off 30s, and tries again — so the
    screen gets force-navigated to the loading page every 30 seconds forever,
    tearing the operator off the dashboard mid-config.
    """
    try:
        return os.path.isfile(os.path.expanduser('~/.xinitrc'))
    except Exception:
        return False


def _launch_chromium(url=None):
    """Launch Chromium in kiosk mode.

    Only call this when no supervisor owns the display — see
    _xinit_supervisor_present().
    """
    global _chromium_process

    if url is None:
        url = get_setting('kiosk_url', 'https://www.google.com')

    devtools = get_setting('kiosk_devtools', '0') == '1'

    if not _is_url_reachable(url):
        url = f'http://127.0.0.1:{BIND_PORT}/kiosk/error-page'

    chromium = _find_chromium()
    cmd = [
        chromium,
        '--kiosk',
        '--noerrdialogs',
        '--disable-infobars',
        '--no-first-run',
        '--disable-session-crashed-bubble',
        '--disable-features=TranslateUI',
    ]

    if devtools:
        cmd.append('--remote-debugging-port=9222')
        cmd.append('--remote-allow-origins=*')

    # Use sysdetect env which includes DISPLAY, XAUTHORITY, etc.
    env = get_sys().get_env_with_display()

    cmd.append(url)

    logger.info('Launching kiosk: %s', ' '.join(cmd))
    _chromium_process = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _chromium_process.pid


def _watchdog_loop():
    """Watchdog: relaunch Chromium only if the process actually died.

    It must NOT kill a running browser just because the kiosk URL is
    unreachable — that would tear the screen away from whatever is showing
    (including the admin dashboard) every 10s. An unreachable URL is already
    handled gracefully by the loading page's Connection Error screen.

    It must also NOT launch when ~/.xinitrc supervises the display. That loop
    can back off for up to 30s after a kill, and this poll runs every 10s — so
    the watchdog would win the race, claim the Chrome profile, and leave the
    two supervisors fighting. Relaunching is the loop's job; here we only watch.
    """
    global _watchdog_running, _chromium_process
    while _watchdog_running:
        time.sleep(10)
        if not _watchdog_running:
            break
        pid = _get_kiosk_pid()
        if pid is None and _watchdog_running and not _xinit_supervisor_present():
            _launch_chromium()


def _kill_chromium():
    """Kill Chromium process. The xinitrc loop will auto-relaunch it."""
    global _chromium_process
    if _chromium_process and _chromium_process.poll() is None:
        _chromium_process.terminate()
        try:
            _chromium_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _chromium_process.kill()
        _chromium_process = None
    # Also kill any system-level chromium/chrome processes
    for pattern in ['chromium', 'chrome']:
        try:
            subprocess.run(['pkill', '-f', pattern], timeout=5, capture_output=True)
        except Exception:
            pass


@kiosk_bp.route('/')
@login_required
def kiosk_page():
    sys = get_sys()
    if sys.is_headless:
        return render_template('headless.html',
                               feature='Chrome Kiosk',
                               reason='No display server detected (headless mode).')
    settings = {
        'url': get_setting('kiosk_url', 'https://www.google.com'),
        'devtools': get_setting('kiosk_devtools', '0'),
        'watchdog': get_setting('kiosk_watchdog', '1'),
        'cursor': get_setting('kiosk_cursor', '1'),
        'check_timeout': get_setting('kiosk_check_timeout', '5'),
    }
    return render_template('kiosk.html', settings=settings)


@kiosk_bp.route('/loading')
def loading_page():
    """Loading page shown on boot before redirecting to kiosk URL."""
    resp = make_response(render_template('kiosk_loading.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@kiosk_bp.route('/api/netinfo')
def netinfo():
    """Return device network info (IP, subnet, gateway, DNS) — no auth required."""
    info = {'ip': '', 'subnet': '', 'gateway': '', 'dns': ''}
    try:
        # Get default interface IP and subnet
        result = subprocess.run(
            ['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split('\n')[0]
            m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', line)
            if m:
                info['ip'] = m.group(1)
                prefix = int(m.group(2))
                mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                info['subnet'] = f'{(mask>>24)&0xFF}.{(mask>>16)&0xFF}.{(mask>>8)&0xFF}.{mask&0xFF}'

        # Get default gateway
        result = subprocess.run(
            ['ip', 'route', 'show', 'default'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            m = re.search(r'default via\s+(\d+\.\d+\.\d+\.\d+)', result.stdout)
            if m:
                info['gateway'] = m.group(1)

        # Get DNS from resolv.conf
        try:
            with open('/etc/resolv.conf', 'r') as f:
                dns_servers = re.findall(r'nameserver\s+(\d+\.\d+\.\d+\.\d+)', f.read())
                info['dns'] = ', '.join(dns_servers) if dns_servers else ''
        except Exception:
            pass
    except Exception as e:
        logger.warning('Could not get network info: %s', e)
    return jsonify(info)


@kiosk_bp.route('/api/check-url')
def check_url():
    """Check if the configured kiosk URL is reachable — no auth required."""
    url = get_setting('kiosk_url', 'https://www.google.com')
    timeout = int(get_setting('kiosk_check_timeout', '5'))
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return jsonify({'reachable': True, 'url': url, 'status': resp.getcode(),
                        'timeout': timeout})
    except urllib.error.HTTPError as e:
        return jsonify({'reachable': False, 'url': url, 'status': e.code,
                        'error': f'HTTP {e.code}: {e.reason}'})
    except urllib.error.URLError as e:
        return jsonify({'reachable': False, 'url': url, 'status': 0,
                        'error': f'Connection error: {e.reason}'})
    except Exception as e:
        return jsonify({'reachable': False, 'url': url, 'status': 0,
                        'error': str(e)})


@kiosk_bp.route('/error-page')
def error_page():
    return render_template('kiosk_error.html')


@kiosk_bp.route('/api/status')
@login_required
def status():
    if get_sys().is_headless:
        return jsonify({'running': False, 'pid': None, 'headless': True, 'error': 'No display server available'})
    pid = _get_kiosk_pid()
    return jsonify({
        'running': pid is not None,
        'pid': pid,
        'url': get_setting('kiosk_url', 'https://www.google.com'),
        'watchdog': _watchdog_running,
    })


@kiosk_bp.route('/api/launch', methods=['POST'])
@login_required
def launch():
    if get_sys().is_headless:
        return jsonify({'error': 'Cannot launch kiosk in headless mode (no display server)'}), 400
    pid = _get_kiosk_pid()
    if pid:
        return jsonify({'error': 'Chromium is already running', 'pid': pid}), 400
    if _xinit_supervisor_present():
        return jsonify({
            'success': True, 'supervised': True,
            'message': 'The kiosk display loop relaunches Chrome automatically '
                       '(it can wait up to 30s after a crash).',
        })
    new_pid = _launch_chromium()
    return jsonify({'success': True, 'pid': new_pid})


@kiosk_bp.route('/api/restart', methods=['POST'])
@login_required
def restart():
    if get_sys().is_headless:
        return jsonify({'error': 'Cannot restart kiosk in headless mode (no display server)'}), 400
    _kill_chromium()
    # Killing is enough when ~/.xinitrc supervises: its loop relaunches Chrome
    # on the loading page a couple of seconds later. Launching our own instance
    # here used to beat that retry by a second, permanently claiming the Chrome
    # profile and leaving .xinitrc hijacking the screen every 30s.
    if _xinit_supervisor_present():
        return jsonify({'success': True, 'supervised': True})
    time.sleep(1)
    new_pid = _launch_chromium()
    return jsonify({'success': True, 'pid': new_pid})


@kiosk_bp.route('/api/kill', methods=['POST'])
@login_required
def kill():
    _kill_chromium()
    return jsonify({'success': True})


_HIDE_CURSOR_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hide_cursor.py')


def _apply_cursor_setting(show_cursor):
    """Show or hide the cursor by managing the hide_cursor.py background process.
    XFixes hide is tied to connection lifetime, so a persistent process is needed."""
    env = get_sys().get_env_with_display()
    try:
        # Kill any existing hide_cursor.py process
        subprocess.run(['pkill', '-f', 'hide_cursor.py'], timeout=5,
                       capture_output=True)
        if show_cursor:
            logger.info('Cursor shown (hide_cursor.py killed)')
        else:
            time.sleep(0.3)
            subprocess.Popen(
                ['python3', _HIDE_CURSOR_SCRIPT],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info('Cursor hidden (hide_cursor.py started)')
    except Exception as e:
        logger.warning('Could not apply cursor setting: %s', e)


def _write_kiosk_url_file(url):
    """Write the kiosk URL to ~/.kiosk-url so .xinitrc picks it up."""
    try:
        url_file = os.path.expanduser('~/.kiosk-url')
        with open(url_file, 'w') as f:
            f.write(url)
        logger.info('Wrote kiosk URL to %s', url_file)
    except Exception as e:
        logger.warning('Could not write ~/.kiosk-url: %s', e)


@kiosk_bp.route('/api/devtools-tabs')
@login_required
def devtools_tabs():
    """Proxy Chrome's remote debugging /json endpoint to list open tabs."""
    try:
        req = urllib.request.Request('http://127.0.0.1:9222/json')
        resp = urllib.request.urlopen(req, timeout=3)
        tabs = json.loads(resp.read().decode())
        # Filter to only page-type targets
        pages = []
        for tab in tabs:
            if tab.get('type') == 'page':
                pages.append({
                    'title': tab.get('title', 'Untitled'),
                    'url': tab.get('url', ''),
                    'id': tab.get('id', ''),
                    'devtoolsFrontendUrl': tab.get('devtoolsFrontendUrl', ''),
                    'webSocketDebuggerUrl': tab.get('webSocketDebuggerUrl', ''),
                })
        return jsonify({'success': True, 'tabs': pages})
    except Exception as e:
        return jsonify({'success': False, 'tabs': [], 'error': str(e)})


@kiosk_bp.route('/api/cursor', methods=['POST'])
def cursor_toggle():
    """Toggle cursor visibility — accessible without auth for login screen shortcuts."""
    data = request.get_json() or {}
    show = data.get('show', True)
    set_setting('kiosk_cursor', '1' if show else '0')
    _apply_cursor_setting(show)
    return jsonify({'success': True, 'cursor': show})


@kiosk_bp.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.get_json()
    if 'url' in data:
        url = data['url'].strip()
        if not url.startswith(('http://', 'https://')):
            return jsonify({'error': 'URL must start with http:// or https://'}), 400
        set_setting('kiosk_url', url)
        _write_kiosk_url_file(url)
    if 'cursor' in data:
        enabled = bool(data['cursor'])
        set_setting('kiosk_cursor', '1' if enabled else '0')
        _apply_cursor_setting(enabled)
    if 'check_timeout' in data:
        try:
            t = int(data['check_timeout'])
            if 1 <= t <= 30:
                set_setting('kiosk_check_timeout', str(t))
        except (ValueError, TypeError):
            pass
    if 'watchdog' in data:
        global _watchdog_running, _watchdog_thread
        enabled = bool(data['watchdog'])
        set_setting('kiosk_watchdog', '1' if enabled else '0')
        if enabled and not _watchdog_running:
            _watchdog_running = True
            _watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
            _watchdog_thread.start()
        elif not enabled:
            _watchdog_running = False
    return jsonify({'success': True})


@kiosk_bp.route('/inspector')
@login_required
def inspector_page():
    """Serve the lightweight built-in DevTools inspector (fallback)."""
    page_id = request.args.get('page', '')
    return render_template('devtools_inspector.html', page_id=page_id)


_DEVTOOLS_MIME = {
    '.html': 'text/html', '.js': 'application/javascript',
    '.css': 'text/css', '.json': 'application/json',
    '.png': 'image/png', '.svg': 'image/svg+xml',
    '.wasm': 'application/wasm',
}


@kiosk_bp.route('/devtools/<path:path>')
@login_required
def devtools_frontend_proxy(path):
    """Proxy Chrome's built-in DevTools frontend from port 9222."""
    try:
        chrome_url = f'http://127.0.0.1:9222/devtools/{path}'
        req = urllib.request.Request(chrome_url)
        resp = urllib.request.urlopen(req, timeout=5)
        content = resp.read()
        ct = resp.headers.get('Content-Type')
        if not ct:
            ext = os.path.splitext(path)[1].lower()
            ct = _DEVTOOLS_MIME.get(ext, 'application/octet-stream')
        return content, 200, {
            'Content-Type': ct,
            'Cache-Control': 'public, max-age=86400',
        }
    except Exception as e:
        return jsonify({'error': f'DevTools frontend not available: {e}'}), 502


def init_kiosk_ws(sock):
    """Register WebSocket routes for DevTools proxy."""

    @sock.route('/kiosk/devtools-ws/<page_id>')
    def devtools_ws_proxy(ws, page_id):
        """Bidirectional WebSocket proxy: browser <-> Chrome DevTools."""
        chrome_ws = None

        def _try_connect(pid):
            url = f'ws://127.0.0.1:9222/devtools/page/{pid}'
            c = ws_client.WebSocket()
            c.connect(url, timeout=5)
            return c

        # Try the requested page ID first, fallback to first available page
        try:
            chrome_ws = _try_connect(page_id)
        except Exception:
            logger.info('Page ID %s stale, looking up current targets...', page_id)
            try:
                req = urllib.request.Request('http://127.0.0.1:9222/json')
                resp = urllib.request.urlopen(req, timeout=3)
                tabs = json.loads(resp.read().decode())
                pages = [t for t in tabs if t.get('type') == 'page']
                if pages:
                    chrome_ws = _try_connect(pages[0]['id'])
                    logger.info('Resolved to page ID %s', pages[0]['id'])
                else:
                    raise RuntimeError('No page targets available')
            except Exception as e2:
                logger.warning('Could not connect to Chrome DevTools WS: %s', e2)
                try:
                    ws.send(json.dumps({
                        'error': f'Cannot connect to Chrome debug port: {e2}'
                    }))
                except Exception:
                    pass
                return

        stop = threading.Event()

        def chrome_to_client():
            """Forward messages from Chrome -> client browser."""
            try:
                while not stop.is_set():
                    try:
                        chrome_ws.settimeout(1)
                        msg = chrome_ws.recv()
                        if msg:
                            ws.send(msg)
                    except ws_client.WebSocketTimeoutException:
                        continue
                    except Exception:
                        break
            finally:
                stop.set()

        relay = threading.Thread(target=chrome_to_client, daemon=True)
        relay.start()

        try:
            while not stop.is_set():
                msg = ws.receive(timeout=1)
                if msg is None:
                    break
                chrome_ws.send(msg)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                chrome_ws.close()
            except Exception:
                pass
