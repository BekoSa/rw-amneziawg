from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from awg_contracts import TrustTunnelTLS


def pem(domain='vpn.example.com', days=3):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def tls(): return TrustTunnelTLS(domain='vpn.example.com', email='ops@example.com')


def test_bundle_validates_san_private_key_and_expiry(tmp_path):
    from awg_agent.certificates import inspect_bundle, atomic_write
    cert, key = pem()
    data = inspect_bundle(cert, key, tls().domain)
    assert len(data['fingerprint']) == 64
    with pytest.raises(ValueError): inspect_bundle(cert, pem()[1], tls().domain)
    with pytest.raises(ValueError): inspect_bundle(cert, key, 'other.example.com')
    with pytest.raises(ValueError): inspect_bundle(*pem(days=-0.5), tls().domain)
    target = tmp_path / 'key.pem'
    atomic_write(target, key)
    assert target.read_bytes() == key and target.stat().st_mode & 0o777 == 0o600


def test_proportional_renewal_uses_actual_lifetime():
    from awg_agent.certificates import renewal_at
    start = datetime.now(timezone.utc)
    end = start + timedelta(hours=3)
    assert renewal_at(start, end) == start + timedelta(hours=2)


def test_lego_flags_ports_ari_and_ca_allowlist(tmp_path, monkeypatch):
    from awg_agent.certificates import CertificateManager
    monkeypatch.setenv('TT_HTTP_PORT', '80')
    monkeypatch.setenv('TT_TLS_ALPN_PORT', '443')
    manager = CertificateManager(tmp_path)
    http = manager.command(tls(), 'http-01', renew=False)
    assert '--http.address=127.0.0.1:10080' in http
    assert '--tls' not in http
    alpn = manager.command(tls(), 'tls-alpn-01', renew=True)
    assert '--tls.address=127.0.0.1:10443' in alpn
    assert alpn[1] == 'run' and '--ari-disable' not in alpn
    assert '--server=https://acme-v02.api.letsencrypt.org/directory' in http
    monkeypatch.setenv('TT_TEST_ACME_DIRECTORY', 'http://attacker.invalid/directory')
    with pytest.raises(ValueError, match='isolated test'):
        CertificateManager(tmp_path / 'unsafe')


def test_persistent_backoff_and_account_path_survive_restart(tmp_path, monkeypatch):
    from awg_agent.certificates import CertificateManager
    monkeypatch.setenv('TT_HTTP_PORT', '80')
    monkeypatch.setenv('TT_TLS_ALPN_PORT', '0')
    manager = CertificateManager(tmp_path)
    calls = []
    def fail(*args): calls.append(args); raise RuntimeError('secret account data')
    monkeypatch.setattr(manager, '_run', fail)
    assert manager.ensure(tls()) is None
    first = manager.metadata(tls())
    assert first['certificate_error_code'] == 'ACME_FAILED'
    assert first['next_attempt_at'] > datetime.now(timezone.utc)
    manager = CertificateManager(tmp_path)
    monkeypatch.setattr(manager, '_run', fail)
    assert manager.ensure(tls()) is None
    assert len(calls) == 1
    assert manager.metadata(tls())['next_attempt_at'] == first['next_attempt_at']


def test_generation_persisted_and_corrupted_bundle_excluded(tmp_path, monkeypatch):
    from awg_agent.certificates import CertificateManager
    monkeypatch.setenv('TT_HTTP_PORT', '80')
    manager = CertificateManager(tmp_path)
    cert, key = pem()
    def issue(argv):
        root = manager.account_path(tls()) / 'certificates'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'vpn.example.com.crt').write_bytes(cert)
        (root / 'vpn.example.com.key').write_bytes(key)
    monkeypatch.setattr(manager, '_run', issue)
    bundle = manager.ensure(tls())
    assert bundle['certificate_generation'] == 1
    manager = CertificateManager(tmp_path)
    assert manager.current(tls())['fingerprint'] == bundle['fingerprint']
    assert manager.current(tls())['certificate_path'].stat().st_mode & 0o777 == 0o600
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in tmp_path.rglob('*') if path.is_dir())
    manager.current(tls())['certificate_path'].write_bytes(b'corrupt')
    assert manager.current(tls()) is None


def test_failed_renewal_restores_durable_last_served_bundle(tmp_path, monkeypatch):
    from awg_agent.certificates import CertificateManager
    manager = CertificateManager(tmp_path)
    issued = [pem(), pem()]
    def issue(argv):
        root = manager.account_path(tls()) / 'certificates'
        cert, key = issued.pop(0)
        (root / 'vpn.example.com.crt').write_bytes(cert)
        (root / 'vpn.example.com.key').write_bytes(key)
    monkeypatch.setattr(manager, '_run', issue)
    old = manager.ensure(tls())
    manager.mark_served(tls(), old)
    record = manager._load(tls())
    record['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    manager._save(tls(), record)
    new = manager.ensure(tls())
    assert new['fingerprint'] != old['fingerprint']
    restarted = CertificateManager(tmp_path)
    restored = restarted.restore_served(tls())
    assert restored['fingerprint'] == old['fingerprint']
    assert restored['certificate_generation'] == new['certificate_generation']
    assert restarted.metadata(tls())['certificate_error_code'] == 'CERTIFICATE_LOAD_FAILED'
    assert restarted.metadata(tls())['next_attempt_at'] > datetime.now(timezone.utc)


@pytest.mark.parametrize('diagnostic,challenge', [
    ('ERROR Error error="obtain certificate: resolver: one or more domains had a problem: '
     '[vpn.tt.test: invalid authorization: acme: error: 403 :: '
     'urn:ietf:params:acme:error:unauthorized :: Non-200 status code from HTTP: returned 503]"', True),
    ('urn:ietf:params:acme:error:rateLimited: retry after 60', False),
    ('urn:ietf:params:acme:error:serverInternal: CA unavailable', False),
])
def test_stdout_cli_diagnostic_controls_safe_challenge_fallback(tmp_path, diagnostic, challenge):
    import sys
    from awg_agent.certificates import AcmeFailure, CertificateManager
    manager = CertificateManager(tmp_path)
    with pytest.raises(AcmeFailure) as failure:
        manager._run([sys.executable, '-c', 'import sys; print(sys.argv[1]); sys.exit(1)', diagnostic])
    assert failure.value.challenge is challenge
