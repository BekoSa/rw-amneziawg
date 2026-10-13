"""Node-local ACME manager using pinned lego v5 CLI and private durable generations."""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

DIRECTORIES = {'production': 'https://acme-v02.api.letsencrypt.org/directory',
               'staging': 'https://acme-staging-v02.api.letsencrypt.org/directory'}


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data.encode() if isinstance(data, str) else data)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inspect_bundle(certificate, private_key, domain):
    chain = x509.load_pem_x509_certificates(certificate)
    if not chain:
        raise ValueError('invalid certificate bundle')
    cert = chain[0]
    key = serialization.load_pem_private_key(private_key, password=None)
    if cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError('certificate key mismatch')
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        raise ValueError('certificate SAN is missing') from None
    if domain not in names:
        raise ValueError('certificate domain mismatch')
    now = datetime.now(timezone.utc)
    if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
        raise ValueError('certificate is not currently valid')
    for child, issuer in zip(chain, chain[1:]):
        child.verify_directly_issued_by(issuer)
    return {'fingerprint': cert.fingerprint(hashes.SHA256()).hex(),
            'not_before': cert.not_valid_before_utc, 'not_after': cert.not_valid_after_utc}


def renewal_at(not_before, not_after):
    return not_after - (not_after - not_before) / 3


class AcmeFailure(RuntimeError):
    def __init__(self, challenge=False):
        super().__init__('ACME_FAILED')
        self.challenge = challenge


class CertificateManager:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.single_flight = threading.Lock()
        self.publish_lock = threading.RLock()
        self.process_lock = threading.Lock()
        self.process = None
        self.closed = threading.Event()
        self.test_directory = os.environ.get('TT_TEST_ACME_DIRECTORY')
        self.test_ca = os.environ.get('TT_TEST_ACME_CA')
        if self.test_directory or self.test_ca:
            if os.environ.get('TT_ISOLATED_TEST') != '1' or not Path('/.dockerenv').exists():
                raise ValueError('custom ACME configuration requires isolated test Docker')
            parsed = urlsplit(self.test_directory or '')
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('invalid isolated test ACME directory')
            if self.test_ca and not Path(self.test_ca).is_file():
                raise ValueError('invalid isolated test CA path')

    def account_path(self, tls):
        # Test CA/account state cannot be reused by a production deployment.
        namespace = 'isolated-test' if self.test_directory else tls.environment
        return self.directory / namespace / tls.domain

    def _load(self, tls):
        path = self.account_path(tls) / 'state.json'
        if not path.exists():
            return {'certificate_state': 'ABSENT', 'certificate_generation': 0, 'failures': 0}
        record = json.loads(path.read_text())
        for key in ('next_attempt_at', 'not_before', 'not_after'):
            if record.get(key):
                record[key] = datetime.fromisoformat(record[key])
        return record

    def _save(self, tls, record):
        data = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in record.items()}
        with self.publish_lock:
            self._private_directory(self.account_path(tls))
            atomic_write(self.account_path(tls) / 'state.json', json.dumps(data))

    def _private_directory(self, path):
        relative = Path(path).relative_to(self.directory)
        current = self.directory
        for component in relative.parts:
            parent = current
            current = current / component
            current.mkdir(exist_ok=True, mode=0o700)
            current.chmod(0o700)
            for directory in (current, parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def mark_served(self, tls, bundle):
        with self.publish_lock:
            record = self._load(tls)
            if record.get('fingerprint') != bundle['fingerprint']:
                raise ValueError('served bundle differs from current generation')
            data = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in record.items()}
            atomic_write(self.account_path(tls) / 'served.json', json.dumps(data))

    def restore_served(self, tls):
        with self.publish_lock:
            try:
                record = json.loads((self.account_path(tls) / 'served.json').read_text())
                path = self.account_path(tls) / 'bundles' / record['bundle']
                metadata = inspect_bundle((path / 'certificate.pem').read_bytes(),
                                          (path / 'key.pem').read_bytes(), tls.domain)
                if metadata['fingerprint'] != record['fingerprint']:
                    return None
                current = self._load(tls)
                if current.get('fingerprint') == metadata['fingerprint']:
                    return self.current(tls)
                record['certificate_generation'] = max(record.get('certificate_generation', 0),
                                                      current.get('certificate_generation', 0))
                record.update(metadata, certificate_state='ERROR', certificate_error_code='CERTIFICATE_LOAD_FAILED',
                              next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=5))
                self._save(tls, record)
                return self.current(tls)
            except (OSError, ValueError, KeyError):
                return None

    def validate(self, tls):
        methods = self.methods(tls)
        if not methods:
            raise ValueError('no permitted ACME challenge public port is available')

    def methods(self, tls, previous=None):
        available = []
        provider = getattr(self, 'available_methods', None)
        if provider is not None:
            available = provider()
        elif int(os.environ.get('TT_HTTP_PORT', '80')) > 0:
            available.append('http-01')
        if provider is None and int(os.environ.get('TT_TLS_ALPN_PORT', '443')) > 0:
            available.append('tls-alpn-01')
        if tls.challenge != 'auto':
            if tls.challenge not in available:
                raise ValueError('requested ACME challenge public port is unavailable')
            return [tls.challenge]
        if previous in available:
            available.remove(previous)
            available.insert(0, previous)
        return available

    def command(self, tls, method, renew=False):
        if method not in self.methods(tls):
            raise ValueError('ACME challenge not allowed')
        # lego5 unified run automatically obtains or renews; ARI remains enabled.
        argv = ['lego', 'run', '--accept-tos', '--path=' + str(self.account_path(tls)),
                '--email=' + tls.email, '--domains=' + tls.domain,
                '--server=' + (self.test_directory or DIRECTORIES[tls.environment]),
                '--http-timeout=30', '--cert.timeout=90', '--no-random-sleep']
        if method == 'http-01':
            argv += ['--http', '--http.address=127.0.0.1:10080']
        else:
            argv += ['--tls', '--tls.address=127.0.0.1:10443']
        return argv

    def _run(self, argv):
        if self.closed.is_set():
            raise AcmeFailure()
        # Explicit environment prevents ambient LEGO_* flags from bypassing CA/solver policy.
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': str(self.directory)}
        if self.test_ca:
            env['LEGO_CA_CERTIFICATES'] = self.test_ca
        with tempfile.TemporaryFile(dir=self.directory) as errors:
            with self.process_lock:
                self.process = subprocess.Popen(argv, stdout=errors, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True, umask=0o077)
                process = self.process
            try:
                process.wait(timeout=150)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise AcmeFailure(challenge=True) from None
            finally:
                with self.process_lock:
                    self.process = None
            if process.returncode:
                errors.seek(0)
                diagnostic = errors.read(131072).decode(errors='replace').lower()
                # Account/order/rate-limit errors never trigger a fallback order storm.
                challenge = any(code in diagnostic for code in ('urn:ietf:params:acme:error:connection',
                    'urn:ietf:params:acme:error:unauthorized', 'challenge failed', 'challenge validation'))
                raise AcmeFailure(challenge=challenge)

    def current(self, tls):
        with self.publish_lock:
            record = self._load(tls)
            generation = record.get('bundle')
            if not generation:
                return None
            path = self.account_path(tls) / 'bundles' / generation
            try:
                metadata = inspect_bundle((path / 'certificate.pem').read_bytes(), (path / 'key.pem').read_bytes(), tls.domain)
                if metadata['fingerprint'] != record.get('fingerprint'):
                    return None
                return {**record, **metadata, 'certificate_path': path / 'certificate.pem', 'key_path': path / 'key.pem'}
            except Exception:
                return None

    def metadata(self, tls):
        with self.publish_lock:
            record = self._load(tls)
            if record.get('not_after') and record['not_after'] <= datetime.now(timezone.utc):
                record['certificate_state'] = 'EXPIRED'
            return record

    def ensure(self, tls):
        if not self.single_flight.acquire(blocking=False):
            return self.current(tls)
        try:
            self.validate(tls)
            record = self._load(tls)
            now = datetime.now(timezone.utc)
            if record.get('next_attempt_at') and now < record['next_attempt_at']:
                return self.current(tls)
            previous = self.current(tls)
            record['certificate_state'] = 'RENEWING' if previous else 'ISSUING'
            # Durable attempt reservation before starting an account/order request.
            record['next_attempt_at'] = now + timedelta(minutes=5)
            self._save(tls, record)
            methods = self.methods(tls, record.get('method'))
            try:
                self._private_directory(self.account_path(tls) / 'certificates')
                for index, method in enumerate(methods):
                    try:
                        self._run(self.command(tls, method, renew=previous is not None))
                        break
                    except AcmeFailure as exc:
                        if not exc.challenge or index == len(methods) - 1:
                            raise
                root = self.account_path(tls) / 'certificates'
                certificate = (root / (tls.domain + '.crt')).read_bytes()
                private_key = (root / (tls.domain + '.key')).read_bytes()
                metadata = inspect_bundle(certificate, private_key, tls.domain)
                with self.publish_lock:
                    if metadata['fingerprint'] != record.get('fingerprint'):
                        generation = uuid4().hex
                        bundle_path = self.account_path(tls) / 'bundles' / generation
                        self._private_directory(bundle_path)
                        atomic_write(bundle_path / 'certificate.pem', certificate)
                        atomic_write(bundle_path / 'key.pem', private_key)
                        record.update(bundle=generation, certificate_generation=record.get('certificate_generation', 0) + 1)
                    lifetime = metadata['not_after'] - metadata['not_before']
                    # Frequent proportional checks let pinned lego evaluate ARI even before fallback due.
                    check = min(timedelta(hours=12), lifetime / 12)
                    check = max(timedelta(seconds=5), check)
                    record.update(metadata, certificate_state='VALID', failures=0, method=method,
                                  certificate_error_code=None,
                                  certificate_environment='staging' if self.test_directory else tls.environment,
                                  next_attempt_at=now + check + timedelta(seconds=random.uniform(0, min(3600, check.total_seconds()/12))))
                    self._save(tls, record)
            except Exception:
                failures = record.get('failures', 0) + 1
                delay = min(21600, 300 * 2 ** min(failures - 1, 8))
                record.update(certificate_state='ERROR', certificate_error_code='ACME_FAILED', failures=failures,
                    next_attempt_at=now + timedelta(seconds=delay + random.uniform(0, delay / 10)))
                self._save(tls, record)
            return self.current(tls)
        finally:
            self.single_flight.release()

    def close(self):
        self.closed.set()
        with self.process_lock:
            if self.process and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
