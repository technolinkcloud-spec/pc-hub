import subprocess
import os
import shutil
import tempfile
import zipfile
import logging
from flask import Blueprint, render_template, request, jsonify, Response, current_app
from auth_utils import login_required
from config import BASE_DIR, VERSION_FILE
from sysdetect import get_sys

logger = logging.getLogger(__name__)

update_bp = Blueprint('update', __name__)

CANONICAL_REPO_URL = 'https://github.com/technolinkcloud-spec/pc-hub.git'


def _ensure_correct_remote():
    """Repoint origin if the deployed kiosk was installed from a stale URL."""
    try:
        current = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=5
        ).stdout.strip()
        if current and current != CANONICAL_REPO_URL:
            logger.info('Updating origin URL: %s -> %s', current, CANONICAL_REPO_URL)
            subprocess.run(
                ['git', 'remote', 'set-url', 'origin', CANONICAL_REPO_URL],
                capture_output=True, text=True, cwd=BASE_DIR, timeout=5
            )
    except Exception as e:
        logger.warning('Could not verify/fix origin URL: %s', e)


def _get_version():
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE) as f:
            return f.read().strip()
    return 'unknown'


def _get_git_info():
    try:
        branch = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=5
        ).stdout.strip()
        commit = subprocess.run(
            ['git', 'log', '-1', '--format=%h %s'],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=5
        ).stdout.strip()
        remote = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=5
        ).stdout.strip()
        return {'branch': branch, 'commit': commit, 'remote': remote}
    except Exception:
        return {'branch': 'N/A', 'commit': 'N/A', 'remote': 'N/A'}


@update_bp.route('/')
@login_required
def update_page():
    return render_template('update.html')


@update_bp.route('/api/info')
@login_required
def info():
    return jsonify({
        'version': _get_version(),
        'git': _get_git_info(),
    })


@update_bp.route('/api/check', methods=['POST'])
@login_required
def check_updates():
    try:
        _ensure_correct_remote()
        subprocess.run(
            ['git', 'fetch'], capture_output=True, text=True,
            cwd=BASE_DIR, timeout=30
        )
        result = subprocess.run(
            ['git', 'log', 'HEAD..origin/main', '--oneline'],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=10
        )
        commits = result.stdout.strip()
        if not commits:
            result = subprocess.run(
                ['git', 'log', 'HEAD..origin/master', '--oneline'],
                capture_output=True, text=True, cwd=BASE_DIR, timeout=10
            )
            commits = result.stdout.strip()

        if commits:
            return jsonify({'updates_available': True, 'commits': commits})
        return jsonify({'updates_available': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@update_bp.route('/api/pull')
@login_required
def pull():
    def generate():
        try:
            _ensure_correct_remote()
            proc = subprocess.Popen(
                ['git', 'pull', '--ff-only'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=BASE_DIR
            )
            for line in iter(proc.stdout.readline, ''):
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()

            if proc.returncode == 0:
                yield f"data: [SUCCESS] Update complete. Version: {_get_version()}\n\n"
                yield "data: [INFO] Installing dependencies...\n\n"
                venv_pip = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
                pip_bin = venv_pip if os.path.isfile(venv_pip) else 'pip'
                req_file = os.path.join(BASE_DIR, 'requirements.txt')
                pip_proc = subprocess.run(
                    [pip_bin, 'install', '-r', req_file],
                    capture_output=True, text=True, cwd=BASE_DIR, timeout=120
                )
                if pip_proc.returncode == 0:
                    yield "data: [SUCCESS] Dependencies installed.\n\n"
                else:
                    for pip_line in pip_proc.stderr.strip().splitlines():
                        yield f"data: [ERROR] {pip_line}\n\n"
                yield "data: [RESTARTING] Restarting service...\n\n"
                try:
                    sys = get_sys()
                    systemctl_bin = sys.bin('systemctl') or 'systemctl'
                    subprocess.Popen(
                        ['sudo', systemctl_bin, 'restart', 'kiosk-manager'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except Exception:
                    yield "data: [INFO] Auto-restart not available. Please restart manually.\n\n"
            else:
                yield f"data: [ERROR] Update failed with exit code {proc.returncode}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@update_bp.route('/api/offline-upload', methods=['POST'])
@login_required
def offline_upload():
    """Accept an update.zip, extract to temp dir, run update.sh, cleanup."""
    if 'update_zip' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['update_zip']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    if not f.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Only .zip files are accepted'}), 400

    # Save zip to a temp location
    tmp_dir = tempfile.mkdtemp(prefix='kiosk-update-')
    zip_path = os.path.join(tmp_dir, 'update.zip')
    try:
        f.save(zip_path)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({'error': f'Failed to save file: {e}'}), 500

    # Validate it's a valid zip
    if not zipfile.is_zipfile(zip_path):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({'error': 'File is not a valid ZIP archive'}), 400

    # Extract
    extract_dir = os.path.join(tmp_dir, 'extracted')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({'error': f'Failed to extract ZIP: {e}'}), 500

    # Check for update.sh
    update_script = os.path.join(extract_dir, 'update.sh')
    if not os.path.isfile(update_script):
        # Also check one level deep (if zip contains a single folder)
        for entry in os.listdir(extract_dir):
            candidate = os.path.join(extract_dir, entry, 'update.sh')
            if os.path.isfile(candidate):
                update_script = candidate
                extract_dir = os.path.join(extract_dir, entry)
                break
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return jsonify({'error': 'No update.sh found in the ZIP archive'}), 400

    # Save tmp_dir path for the run step
    state_file = os.path.join(current_app.root_path, 'data', '.offline-update-dir')
    with open(state_file, 'w') as sf:
        sf.write(tmp_dir + '\n' + extract_dir + '\n' + update_script)

    return jsonify({
        'success': True,
        'tmp_dir': tmp_dir,
        'files': os.listdir(extract_dir),
    })


@update_bp.route('/api/offline-run')
@login_required
def offline_run():
    """Run the extracted update.sh and stream output. Cleanup after."""
    state_file = os.path.join(current_app.root_path, 'data', '.offline-update-dir')
    if not os.path.isfile(state_file):
        return jsonify({'error': 'No pending offline update. Upload a ZIP first.'}), 400

    with open(state_file, 'r') as sf:
        lines = sf.read().strip().split('\n')
    if len(lines) < 3:
        os.remove(state_file)
        return jsonify({'error': 'Invalid update state'}), 500

    tmp_dir, extract_dir, update_script = lines[0], lines[1], lines[2]

    if not os.path.isfile(update_script):
        os.remove(state_file)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({'error': 'update.sh not found — was it already cleaned up?'}), 400

    def generate():
        try:
            os.chmod(update_script, 0o755)
            yield f"data: [INFO] Running: {update_script}\n\n"
            yield f"data: [INFO] Working directory: {extract_dir}\n\n"

            proc = subprocess.Popen(
                ['sudo', 'bash', update_script],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=extract_dir
            )
            for line in iter(proc.stdout.readline, ''):
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()

            if proc.returncode == 0:
                yield "data: [SUCCESS] Update script completed successfully.\n\n"
            else:
                yield f"data: [ERROR] Update script exited with code {proc.returncode}\n\n"

        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n"
        finally:
            # Cleanup
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if os.path.isfile(state_file):
                    os.remove(state_file)
                yield "data: [INFO] Temporary files cleaned up.\n\n"
            except Exception:
                pass
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
