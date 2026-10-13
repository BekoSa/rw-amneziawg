"""Official Throne 1.3.2 file importer and GUI-started core; no host X/TUN."""
import ipaddress
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.umask(0o077)
os.environ.update(DISPLAY=':99', HOME='/tmp/home', XDG_RUNTIME_DIR='/tmp/runtime',
                  QT_X11_NO_MITSHM='1')
for directory in ('/tmp/home', '/tmp/runtime'):
    Path(directory).mkdir(mode=0o700, exist_ok=True)
subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp'],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
process = None
credentials = ()


def exact(stream, count):
    data = b''
    while len(data) < count:
        block = stream.recv(count - len(data))
        if not block:
            raise ConnectionError('SOCKS connection closed')
        data += block
    return data


def address(host, port):
    return b'\x01' + ipaddress.ip_address(host).packed + struct.pack('!H', port)


def socks(command, host, port):
    stream = socket.create_connection(('127.0.0.1', 2080), 3)
    stream.settimeout(3)
    stream.sendall(b'\x05\x01\x00')
    assert exact(stream, 2) == b'\x05\x00'
    stream.sendall(b'\x05' + bytes([command]) + b'\x00' + address(host, port))
    reply = exact(stream, 4)
    assert reply[:2] == b'\x05\x00'
    assert reply[3] == 1
    relay = socket.inet_ntoa(exact(stream, 4))
    return stream, relay, struct.unpack('!H', exact(stream, 2))[0]


def probe():
    result = {'http': False, 'udp': False}
    try:
        with socks(1, '192.0.0.9', 8080)[0] as stream:
            stream.sendall(b'GET / HTTP/1.0\r\nHost: target\r\n\r\n')
            data = b''
            while block := stream.recv(4096):
                data += block
            result['http'] = b'trusttunnel-real-payload' in data
    except (OSError, AssertionError, ConnectionError):
        pass
    try:
        control, relay, port = socks(3, '0.0.0.0', 0)
        with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(3)
            payload = b'trusttunnel-udp-real-payload'
            udp.sendto(b'\x00\x00\x00' + address('192.0.0.9', 8081) + payload, (relay, port))
            result['udp'] = udp.recv(4096).endswith(payload)
    except (OSError, AssertionError, ConnectionError):
        pass
    return result


def diagnostic():
    message = Path('/tmp/throne.log').read_text(errors='replace')[-4000:]
    gui_log = Path('/tmp/appdata/config/logs/throne.log')
    if gui_log.exists():
        message += gui_log.read_text(errors='replace')[-4000:]
    for value in credentials:
        message = message.replace(value, '[redacted]')
    return message


def core_rpc_ready():
    """Observe the bundled core's connected IPC channel, not importer timing."""
    inodes = set()
    for directory in Path('/proc').glob('[0-9]*'):
        try:
            if Path(os.readlink(directory / 'exe')).name != 'ThroneCore':
                continue
            for descriptor in (directory / 'fd').iterdir():
                target = os.readlink(descriptor)
                if target.startswith('socket:['):
                    inodes.add(target[8:-1])
        except (OSError, ValueError):
            continue
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and fields[9] in inodes:
                return True
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split()
        # Throne's GUI owns the IPC server; ThroneCore connects as its client.
        if fields[5] == '03' and fields[6] in inodes:
            return True
    return False


def configure(body):
    global process, credentials
    if process:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    shutil.rmtree('/tmp/appdata', ignore_errors=True)
    Path('/tmp/subscription.txt').write_text(body['subscription'])
    binary = next(path for path in Path('/opt/throne').rglob('Throne') if path.is_file())
    with Path('/tmp/throne.log').open('wb') as log:
        process = subprocess.Popen([str(binary), '-appdata', '/tmp/appdata', '-many',
                                    '/tmp/subscription.txt'], stdout=log, stderr=log,
                                    cwd=binary.parent, start_new_session=True)
    profile = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None, diagnostic()
        databases = list(Path('/tmp/appdata').rglob('throne.db'))
        if databases:
            try:
                with closing(sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True, timeout=.1)) as connection:
                    rows = connection.execute("SELECT outbound_json FROM profiles WHERE type='trusttunnel'").fetchall()
                if rows:
                    assert len(rows) == 1
                    profile = json.loads(rows[0][0])
                    break
            except sqlite3.OperationalError:
                pass
        time.sleep(.1)
    assert profile is not None, 'Official GUI importer did not create TT profile: ' + diagnostic()
    credentials = (profile['username'], profile['password'])
    assert profile['tls']['enabled'] and not profile['tls'].get('insecure', False)
    # GUI import finishes before the initial core RPC starts. Starting a profile
    # then makes Throne restart that core and discard the start request.
    deadline = time.monotonic() + 30
    while not core_rpc_ready():
        assert process.poll() is None, diagnostic()
        assert time.monotonic() < deadline, 'Initial core RPC readiness timeout: ' + diagnostic()
        time.sleep(.1)
    # Start through the real GUI: its Build() and bundled core consume this import.
    windows = subprocess.check_output(['xdotool', 'search', '--onlyvisible', '--name', 'Throne'], text=True).splitlines()
    assert windows, 'Throne GUI not ready'
    window = windows[-1]
    subprocess.run(['xdotool', 'windowfocus', window], check=True)
    # This fixture has stock first and TT second. The first visible row is y=180.
    subprocess.run(['xdotool', 'mousemove', '--window', window, '220', '180', 'click', '1',
                    'key', 'Home', 'Down', 'Return'], check=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', 2080), .2):
                assert 'Starting profile [TrustTunnel]' in diagnostic(), 'GUI selected stock instead of TT'
                return {'imported': True, 'tls_verified': True, 'started': True,
                        'server': profile['server'], 'port': profile['server_port']}
        except OSError:
            time.sleep(.1)
    raise AssertionError('GUI-started core SOCKS readiness timeout: ' + diagnostic())


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/configure':
                response = configure(body)
            elif self.path == '/probe':
                response = probe()
            elif self.path == '/diagnostic':
                response = {'diagnostic': diagnostic()}
            elif self.path == '/direct':
                try:
                    with socket.create_connection(('192.0.0.9', 8080), 1):
                        response = {'reachable': True}
                except OSError:
                    response = {'reachable': False}
            else:
                raise ValueError('unknown lab action')
            self.send_response(200)
        except Exception as error:
            detail = str(error)
            for value in credentials:
                detail = detail.replace(value, '[redacted]')
            response = {'error': type(error).__name__, 'detail': detail,
                        'diagnostic': diagnostic() if Path('/tmp/throne.log').exists() else ''}
            self.send_response(500)
        data = json.dumps(response).encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('10.241.13.5', 9000), Handler).serve_forever()
