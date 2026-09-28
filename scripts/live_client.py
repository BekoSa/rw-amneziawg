"""Disposable AWG test client. All routes and interfaces live in this container namespace only.

A tiny control API listens only on the lab-control network so the E2E runner can bring the
tunnel up/down and run probes. It never listens on the underlay used by the tunnel.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if os.environ.get('AWG_ISOLATED_RUNTIME') != '1' or not Path('/.dockerenv').exists():
    raise SystemExit('refusing networking outside isolated container')

INTERFACE = 'awg0'
TARGET = os.environ.get('AWG_LAB_TARGET', '10.240.5.3')
CONTROL_ADDRESS = os.environ['AWG_LAB_CONTROL_ADDRESS']
AWG_KEYS = ('Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4', 'I1', 'I2', 'I3', 'I4', 'I5',
            'HeaderProtectionKey', 'ContentPaddingAddition', 'RekeyAfterTime', 'RekeyTimeout', 'RejectAfterTime',
            'KeepaliveTimeout', 'MaxHandshakeAttempts', 'RandomTrailers', 'DisableCookies')
process = None


def run(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, timeout=15)


def down():
    global process
    run('ip', 'link', 'del', INTERFACE, check=False)
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        process = None
    Path(f'/run/amneziawg/{INTERFACE}.sock').unlink(missing_ok=True)


def up(config):
    """config: private_key, address, server_public_key, endpoint, allowed_ips, mtu, parameters, routes."""
    global process
    down()
    for key in ('private_key', 'server_public_key'):
        if len(base64.b64decode(config[key], validate=True)) != 32:
            raise ValueError('invalid key')
    address = ipaddress.ip_interface(config['address'])
    routes = [str(ipaddress.ip_network(item, strict=True)) for item in config['routes']]
    allowed = [str(ipaddress.ip_network(item, strict=True)) for item in config['allowed_ips']]
    host, port = config['endpoint'].rsplit(':', 1)
    ipaddress.ip_address(host)
    lines = ['[Interface]', f'PrivateKey = {config["private_key"]}']
    for key in AWG_KEYS:
        if key in config['parameters']:
            value = str(config['parameters'][key])
            if value in ('true', 'false'):
                value = 'on' if value == 'true' else 'off'  # awg setconf toggles
            if any(c in value for c in '\r\n'):
                raise ValueError('invalid parameter')
            lines.append(f'{key} = {value}')
    lines += ['', '[Peer]', f'PublicKey = {config["server_public_key"]}', f'Endpoint = {host}:{int(port)}',
              'AllowedIPs = ' + ', '.join(allowed), 'PersistentKeepalive = 5']
    path = Path('/run/awg-client.conf')
    path.write_text('\n'.join(lines) + '\n')
    path.chmod(0o600)
    Path('/run/amneziawg').mkdir(exist_ok=True)
    process = subprocess.Popen(['amneziawg-go', '-f', INTERFACE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if Path(f'/run/amneziawg/{INTERFACE}.sock').exists():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError('amneziawg-go did not start')
    run('awg', 'setconf', INTERFACE, str(path))
    run('ip', 'address', 'add', str(address), 'dev', INTERFACE)
    run('ip', 'link', 'set', 'dev', INTERFACE, 'mtu', str(int(config['mtu'])), 'up')
    for route in routes:
        # Full tunnel replaces the default route of THIS namespace only.
        run('ip', 'route', 'replace', route, 'dev', INTERFACE)


def http_probe():
    try:
        with urllib.request.urlopen(f'http://{TARGET}:8080/', timeout=4) as response:
            return response.read().decode() == 'awg-tunnel-payload-ok'
    except OSError:
        return False


def udp_probe():
    payload = os.urandom(16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        try:
            sock.sendto(payload, (TARGET, 8081))
            return sock.recvfrom(64)[0] == payload
        except OSError:
            return False


def dns_probe():
    query = struct.pack('!HHHHHH', 0x4157, 0x0100, 1, 0, 0, 0) + b'\x06target\x03lab\x00\x00\x01\x00\x01'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        try:
            sock.sendto(query, (TARGET, 5353))
            answer = sock.recvfrom(512)[0]
            return answer[:2] == query[:2] and socket.inet_ntoa(answer[-4:]) == TARGET
        except OSError:
            return False


def reach(host, port):
    ipaddress.ip_address(host)
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def handshake():
    result = run('awg', 'show', INTERFACE, 'latest-handshakes', check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    return int(result.stdout.split()[-1])


def probe(wait):
    deadline = time.monotonic() + wait
    while True:
        result = {'http': http_probe(), 'udp': udp_probe(), 'dns': dns_probe(), 'handshake': handshake(),
                  'routes': run('ip', '-j', 'route', 'show').stdout, 'interface': Path(f'/sys/class/net/{INTERFACE}').exists()}
        result['routes'] = json.loads(result['routes'] or '[]')
        if all(result[k] for k in ('http', 'udp', 'dns')) or time.monotonic() >= deadline:
            return result
        time.sleep(1)


class Control(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/up':
                up(body)
            elif self.path == '/down':
                down()
            elif self.path == '/reach':
                # TCP connect attempts through whatever routes the namespace currently has.
                with ThreadPoolExecutor(max_workers=64) as pool:
                    return self.reply(200, list(pool.map(lambda t: reach(t[0], int(t[1])), body['targets'])))
            elif self.path == '/probe':
                return self.reply(200, probe(float(body.get('wait', 0))))
            else:
                return self.reply(404, {'error': 'unknown'})
            self.reply(200, {'ok': True})
        except Exception as exc:
            # Lab-only diagnostics; never include config (it contains the private key).
            self.reply(500, {'error': type(exc).__name__})

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer((CONTROL_ADDRESS, 9000), Control).serve_forever()
