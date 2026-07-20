import subprocess
import os
import re
import json
import time
import threading
import tempfile
import logging
from flask import Blueprint, render_template, request, jsonify, current_app
from auth_utils import login_required
from sysdetect import get_sys
from config import BASE_DIR

logger = logging.getLogger(__name__)

system_bp = Blueprint('system', __name__)

SCHEDULE_FILE = os.path.join(BASE_DIR, 'data', 'schedule.json')


@system_bp.route('/')
@login_required
def system_page():
    return render_template('system.html')


@system_bp.route('/api/reboot', methods=['POST'])
@login_required
def reboot():
    data = request.get_json() or {}
    confirm = data.get('confirm', False)
    if not confirm:
        return jsonify({'error': 'Confirmation required'}), 400
    cmd = get_sys().get_reboot_cmd()
    if not cmd:
        return jsonify({'error': 'Reboot command not available on this system'}), 500
    try:
        logger.info('Executing reboot: %s', cmd)
        subprocess.Popen(cmd)
        return jsonify({'success': True, 'message': 'System is rebooting...'})
    except Exception as e:
        logger.error('Reboot failed: %s', e)
        return jsonify({'error': str(e)}), 500


@system_bp.route('/api/shutdown', methods=['POST'])
@login_required
def shutdown():
    data = request.get_json() or {}
    confirm = data.get('confirm', False)
    if not confirm:
        return jsonify({'error': 'Confirmation required'}), 400
    cmd = get_sys().get_shutdown_cmd()
    if not cmd:
        return jsonify({'error': 'Shutdown command not available on this system'}), 500
    try:
        logger.info('Executing shutdown: %s', cmd)
        subprocess.Popen(cmd)
        return jsonify({'success': True, 'message': 'System is shutting down...'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@system_bp.route('/api/info')
@login_required
def info():
    sys = get_sys()
    return jsonify({
        'cpu_cores': sys.get_cpu_cores(),
        'memory': sys.get_memory_info(),
        'disk': sys.get_disk_info(),
        'load': sys.get_load_average(),
    })


# ── Scheduled Reboot (JSON file + background thread) ─────────

def _read_schedule():
    """Read schedule from JSON file."""
    default = {'enabled': False, 'hour': 3, 'minute': 0, 'days': '*'}
    if not os.path.exists(SCHEDULE_FILE):
        return default
    try:
        with open(SCHEDULE_FILE, 'r') as f:
            data = json.load(f)
        return data
    except Exception as e:
        logger.error('Error reading schedule.json: %s', e)
        return default


def _write_schedule(data):
    """Write schedule to JSON file."""
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    try:
        with open(SCHEDULE_FILE, 'w') as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        logger.error('Error writing schedule.json: %s', e)
        return False


def _check_reboot_time():
    """Background thread: every 30s read schedule.json, compare time, reboot if match."""
    last_reboot = None
    while True:
        try:
            schedule = _read_schedule()
            if schedule.get('enabled'):
                now = time.strftime('%H:%M')
                reboot_time = f"{schedule['hour']:02d}:{schedule['minute']:02d}"

                if now == reboot_time and last_reboot != reboot_time:
                    last_reboot = reboot_time
                    logger.info('Scheduled reboot time %s reached. Rebooting...', reboot_time)
                    subprocess.Popen(['sudo', 'reboot'])
                elif now != reboot_time:
                    last_reboot = None
        except Exception as e:
            logger.error('Reboot scheduler error: %s', e)
        time.sleep(30)


def start_reboot_scheduler():
    """Start the background reboot checker thread."""
    t = threading.Thread(target=_check_reboot_time, daemon=True)
    t.start()
    logger.info('Reboot scheduler thread started')


@system_bp.route('/api/schedule-reboot')
@login_required
def get_schedule_reboot():
    """Get current auto-reboot schedule."""
    return jsonify(_read_schedule())


@system_bp.route('/api/schedule-reboot', methods=['POST'])
@login_required
def set_schedule_reboot():
    """Set or update auto-reboot schedule."""
    data = request.get_json()
    enabled = data.get('enabled', False)

    schedule = {
        'enabled': enabled,
        'hour': int(data.get('hour', 3)),
        'minute': int(data.get('minute', 0)),
        'days': data.get('days', '*').strip() or '*',
    }

    if enabled:
        if not (0 <= schedule['hour'] <= 23 and 0 <= schedule['minute'] <= 59):
            return jsonify({'error': 'Invalid time'}), 400
        if not re.match(r'^[\d,\*\-]+$', schedule['days']):
            return jsonify({'error': 'Invalid days format'}), 400

    if not _write_schedule(schedule):
        return jsonify({'error': 'Failed to save schedule'}), 500

    logger.info('Schedule updated: %s', schedule)
    return jsonify({'success': True, 'enabled': enabled})


# ── Self-signed Certificate Management ───────────────────────

def _get_nssdb_path():
    """Get the Chromium NSS database path."""
    home = os.path.expanduser('~')
    for candidate in [
        os.path.join(home, '.pki/nssdb'),
        os.path.join(home, 'snap/chromium/current/.pki/nssdb'),
    ]:
        if os.path.isdir(candidate):
            return candidate
    # Create default path if it doesn't exist
    default = os.path.join(home, '.pki/nssdb')
    os.makedirs(default, exist_ok=True)
    # Initialize the NSS database
    subprocess.run(
        ['certutil', '-d', f'sql:{default}', '-N', '--empty-password'],
        capture_output=True, timeout=10
    )
    return default


@system_bp.route('/api/certs')
@login_required
def list_certs():
    """List installed certificates in Chromium's NSS database."""
    try:
        nssdb = _get_nssdb_path()
        result = subprocess.run(
            ['certutil', '-d', f'sql:{nssdb}', '-L'],
            capture_output=True, text=True, timeout=10
        )
        certs = []
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('Certificate Nickname') or line.startswith('-'):
                continue
            # Format: "nickname    trust_flags"
            parts = line.rsplit(None, 1)
            if len(parts) >= 1:
                certs.append({
                    'name': parts[0].strip(),
                    'trust': parts[1].strip() if len(parts) > 1 else '',
                })
        return jsonify({'success': True, 'certs': certs, 'nssdb': nssdb})
    except FileNotFoundError:
        return jsonify({'success': False, 'error': 'certutil not found. Install libnss3-tools: sudo apt install libnss3-tools', 'certs': []})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'certs': []})


@system_bp.route('/api/certs/upload', methods=['POST'])
@login_required
def upload_cert():
    """Upload and install a self-signed certificate into Chromium's NSS database."""
    if 'cert' not in request.files:
        return jsonify({'error': 'No certificate file provided'}), 400

    f = request.files['cert']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    name = request.form.get('name', '').strip()
    if not name:
        name = os.path.splitext(f.filename)[0]

    # Save to temp file
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'pem'
    if ext not in ('pem', 'crt', 'cer', 'der'):
        return jsonify({'error': 'Invalid certificate file. Allowed: .pem, .crt, .cer, .der'}), 400

    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
            f.save(tmp)
            tmp_path = tmp.name

        nssdb = _get_nssdb_path()

        # If DER format, convert to PEM first
        cert_path = tmp_path
        if ext == 'der':
            pem_path = tmp_path + '.pem'
            result = subprocess.run(
                ['openssl', 'x509', '-inform', 'DER', '-in', tmp_path, '-out', pem_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                os.remove(tmp_path)
                return jsonify({'error': 'Failed to convert DER certificate'}), 500
            cert_path = pem_path

        # Pick the trust flags to match what this certificate actually is.
        # "CT,C,C" means "trust as a certificate authority" and only works for a
        # real CA: Chrome refuses to use a certificate with basicConstraints
        # CA:FALSE as a trust anchor, so a self-signed *server* cert installed
        # this way still fails with ERR_CERT_AUTHORITY_INVALID. Such a cert has
        # to be trusted as a peer ("P,,") instead — verified working against a
        # self-signed site on Chromium 150.
        trust = 'CT,C,C'
        try:
            probe = subprocess.run(
                ['openssl', 'x509', '-in', cert_path, '-noout',
                 '-ext', 'basicConstraints'],
                capture_output=True, text=True, timeout=10
            )
            # No basicConstraints at all also means "not a CA" (RFC 5280).
            if probe.returncode == 0 and 'CA:TRUE' not in probe.stdout.upper():
                trust = 'P,,'
        except Exception:
            pass  # undetectable — keep the CA trust that was always used

        result = subprocess.run(
            ['certutil', '-d', f'sql:{nssdb}', '-A', '-t', trust, '-n', name, '-i', cert_path],
            capture_output=True, text=True, timeout=10
        )

        # Cleanup temp files
        os.remove(tmp_path)
        if cert_path != tmp_path and os.path.exists(cert_path):
            os.remove(cert_path)

        if result.returncode != 0:
            return jsonify({'error': f'certutil failed: {result.stderr.strip()}'}), 500

        logger.info('Installed certificate: %s (trust %s)', name, trust)
        return jsonify({'success': True, 'name': name, 'trust': trust,
                        'kind': 'authority' if trust == 'CT,C,C' else 'self-signed server'})
    except FileNotFoundError:
        return jsonify({'error': 'certutil not found. Install: sudo apt install libnss3-tools'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@system_bp.route('/api/certs/delete', methods=['POST'])
@login_required
def delete_cert():
    """Delete a certificate from Chromium's NSS database."""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Certificate name required'}), 400

    try:
        nssdb = _get_nssdb_path()
        result = subprocess.run(
            ['certutil', '-d', f'sql:{nssdb}', '-D', '-n', name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return jsonify({'error': f'Failed to delete: {result.stderr.strip()}'}), 500
        logger.info('Deleted certificate: %s', name)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
