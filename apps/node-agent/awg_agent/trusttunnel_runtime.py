"""Bounded child-process supervision and HAProxy raw TLS ingress inside Docker."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from awg_contracts import trusttunnel_digest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from .certificates import atomic_write


def trusted_native_chain(pem, ca_file=None):
    """Complete an exported chain with a verified root from the selected trusted store."""
    chain = x509.load_pem_x509_certificates(pem)
    if not chain:
        raise ValueError('native certificate chain is empty')
    # Explicit default cafile prevents ambient SSL_CERT_FILE from changing production trust.
    context = ssl.create_default_context(cafile=ca_file or ssl.get_default_verify_paths().openssl_cafile)
    roots = [x509.load_der_x509_certificate(der) for der in context.get_ca_certs(binary_form=True)]
    now = datetime.now(timezone.utc)
    for certificate in chain:
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise ValueError('native certificate chain is expired or not yet valid')
    for child, issuer in zip(chain, chain[1:]):
        try:
            child.verify_directly_issued_by(issuer)
        except InvalidSignature:
            raise ValueError('native certificate chain signature is invalid') from None
        if not issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError('native certificate issuer is not a CA')
    last = chain[-1]
    for root in roots:
        try:
            if not root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                continue
            root.verify_directly_issued_by(root)
            if not root.not_valid_before_utc <= now < root.not_valid_after_utc:
                continue
            if last.fingerprint(hashes.SHA256()) == root.fingerprint(hashes.SHA256()):
                return pem
            last.verify_directly_issued_by(root)
            return pem.rstrip() + b'\n' + root.public_bytes(serialization.Encoding.PEM)
        except (ValueError, TypeError, InvalidSignature, x509.ExtensionNotFound):
            continue
    raise ValueError('native certificate chain has no verified trusted root')


def validate_ports(data, http, alpn):
    if type(data) is not int or not 1 <= data <= 65535 or data in (80, 8443, 9443, 10080, 10443):
        raise ValueError('invalid or reserved TrustTunnel data port')
    if http not in (0, 80) or alpn not in (0, 443):
        raise ValueError('ACME public ports must be 80/443 or disabled')


def ingress_config(data, http, alpn, *, managed=False):
    if managed:
        if (data,http,alpn)!=(8444,8080,8444): raise ValueError('invalid managed frontend roles')
    else:
        validate_ports(data, http, alpn)
    binds = sorted({data} | ({alpn} if alpn else set()))
    config = ['global', '  maxconn 4096', '  tune.bufsize 65536',
              'defaults', '  mode tcp', '  timeout connect 5s', '  timeout client 1h',
              '  timeout server 1h', 'frontend transport']
    config += [f'  bind 0.0.0.0:{port}' for port in binds]
    config += ['  tcp-request inspect-delay 5s',
               '  tcp-request content accept if { req.ssl_hello_type 1 }',
               '  tcp-request content reject if WAIT_END',
               '  use_backend acme_tls if { req.ssl_alpn acme-tls/1 }',
               '  default_backend trusttunnel', 'backend acme_tls',
               '  server solver 127.0.0.1:10443', 'backend trusttunnel',
               '  server endpoint 127.0.0.1:9443']
    if http:
        config += ['frontend challenge_http', '  mode http', f'  bind 0.0.0.0:{http}',
                   '  acl challenge_path url -m reg ^/\\.well-known/acme-challenge/[A-Za-z0-9_-]+$',
                   '  http-request return status 404 unless METH_GET challenge_path',
                   '  default_backend acme_http', 'backend acme_http', '  mode http',
                   '  server solver 127.0.0.1:10080']
    return '\n'.join(config) + '\n'


def write_configuration(directory, desired, bundle, *, allow_private=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # JSON quoted Unicode strings are also TOML basic strings; validated inputs exclude controls.
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    files = {name: directory / (name + '.toml') for name in ('vpn', 'hosts', 'credentials')}
    credentials = '\n'.join('[[client]]\nusername = ' + quote(client.username) + '\npassword = ' +
        quote(client.password.get_secret_value()) + '\n' for client in desired.clients)
    vpn = '\n'.join(['listen_address = "0.0.0.0:9443"', 'ipv6_available = true',
        'allow_private_network_connections = ' + str(allow_private).lower(),
        'credentials_file = ' + quote(files['credentials']), 'tls_handshake_timeout_secs = 10',
        'connection_establishment_timeout_secs = 15', '[listen_protocols.http2]',
        '[listen_protocols.quic]', '[forward_protocol]', 'direct = {}', ''])
    hosts = '\n'.join(['[[main_hosts]]', 'hostname = ' + quote(desired.profile.tls.domain),
        'cert_chain_path = ' + quote(bundle['certificate_path']), 'private_key_path = ' + quote(bundle['key_path']), ''])
    atomic_write(files['credentials'], credentials)
    atomic_write(files['vpn'], vpn)
    atomic_write(files['hosts'], hosts)
    return files


class TrustTunnelRuntime:
    def __init__(self, directory, *, managed=False):
        if os.environ.get('TT_ISOLATED_RUNTIME') != '1' or not Path('/.dockerenv').exists():
            raise RuntimeError('TrustTunnel runtime requires an explicitly isolated Docker container')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.managed = managed
        self.data_port = 8444 if managed else int(os.environ.get('TT_DATA_PORT', '443'))
        self.http_port = 8080 if managed else int(os.environ.get('TT_HTTP_PORT', '80'))
        self.alpn_port = 8444 if managed else int(os.environ.get('TT_TLS_ALPN_PORT', '443'))
        if not self.managed: validate_ports(self.data_port, self.http_port, self.alpn_port)
        self.allow_private = os.environ.get('TT_TEST_ALLOW_PRIVATE') == '1'
        self.test = os.environ.get('TT_ISOLATED_TEST') == '1'
        if self.allow_private and not self.test:
            raise ValueError('private targets require isolated test mode')
        self.test_ca = os.environ.get('TT_TEST_ACME_CA') if self.test else None
        self.lock = threading.RLock()
        self.endpoint = self.ingress = None
        self.config = None
        self.desired_digest = self.fingerprint = None
        self.epoch = uuid4()
        # Capabilities must correspond to pinned installed binaries.
        try:
            version = subprocess.run(['trusttunnel_endpoint', '--version'], check=True, capture_output=True, timeout=10).stdout.decode()
            lego = subprocess.run(['lego', '--version'], check=True, capture_output=True, timeout=10).stdout.decode()
            if '1.1.0' not in version or '5.5.2' not in lego:
                raise RuntimeError('incompatible TrustTunnel toolchain')
        except Exception:
            raise RuntimeError('pinned TrustTunnel toolchain is unavailable') from None

    def validate(self, desired):
        if not self.managed and desired.profile.endpoint.port != self.data_port:
            raise ValueError('TrustTunnel endpoint port differs from installed public data port')
        if not self.managed: validate_ports(desired.profile.endpoint.port, self.http_port, self.alpn_port)

    @staticmethod
    def _terminate(process):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    @staticmethod
    def _spawn(argv):
        # Subprocess logs can contain credentials/exported config; discard them completely.
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, umask=0o077)

    def ensure_ingress(self):
        with self.lock:
            if self.ingress is not None and self.ingress.poll() is None:
                return
            path = self.directory / 'haproxy.cfg'
            atomic_write(path, ingress_config(self.data_port, self.http_port, self.alpn_port, managed=self.managed))
            try:
                subprocess.run(['haproxy', '-c', '-f', str(path)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                self.ingress = self._spawn(['haproxy', '-db', '-f', str(path)])
                time.sleep(0.1)
                if self.ingress.poll() is not None:
                    raise RuntimeError('ingress failed')
            except Exception:
                raise RuntimeError('TrustTunnel ingress unavailable') from None

    def _start_endpoint(self):
        self._terminate(self.endpoint)
        self.endpoint = self._spawn(['trusttunnel_endpoint', str(self.config['vpn']), str(self.config['hosts']),
                                     '--loglvl', 'info'])
        self.epoch = uuid4()

    def served_fingerprint(self, desired, bundle):
        if bundle is None:
            return None
        try:
            context = ssl.create_default_context(cafile=self.test_ca)
            context.set_alpn_protocols(['h2'])
            with socket.create_connection(('127.0.0.1', self.data_port), timeout=2) as connection:
                with context.wrap_socket(connection, server_hostname=desired.profile.tls.domain) as tls:
                    return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        except (OSError, ssl.SSLError, ValueError):
            return None

    def _probe(self, desired, bundle):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.served_fingerprint(desired, bundle) == bundle['fingerprint']:
                return True
            if self.endpoint is None or self.endpoint.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def apply(self, desired, bundle):
        with self.lock:
            self.validate(desired)
            self.ensure_ingress()
            digest = trusttunnel_digest(desired)
            running = self.endpoint is not None and self.endpoint.poll() is None
            if running and digest == self.desired_digest and bundle['fingerprint'] == self.fingerprint:
                if self._probe(desired, bundle):
                    return
            # New immutable config generation, with private secrets committed before process switch.
            directory = self.directory / 'configs' / uuid4().hex
            files = write_configuration(directory, desired, bundle, allow_private=self.allow_private)
            if running and digest == self.desired_digest:
                # Pinned SIGHUP reloads only hosts; credentials changes always terminate sessions.
                atomic_write(self.config['hosts'], files['hosts'].read_bytes())
                os.kill(self.endpoint.pid, signal.SIGHUP)
                if self._probe(desired, bundle):
                    self.fingerprint = bundle['fingerprint']
                    return
            self.config = files
            self._start_endpoint()
            if not self._probe(desired, bundle):
                self._terminate(self.endpoint)
                self.endpoint = None
                raise RuntimeError('TrustTunnel served certificate verification failed')
            self.desired_digest, self.fingerprint = digest, bundle['fingerprint']

    def healthy(self, desired, bundle):
        with self.lock:
            return bool(self.endpoint and self.endpoint.poll() is None and self.ingress and self.ingress.poll() is None
                and self.desired_digest == trusttunnel_digest(desired) and bundle
                and self.served_fingerprint(desired, bundle) == bundle['fingerprint'])

    @contextmanager
    def native_hosts(self):
        # The official native client uses only its embedded CA store, without platform roots.
        hosts = tomllib.loads(self.config['hosts'].read_text())['main_hosts']
        with tempfile.TemporaryDirectory(prefix='native-export-', dir=self.directory) as temporary:
            directory = Path(temporary)
            lines = []
            for index, host in enumerate(hosts):
                certificate = directory / f'chain-{index}.pem'
                atomic_write(certificate, trusted_native_chain(Path(host['cert_chain_path']).read_bytes(), self.test_ca))
                host['cert_chain_path'] = str(certificate)
                lines.append('[[main_hosts]]')
                lines.extend(key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in host.items())
            path = directory / 'hosts.toml'
            atomic_write(path, '\n'.join(lines) + '\n')
            yield path

    def export(self, desired, peer, format):
        with self.lock:
            if format not in ('deeplink', 'toml') or not self.config:
                raise ValueError('unsupported native export')
            try:
                with self.native_hosts() as hosts:
                    result = subprocess.run(['trusttunnel_endpoint', str(self.config['vpn']), str(hosts),
                        '-c', peer.username, '-a', f'{desired.profile.endpoint.host}:{desired.profile.endpoint.port}',
                        '--format', format, '--name', desired.profile.name], check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
                if not result.stdout or len(result.stdout) > 1048576:
                    raise RuntimeError('invalid export')
                content = result.stdout.decode().strip()
                if format == 'deeplink':
                    links = [line for line in content.splitlines() if line.startswith('tt://?')]
                    if len(links) != 1 or any(char.isspace() for char in links[0]):
                        raise RuntimeError('invalid native deeplink')
                    return links[0]
                return content
            except Exception:
                raise RuntimeError('TrustTunnel native export failed') from None

    def stop(self):
        with self.lock:
            self._terminate(self.endpoint)
            self.endpoint = None
            self.desired_digest = None

    def close(self):
        with self.lock:
            self.stop()
            self._terminate(self.ingress)
            self.ingress = None
