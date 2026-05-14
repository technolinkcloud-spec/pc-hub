import os
import subprocess
import re
import logging
from flask import Blueprint, render_template, request, jsonify
from auth_utils import login_required
from sysdetect import get_sys

logger = logging.getLogger(__name__)

network_bp = Blueprint('network', __name__)

SAFE_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,62}$')
SAFE_IP_RE = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(/\d{1,2})?$')
SAFE_IFACE_RE = re.compile(r'^[a-zA-Z0-9\-_:\.]+$')


def _subnet_mask_to_cidr(mask):
    """Convert subnet mask (e.g. 255.255.255.0) to CIDR prefix (e.g. 24)."""
    try:
        octets = [int(o) for o in mask.split('.')]
        if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
            return None
        binary = ''.join(format(o, '08b') for o in octets)
        # Count consecutive 1s from the left
        cidr = len(binary) - len(binary.lstrip('1'))
        # Verify it's a valid mask (all 1s followed by all 0s)
        if binary != ('1' * cidr + '0' * (32 - cidr)):
            return None
        return cidr
    except (ValueError, AttributeError):
        return None


def _run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            if err:
                output = f"{output}\n{err}".strip() if output else err
            logger.warning('Command %s failed (rc=%d): %s', cmd, result.returncode, output)
        return output, result.returncode
    except FileNotFoundError:
        logger.error('Command not found: %s', cmd[0] if cmd else '')
        return f'Command not found: {cmd[0] if cmd else ""}', 1
    except subprocess.TimeoutExpired:
        logger.error('Command timed out: %s', cmd)
        return 'Command timed out', 1
    except Exception as e:
        logger.error('Command error: %s', e)
        return str(e), 1


def _cidr_to_subnet_mask(cidr):
    """Convert CIDR prefix (e.g. 24) to subnet mask (e.g. 255.255.255.0)."""
    try:
        cidr = int(cidr)
        if cidr < 0 or cidr > 32:
            return ''
        bits = ('1' * cidr + '0' * (32 - cidr))
        return '.'.join(str(int(bits[i:i+8], 2)) for i in range(0, 32, 8))
    except (ValueError, TypeError):
        return ''


def _parse_ifupdown_config(iface):
    """Parse /etc/network/interfaces for a specific interface config."""
    config = {'method': 'dhcp', 'ip': '', 'subnet': '', 'gateway': '', 'dns': ''}
    interfaces_file = '/etc/network/interfaces'
    if not os.path.exists(interfaces_file):
        return config
    try:
        with open(interfaces_file) as f:
            content = f.read()
    except Exception:
        return config

    in_block = False
    for line in content.split('\n'):
        stripped = line.strip()
        if re.match(rf'^iface\s+{re.escape(iface)}\s+inet\s+(\S+)', stripped):
            m = re.match(rf'^iface\s+{re.escape(iface)}\s+inet\s+(\S+)', stripped)
            config['method'] = m.group(1)
            in_block = True
            continue
        if in_block:
            if stripped and not stripped.startswith('#') and not (line.startswith(' ') or line.startswith('\t')):
                break
            if stripped.startswith('address'):
                addr = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ''
                if '/' in addr:
                    ip_part, cidr_part = addr.rsplit('/', 1)
                    config['ip'] = ip_part
                    config['subnet'] = _cidr_to_subnet_mask(cidr_part)
                else:
                    config['ip'] = addr
            elif stripped.startswith('netmask'):
                config['subnet'] = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ''
            elif stripped.startswith('gateway'):
                config['gateway'] = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ''
            elif stripped.startswith('dns-nameservers'):
                config['dns'] = stripped.split(None, 1)[1].replace(' ', ', ') if len(stripped.split(None, 1)) > 1 else ''
    return config


def _find_nm_connection(nmcli, iface):
    """Find NM connection profile for a device. Returns None if none targets it.

    Unlike _get_connection_name, this does NOT fall back to the iface name —
    used by the read path so we can fall through to ifupdown when there is
    truly no NM profile for this device.
    """
    out, rc = _run_cmd([nmcli, '-g', 'GENERAL.CONNECTION', 'device', 'show', iface])
    if rc == 0 and out.strip() and out.strip() != '--':
        return out.strip()
    out, rc = _run_cmd([nmcli, '-t', '-f', 'NAME,DEVICE', 'con', 'show'])
    if rc == 0:
        for line in out.split('\n'):
            parts = line.rsplit(':', 1)
            if len(parts) == 2 and parts[1].strip() == iface:
                return parts[0].replace('\\:', ':')
    return None


def _get_iface_config_linux(iface_name):
    """Get current config for a Linux interface."""
    sys = get_sys()
    ip_bin = sys.bin('ip')
    config = {'method': 'dhcp', 'ip': '', 'subnet': '', 'gateway': '', 'dns': ''}

    # Try NM first — it is authoritative when it manages the interface.
    # Look up the connection profile even if no active connection (e.g. after
    # a static→DHCP switch where con up failed or the device is still settling).
    nmcli = sys.bin('nmcli')
    if nmcli and _is_nm_managed(nmcli, iface_name):
        conn = _find_nm_connection(nmcli, iface_name)
        if conn:
            # Get method
            out, _ = _run_cmd([nmcli, '-g', 'ipv4.method', 'con', 'show', conn])
            if out.strip() == 'manual':
                config['method'] = 'static'
            # Get addresses
            out, _ = _run_cmd([nmcli, '-g', 'ipv4.addresses', 'con', 'show', conn])
            if out.strip():
                addr = out.strip()
                if '/' in addr:
                    ip_part, cidr_part = addr.rsplit('/', 1)
                    config['ip'] = ip_part
                    config['subnet'] = _cidr_to_subnet_mask(cidr_part)
                else:
                    config['ip'] = addr
            out, _ = _run_cmd([nmcli, '-g', 'ipv4.gateway', 'con', 'show', conn])
            if out.strip():
                config['gateway'] = out.strip()
            out, _ = _run_cmd([nmcli, '-g', 'ipv4.dns', 'con', 'show', conn])
            if out.strip():
                config['dns'] = out.strip().replace(' ', ', ')
            return config

    # Fallback: ifupdown
    ifup_config = _parse_ifupdown_config(iface_name)
    if ifup_config['method'] == 'static' and ifup_config['ip']:
        return ifup_config
    if ifup_config['method'] == 'dhcp':
        # Interface is managed by ifupdown as DHCP — don't fall through to the
        # ip command which cannot distinguish DHCP from static and would
        # incorrectly mark the DHCP-assigned IP as a static config.
        return ifup_config

    # Fallback: get from ip command (do NOT set method here — we can't tell
    # whether the current IP was assigned by DHCP or configured statically)
    if ip_bin:
        ip_out, _ = _run_cmd([ip_bin, '-4', 'addr', 'show', iface_name])
        ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', ip_out)
        if ip_match:
            config['ip'] = ip_match.group(1)
            config['subnet'] = _cidr_to_subnet_mask(ip_match.group(2))
        route_out, _ = _run_cmd([ip_bin, 'route', 'show', 'dev', iface_name])
        gw_match = re.search(r'default via\s+([\d.]+)', route_out)
        if gw_match:
            config['gateway'] = gw_match.group(1)
        # DNS from resolv.conf
        if os.path.exists('/etc/resolv.conf'):
            try:
                with open('/etc/resolv.conf') as f:
                    servers = [line.split()[1] for line in f if line.startswith('nameserver')]
                    config['dns'] = ', '.join(servers)
            except Exception:
                pass
    return config


def _get_interfaces_linux():
    """Get network interfaces on Linux using ip command."""
    sys = get_sys()
    ip_bin = sys.bin('ip')
    if not ip_bin:
        return []

    interfaces = []
    output, _ = _run_cmd([ip_bin, '-o', 'link', 'show'])
    if not output:
        return interfaces

    for line in output.split('\n'):
        parts = line.split(': ')
        if len(parts) < 2:
            continue
        iface_name = parts[1].split('@')[0].strip()
        if iface_name == 'lo':
            continue

        state = 'DOWN'
        if 'state UP' in line:
            state = 'UP'

        mac = ''
        mac_match = re.search(r'link/ether\s+([\da-f:]+)', line)
        if mac_match:
            mac = mac_match.group(1)

        ip_out, _ = _run_cmd([ip_bin, '-4', 'addr', 'show', iface_name])
        ip_addr = ''
        ip_match = re.search(r'inet\s+([\d./]+)', ip_out)
        if ip_match:
            ip_addr = ip_match.group(1)

        interfaces.append({
            'name': iface_name,
            'state': state,
            'mac': mac,
            'ip': ip_addr,
        })

    return interfaces


def _get_interfaces_macos():
    """Get network interfaces on macOS using ifconfig."""
    sys = get_sys()
    ifconfig_bin = sys.bin('ifconfig')
    if not ifconfig_bin:
        return []

    interfaces = []
    output, _ = _run_cmd([ifconfig_bin])
    if not output:
        return interfaces

    current_iface = None
    for line in output.split('\n'):
        iface_match = re.match(r'^(\w+):\s+flags=', line)
        if iface_match:
            name = iface_match.group(1)
            if name == 'lo0':
                current_iface = None
                continue
            current_iface = {'name': name, 'state': 'DOWN', 'mac': '', 'ip': ''}
            if 'UP' in line:
                current_iface['state'] = 'UP'
            interfaces.append(current_iface)
        elif current_iface:
            mac_match = re.search(r'ether\s+([\da-f:]+)', line)
            if mac_match:
                current_iface['mac'] = mac_match.group(1)
            ip_match = re.search(r'inet\s+([\d.]+)', line)
            if ip_match:
                current_iface['ip'] = ip_match.group(1)

    return interfaces


def _get_interfaces():
    """Get network interfaces, cross-platform."""
    sys = get_sys()
    if sys.is_linux:
        return _get_interfaces_linux()
    if sys.is_macos:
        return _get_interfaces_macos()
    return []


@network_bp.route('/')
@login_required
def network_page():
    return render_template('network.html')


@network_bp.route('/api/interfaces')
@login_required
def interfaces():
    return jsonify({'interfaces': _get_interfaces()})


@network_bp.route('/api/iface-config/<name>')
@login_required
def iface_config(name):
    """Return current configuration for a specific interface."""
    if not SAFE_IFACE_RE.match(name):
        return jsonify({'error': 'Invalid interface name'}), 400
    sys = get_sys()
    if sys.is_linux:
        config = _get_iface_config_linux(name)
    else:
        config = {'method': 'dhcp', 'ip': '', 'subnet': '', 'gateway': '', 'dns': ''}
    # Also return proxy settings
    config['proxy'] = _get_proxy_settings()
    resp = jsonify(config)
    # Prevent stale UI state after a config change — never serve from cache
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _get_proxy_settings():
    """Read proxy settings from /etc/environment."""
    proxy = {'http_proxy': '', 'https_proxy': '', 'no_proxy': ''}
    env_file = '/etc/environment'
    if not os.path.exists(env_file):
        return proxy
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                for key in ('http_proxy', 'https_proxy', 'no_proxy'):
                    if line.lower().startswith(key + '='):
                        val = line.split('=', 1)[1].strip().strip('"').strip("'")
                        proxy[key] = val
    except Exception:
        pass
    return proxy


def _set_proxy_settings(http_proxy, https_proxy, no_proxy):
    """Write proxy settings to /etc/environment."""
    env_file = '/etc/environment'
    proxy_keys = {'http_proxy', 'https_proxy', 'no_proxy',
                  'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'}
    lines = []
    if os.path.exists(env_file):
        try:
            with open(env_file) as f:
                for line in f:
                    key = line.strip().split('=', 1)[0].strip()
                    if key not in proxy_keys:
                        lines.append(line.rstrip('\n'))
        except Exception:
            pass

    if http_proxy:
        lines.append(f'http_proxy="{http_proxy}"')
        lines.append(f'HTTP_PROXY="{http_proxy}"')
    if https_proxy:
        lines.append(f'https_proxy="{https_proxy}"')
        lines.append(f'HTTPS_PROXY="{https_proxy}"')
    if no_proxy:
        lines.append(f'no_proxy="{no_proxy}"')
        lines.append(f'NO_PROXY="{no_proxy}"')

    try:
        content = '\n'.join(lines) + '\n'
        proc = subprocess.run(['sudo', 'tee', env_file],
                              input=content, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            logger.error('Failed to write %s: %s', env_file, proc.stderr)
            return
        logger.info('Proxy settings updated in %s', env_file)
    except Exception as e:
        logger.error('Failed to write proxy settings: %s', e)


def _is_nm_managed(nmcli, iface):
    """Check if NetworkManager actually manages this device."""
    output, rc = _run_cmd([nmcli, '-t', '-f', 'DEVICE,STATE', 'device', 'status'])
    if rc != 0:
        return False
    for line in output.split('\n'):
        parts = line.split(':')
        if len(parts) >= 2 and parts[0] == iface:
            state = parts[1].strip()
            logger.info('NM device %s state: %s', iface, state)
            return state != 'unmanaged'
    return False


@network_bp.route('/api/configure', methods=['POST'])
@login_required
def configure():
    sys = get_sys()
    data = request.get_json()
    iface = data.get('interface', '')
    method = data.get('method', 'dhcp')

    if not SAFE_IFACE_RE.match(iface):
        return jsonify({'error': 'Invalid interface name'}), 400

    # Save proxy settings if provided
    proxy = data.get('proxy', {})
    if proxy:
        _set_proxy_settings(
            proxy.get('http_proxy', ''),
            proxy.get('https_proxy', ''),
            proxy.get('no_proxy', ''),
        )

    # Check if NM manages this specific interface; fall back to ifupdown if not
    if sys.net_backend == 'networkmanager':
        nmcli = sys.bin('nmcli')
        if nmcli and _is_nm_managed(nmcli, iface):
            return _configure_nmcli(nmcli, iface, method, data)
        logger.info('Interface %s is not NM-managed, trying ifupdown', iface)

    if sys.is_linux and os.path.exists('/etc/network/interfaces'):
        return _configure_ifupdown(iface, method, data)

    if sys.net_backend == 'macos':
        return jsonify({'error': 'Network configuration via dashboard not supported on macOS. Use System Preferences.'}), 400

    return jsonify({'error': f'Network backend "{sys.net_backend}" not supported for configuration'}), 400


def _get_connection_name(nmcli, iface):
    """Return the NetworkManager connection profile name for a network device."""
    # Step 1: active connection
    output, rc = _run_cmd([nmcli, '-g', 'GENERAL.CONNECTION', 'device', 'show', iface])
    if rc == 0 and output.strip() and output.strip() != '--':
        logger.info('Found active connection "%s" for device %s', output.strip(), iface)
        return output.strip()

    # Step 2: any connection with a matching device field
    output, rc = _run_cmd([nmcli, '-t', '-f', 'NAME,DEVICE', 'con', 'show'])
    if rc == 0:
        for line in output.split('\n'):
            parts = line.rsplit(':', 1)
            if len(parts) == 2 and parts[1].strip() == iface:
                name = parts[0].replace('\\:', ':')
                logger.info('Found connection "%s" for device %s', name, iface)
                return name

    # Step 3: fall back to interface name
    logger.warning('No NM connection profile found for %s', iface)
    return iface


def _remove_ifupdown_iface(iface):
    """Remove any stanza for iface from /etc/network/interfaces (best-effort)."""
    interfaces_file = '/etc/network/interfaces'
    if not os.path.exists(interfaces_file):
        return
    try:
        with open(interfaces_file) as f:
            content = f.read()
        lines = content.split('\n')
        new_lines = []
        skip = False
        for line in lines:
            if re.match(rf'^(auto|iface|allow-hotplug)\s+{re.escape(iface)}\b', line):
                skip = True
                continue
            # Consume any indented body line (space- or tab-indented) and blank lines
            if skip and (line.startswith(' ') or line.startswith('\t') or line.strip() == ''):
                continue
            skip = False
            new_lines.append(line)
        new_content = '\n'.join(new_lines)
        subprocess.run(['sudo', 'tee', interfaces_file],
                       input=new_content, capture_output=True, text=True, timeout=10)
        logger.info('Removed ifupdown stanza for %s from %s', iface, interfaces_file)
    except Exception as e:
        logger.warning('Could not remove ifupdown stanza for %s: %s', iface, e)


def _configure_nmcli(nmcli, iface, method, data):
    """Configure network interface using nmcli."""
    conn_name = _get_connection_name(nmcli, iface)
    logger.info('Configuring %s on profile "%s" with method=%s', iface, conn_name, method)

    if method == 'dhcp':
        output, rc = _run_cmd([
            'sudo', nmcli, 'con', 'mod', conn_name,
            'ipv4.method', 'auto',
            'ipv4.gateway', '',
            'ipv4.dns', '',
        ])
        if rc != 0:
            return jsonify({'error': f'Failed to set DHCP: {output[:200]}'}), 500
        # Clear any leftover static addresses separately (empty string clears the list)
        _run_cmd(['sudo', nmcli, 'con', 'mod', conn_name, 'ipv4.addresses', ''])
        # Verify the method actually persisted — if not, the modify silently
        # targeted the wrong profile or NM ignored the change. Log loudly so we
        # can catch this in the field instead of returning success while the UI
        # keeps reading 'manual'.
        verify, _ = _run_cmd([nmcli, '-g', 'ipv4.method', 'con', 'show', conn_name])
        if verify.strip() != 'auto':
            logger.warning('NM profile "%s" reports ipv4.method=%r after DHCP modify (expected auto)',
                           conn_name, verify.strip())
        # Also remove static stanza from /etc/network/interfaces to prevent
        # networking.service from re-applying the old static IP on reboot
        _remove_ifupdown_iface(iface)

    elif method == 'static':
        ip_addr = data.get('ip', '')
        gateway = data.get('gateway', '')
        dns = data.get('dns', '')
        subnet_mask = data.get('subnet', '255.255.255.0')

        if not SAFE_IP_RE.match(ip_addr):
            return jsonify({'error': 'Invalid IP address'}), 400
        if gateway and not SAFE_IP_RE.match(gateway):
            return jsonify({'error': 'Invalid gateway'}), 400

        # Convert subnet mask to CIDR prefix
        cidr = _subnet_mask_to_cidr(subnet_mask)
        if cidr is None:
            return jsonify({'error': 'Invalid subnet mask'}), 400

        # nmcli requires CIDR notation
        if '/' not in ip_addr:
            ip_addr = f'{ip_addr}/{cidr}'

        # Build address list (primary + optional alternate)
        addresses = ip_addr
        alt_ip = data.get('alt_ip', '')
        alt_subnet = data.get('alt_subnet', '')
        if alt_ip and SAFE_IP_RE.match(alt_ip):
            alt_cidr = _subnet_mask_to_cidr(alt_subnet) if alt_subnet else cidr
            if alt_cidr is None:
                alt_cidr = cidr
            addresses = f'{ip_addr},{alt_ip}/{alt_cidr}'

        cmd = [
            'sudo', nmcli, 'con', 'mod', conn_name,
            'ipv4.method', 'manual',
            'ipv4.addresses', addresses,
        ]
        if gateway:
            cmd.extend(['ipv4.gateway', gateway])
        if dns:
            dns_servers = [d.strip() for d in dns.split(',') if SAFE_IP_RE.match(d.strip())]
            if dns_servers:
                cmd.extend(['ipv4.dns', ' '.join(dns_servers)])

        output, rc = _run_cmd(cmd)
        if rc != 0:
            return jsonify({'error': f'Failed to set static IP: {output[:200]}'}), 500
    else:
        return jsonify({'error': 'Invalid method'}), 400

    _run_cmd(['sudo', nmcli, 'con', 'down', conn_name], timeout=15)
    out, rc = _run_cmd(['sudo', nmcli, 'con', 'up', conn_name], timeout=30)
    if rc != 0:
        logger.warning('Settings saved but could not bring %s up immediately: %s', conn_name, out[:200])
        return jsonify({'success': True, 'warning': 'Settings saved. Interface will be reconfigured on reboot.'})

    return jsonify({'success': True})


def _configure_ifupdown(iface, method, data):
    """Configure network interface via /etc/network/interfaces."""
    interfaces_file = '/etc/network/interfaces'

    if method == 'static':
        ip_addr = data.get('ip', '')
        gateway = data.get('gateway', '')
        dns = data.get('dns', '')
        subnet_mask = data.get('subnet', '255.255.255.0')

        if not SAFE_IP_RE.match(ip_addr):
            return jsonify({'error': 'Invalid IP address'}), 400
        if gateway and not SAFE_IP_RE.match(gateway):
            return jsonify({'error': 'Invalid gateway'}), 400

        cidr = _subnet_mask_to_cidr(subnet_mask)
        if cidr is None:
            return jsonify({'error': 'Invalid subnet mask'}), 400

        block = f'auto {iface}\niface {iface} inet static\n'
        block += f'    address {ip_addr}/{cidr}\n'
        if gateway:
            block += f'    gateway {gateway}\n'
        if dns:
            servers = ' '.join(d.strip() for d in dns.split(',') if SAFE_IP_RE.match(d.strip()))
            if servers:
                block += f'    dns-nameservers {servers}\n'

        # Alternate IP as a secondary address
        alt_ip = data.get('alt_ip', '')
        alt_subnet = data.get('alt_subnet', '')
        if alt_ip and SAFE_IP_RE.match(alt_ip):
            alt_cidr = _subnet_mask_to_cidr(alt_subnet) if alt_subnet else cidr
            if alt_cidr is None:
                alt_cidr = cidr
            block += f'    up ip addr add {alt_ip}/{alt_cidr} dev {iface} label {iface}:1\n'
            block += f'    down ip addr del {alt_ip}/{alt_cidr} dev {iface} label {iface}:1\n'
    elif method == 'dhcp':
        block = f'auto {iface}\niface {iface} inet dhcp\n'
    else:
        return jsonify({'error': 'Invalid method'}), 400

    try:
        # Read existing file, replace or append the iface block
        content = ''
        if os.path.exists(interfaces_file):
            with open(interfaces_file) as f:
                content = f.read()

        # Remove existing block for this interface
        lines = content.split('\n')
        new_lines = []
        skip = False
        for line in lines:
            if re.match(rf'^(auto|iface|allow-hotplug)\s+{re.escape(iface)}\b', line):
                skip = True
                continue
            # Consume any indented body line (space- or tab-indented) and blank lines
            if skip and (line.startswith(' ') or line.startswith('\t') or line.strip() == ''):
                continue
            skip = False
            new_lines.append(line)

        # Remove trailing blank lines then append new block
        while new_lines and new_lines[-1].strip() == '':
            new_lines.pop()
        new_content = '\n'.join(new_lines) + '\n\n' + block

        # Backup and write (use sudo since service may run as non-root)
        _run_cmd(['sudo', 'cp', '-a', interfaces_file, interfaces_file + '.bak'])
        proc = subprocess.run(['sudo', 'tee', interfaces_file],
                              input=new_content, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)

        logger.info('Wrote ifupdown config for %s', iface)
    except Exception as e:
        logger.error('Failed to write %s: %s', interfaces_file, e)
        return jsonify({'error': f'Failed to write config: {e}'}), 500

    # Restart the interface
    _run_cmd(['sudo', 'ifdown', iface], timeout=15)
    out, rc = _run_cmd(['sudo', 'ifup', iface], timeout=30)
    if rc != 0:
        logger.warning('Config saved but could not bring %s up immediately: %s', iface, out[:200])
        return jsonify({'success': True, 'warning': 'Settings saved. Interface will be reconfigured on reboot.'})

    return jsonify({'success': True})


@network_bp.route('/api/hostname', methods=['POST'])
@login_required
def set_hostname():
    sys = get_sys()
    data = request.get_json()
    hostname = data.get('hostname', '').strip()

    if not SAFE_HOSTNAME_RE.match(hostname):
        return jsonify({'error': 'Invalid hostname'}), 400

    if sys.has('hostnamectl'):
        output, rc = _run_cmd(['sudo', sys.bin('hostnamectl'), 'set-hostname', hostname])
    elif sys.is_macos:
        output, rc = _run_cmd(['sudo', 'scutil', '--set', 'HostName', hostname])
    else:
        return jsonify({'error': 'No hostname tool available'}), 500

    if rc != 0:
        return jsonify({'error': f'Failed to set hostname: {output}'}), 500

    return jsonify({'success': True, 'hostname': hostname})
