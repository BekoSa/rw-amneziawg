"""Mihomo SOCKS harness: no TUN, routes, shell or network capabilities."""
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import yaml

process = None
sessions = {}
credentials = ()
process_family = 'mihomo'
os.umask(0o077)


def exact(sock, count):
    data = b''
    while len(data) < count:
        block = sock.recv(count - len(data))
        if not block:
            raise ConnectionError('closed')
        data += block
    return data


def address(host, port):
    return b'\x01' + ipaddress.ip_address(host).packed + struct.pack('!H', port)


def socks(command, host, port):
    stream = socket.create_connection(('127.0.0.1', 1080), 3)
    stream.settimeout(3)
    stream.sendall(b'\x05\x01\x00')
    assert exact(stream, 2) == b'\x05\x00'
    stream.sendall(b'\x05' + bytes([command]) + b'\x00' + address(host, port))
    reply = exact(stream, 4)
    assert reply[:2] == b'\x05\x00', 'SOCKS request rejected'
    if reply[3] == 1:
        relay = socket.inet_ntoa(exact(stream, 4))
    elif reply[3] == 4:
        relay = socket.inet_ntop(socket.AF_INET6, exact(stream, 16))
    else:
        relay = exact(stream, exact(stream, 1)[0]).decode()
    return stream, relay, struct.unpack('!H', exact(stream, 2))[0]


def probe(host='192.0.0.9', port=8080):
    result = {'http': False, 'udp': False}
    try:
        with socks(1, host, port)[0] as stream:
            stream.sendall(b'GET / HTTP/1.0\r\nHost: target\r\n\r\n')
            data = b''
            while block := stream.recv(4096):
                data += block
            result['http'] = b'trusttunnel-real-payload' in data
    except (OSError, AssertionError):
        pass
    try:
        control, relay, relay_port = socks(3, '0.0.0.0', 0)
        with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(3)
            payload = b'trusttunnel-udp-real-payload'
            udp.sendto(b'\x00\x00\x00' + address(host, 8081) + payload, (relay, relay_port))
            result['udp'] = udp.recv(4096).endswith(payload)
    except (OSError, AssertionError):
        pass
    return result


def action(path, body):
    global process, credentials, process_family
    if path == '/udp-isolation':
        # Two distinct source sockets must receive only their own backend replies.
        left, right = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            left.settimeout(3)
            right.settimeout(3)
            destination = ('10.242.20.2', body['port'])
            for index in range(16):
                a, b = b'left-' + str(index).encode(), b'right-' + str(index).encode()
                left.sendto(a, destination)
                right.sendto(b, destination)
                if left.recv(65535) != a or right.recv(65535) != b:
                    return {'isolated': False}
            return {'isolated': True}
        finally:
            left.close()
            right.close()
    if path == '/configure':
        if process:
            process.terminate()
            process.wait(timeout=5)
        proxy = {'name': 'TT', 'type': 'trusttunnel', 'server': '10.242.20.2',
                 'port': body.get('port', 2443), 'sni': body.get('domain', 'vpn.tt.test'),
                 'username': body['username'], 'password': body['password'], 'udp': True,
                 'quic': body.get('quic', False)}
        config = {'socks-port': 1080, 'allow-lan': False, 'mode': 'rule', 'log-level': 'warning',
                  'ipv6': False, 'proxies': [proxy], 'rules': ['MATCH,TT'],
                  'dns': {'enable': False},
                  # v1.19.32 spells this field custom-certifactes; outbound certificate
                  # is a client TLS identity, not a root trust store.
                  'tls': {'custom-certifactes': [body['ca']]}}
        Path('/tmp/mihomo.yaml').write_text(yaml.safe_dump(config))
        Path('/tmp/mihomo-ca.pem').write_text(body['ca'])
        credentials = (body['username'], body['password'])
        process_family = 'mihomo'
        with Path('/tmp/mihomo.log').open('wb') as log:
            process = subprocess.Popen(['mihomo', '-d', '/tmp', '-f', '/tmp/mihomo.yaml'],
                stdout=log, stderr=log, env=os.environ | {'SSL_CERT_FILE': '/tmp/mihomo-ca.pem'})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            assert process.poll() is None, 'Mihomo configuration rejected'
            try:
                with socket.create_connection(('127.0.0.1', 1080), .2):
                    break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError('Mihomo SOCKS readiness timeout')
        return {'configured': True}
    if path == '/native':
        if process:
            process.terminate()
            process.wait(timeout=5)
        Path('/tmp/endpoint.toml').write_text(body['toml'])
        subprocess.run(['setup_wizard', '--mode', 'non-interactive', '--endpoint_config',
            '/tmp/endpoint.toml', '--settings', '/tmp/native.toml'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        config = Path('/tmp/native.toml').read_text()
        # Replace the wizard default TUN listener with SOCKS before starting the client.
        # No TUN device, capabilities, host routes or DNS changes are available.
        config = re.sub(r'(?ms)^\[listener\.tun\].*?(?=^\[|\Z)', '', config)
        config += '\n[listener.socks]\naddress = "127.0.0.1:1080"\n'
        assert tomllib.loads(config)['endpoint']['skip_verification'] is False
        Path('/tmp/native.toml').write_text(config)
        credentials = tuple(tomllib.loads(config)['endpoint'][field] for field in ('username', 'password'))
        process_family = 'native'
        with Path('/tmp/native.log').open('wb') as log:
            process = subprocess.Popen(['trusttunnel_client', '--config', '/tmp/native.toml'],
                stdout=log, stderr=log)
        time.sleep(.5)
        if process.poll() is not None:
            diagnostic = Path('/tmp/native.log').read_text(errors='replace')
            for field in ('password', 'username'):
                diagnostic = diagnostic.replace(tomllib.loads(config)['endpoint'][field], '[redacted]')
            diagnostic = '\n'.join(line for line in diagnostic.splitlines() if 'ERROR' in line)
            return {'configured': False, 'diagnostic': diagnostic[-3000:]}
        return {'configured': True}
    if path == '/diagnostic':
        message = Path('/tmp/' + process_family + '.log').read_text(errors='replace')[-4000:]
        for value in credentials:
            message = message.replace(value, '[redacted]')
        return {'client': process_family, 'diagnostic': message}
    if path == '/probe':
        return probe(**body)
    if path == '/tls':
        # Produce a real ClientHello and split its bytes across TCP writes.
        context = ssl.create_default_context(cadata=body['ca'])
        context.set_alpn_protocols(body['alpn'])
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = context.wrap_bio(incoming, outgoing, server_side=False, server_hostname=body['domain'])
        try:
            with socket.create_connection(('10.242.20.2', body.get('port', 443)), 3) as stream:
                stream.settimeout(6)
                initial = True
                while True:
                    done = False
                    try:
                        tls.do_handshake()
                        done = True
                    except ssl.SSLWantReadError:
                        pass
                    data = outgoing.read()
                    if data:
                        if initial and body.get('fragmented'):
                            for start in range(0, len(data), 7):
                                stream.sendall(data[start:start + 7])
                                time.sleep(.002)
                        else:
                            stream.sendall(data)
                        initial = False
                    if done:
                        return {'connected': True, 'alpn': tls.selected_alpn_protocol()}
                    data = stream.recv(65536)
                    if not data:
                        raise ConnectionError('closed')
                    incoming.write(data)
        except (OSError, ssl.SSLError, ConnectionError):
            return {'connected': False, 'alpn': None}
    if path == '/direct':
        try:
            with socket.create_connection(('192.0.0.9', 8080), 1):
                return {'reachable': True}
        except OSError:
            return {'reachable': False}
    if path == '/reach':
        try:
            with socks(1, body['host'], body['port'])[0] as stream:
                # SOCKS CONNECT alone is optimistic in some cores; demand real bytes.
                stream.sendall(b'POST /health HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}')
                return {'reachable': bool(stream.recv(4096))}
        except (OSError, AssertionError):
            return {'reachable': False}
    if path == '/session/open':
        sessions[body['id']] = socks(1, '192.0.0.9', 8082)[0]
        return {'open': True}
    if path == '/session/probe':
        try:
            stream = sessions[body['id']]
            stream.sendall(b'persist')
            return {'alive': exact(stream, 7) == b'persist'}
        except (OSError, ConnectionError):
            return {'alive': False}
    raise ValueError('unknown lab action')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            response = action(self.path, body)
            self.send_response(200)
        except Exception as error:
            response = {'error': type(error).__name__}
            self.send_response(500)
        data = json.dumps(response).encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('10.242.23.3', 9000), Handler).serve_forever()
