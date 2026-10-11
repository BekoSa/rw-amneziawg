import tomllib
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.unit.agent.test_trusttunnel import desired


def test_ingress_is_raw_tls_no_proxy_header_and_fail_closed():
    from awg_agent.trusttunnel_runtime import ingress_config
    config = ingress_config(14443, 80, 443)
    assert 'bind 0.0.0.0:14443' in config and 'bind 0.0.0.0:443' in config
    assert 'req.ssl_alpn acme-tls/1' in config
    assert '127.0.0.1:10443' in config and '127.0.0.1:10080' in config
    assert 'send-proxy' not in config and ' ssl ' not in config
    assert 'tcp-request content reject if WAIT_END' in config
    assert 'http-request return status 404' in config
    assert '127.0.0.1:9443' in config


def test_runtime_configs_parse_and_secrets_only_in_private_files(tmp_path):
    from awg_agent.trusttunnel_runtime import write_configuration
    config = desired()
    config.clients[0].password = 'secret"\\special' * 4
    bundle = {'certificate_path': tmp_path / 'cert.pem', 'key_path': tmp_path / 'key.pem'}
    files = write_configuration(tmp_path / 'config', config, bundle, allow_private=False)
    vpn = tomllib.loads(files['vpn'].read_text())
    assert vpn['listen_address'] == '0.0.0.0:9443'
    assert not vpn['allow_private_network_connections']
    clients = tomllib.loads(files['credentials'].read_text())['client']
    assert clients[0]['password'] == config.clients[0].password.get_secret_value()
    assert set(vpn['listen_protocols']) == {'http2', 'quic'}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files.values())


def test_runtime_refuses_host_and_conflicting_transport(monkeypatch, tmp_path):
    from awg_agent.trusttunnel_runtime import TrustTunnelRuntime, validate_ports
    monkeypatch.delenv('TT_ISOLATED_RUNTIME', raising=False)
    with pytest.raises(RuntimeError, match='isolated Docker'):
        TrustTunnelRuntime(tmp_path)
    for port in (80, 8443, 9443, 10080, 10443):
        with pytest.raises(ValueError): validate_ports(port, 80, 443)


def test_native_deeplink_export_strips_upstream_qr_footer(monkeypatch, tmp_path):
    from awg_agent.trusttunnel_runtime import TrustTunnelRuntime
    runtime = object.__new__(TrustTunnelRuntime)
    runtime.lock = threading.RLock()
    runtime.config = {'vpn': tmp_path / 'vpn.toml', 'hosts': tmp_path / 'hosts.toml'}
    @contextmanager
    def native_hosts():
        yield runtime.config['hosts']
    runtime.native_hosts = native_hosts
    monkeypatch.setattr('awg_agent.trusttunnel_runtime.subprocess.run', lambda *a, **kw:
        SimpleNamespace(stdout=b'tt://?AAEBAQ\n\nTo connect on mobile, scan QR: https://trusttunnel.org/qr.html#tt=AAEBAQ\n'))
    config = desired()
    assert runtime.export(config, config.clients[0], 'deeplink') == 'tt://?AAEBAQ'


def certificate_chain(include_key=False):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(3)]
    names = [x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
             for name in ('root', 'intermediate', 'vpn.example.com')]
    now = datetime.now(timezone.utc)
    certificates = []
    for index in range(3):
        issuer = max(0, index - 1)
        certificates.append(x509.CertificateBuilder().subject_name(names[index]).issuer_name(names[issuer])
            .public_key(keys[index].public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=index < 2, path_length=None), critical=True)
            .sign(keys[issuer], hashes.SHA256()))
    chain = [cert.public_bytes(serialization.Encoding.PEM) for cert in certificates]
    if include_key:
        chain.append(keys[-1].private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return chain


def test_native_chain_requires_verified_trusted_root_and_preserves_complete_chain(tmp_path):
    from awg_agent.trusttunnel_runtime import trusted_native_chain
    root, intermediate, leaf = certificate_chain()
    ca = tmp_path / 'ca.pem'
    ca.write_bytes(root)
    assert trusted_native_chain(leaf + intermediate, str(ca)) == leaf + intermediate + root
    assert trusted_native_chain(leaf + intermediate + root, str(ca)) == leaf + intermediate + root
    unrelated = tmp_path / 'other.pem'
    unrelated.write_bytes(certificate_chain()[0])
    with pytest.raises(ValueError, match='trusted root'):
        trusted_native_chain(leaf + intermediate, str(unrelated))
    with pytest.raises(ValueError):
        trusted_native_chain(leaf + certificate_chain()[1], str(ca))


def test_native_production_anchor_ignores_ambient_ca_override(tmp_path, monkeypatch):
    from awg_agent.trusttunnel_runtime import trusted_native_chain
    root, intermediate, leaf = certificate_chain()
    ca = tmp_path / 'system.pem'
    ca.write_bytes(root)
    unrelated = tmp_path / 'ambient.pem'
    unrelated.write_bytes(certificate_chain()[0])
    monkeypatch.setenv('SSL_CERT_FILE', str(unrelated))
    monkeypatch.setattr('awg_agent.trusttunnel_runtime.ssl.get_default_verify_paths',
                        lambda: SimpleNamespace(openssl_cafile=str(ca)))
    assert trusted_native_chain(leaf + intermediate) == leaf + intermediate + root


def test_native_hosts_augments_only_exporter_chain_and_removes_private_temp_files(tmp_path):
    from awg_agent.trusttunnel_runtime import TrustTunnelRuntime, write_configuration
    root, intermediate, leaf = certificate_chain()
    ca = tmp_path / 'ca.pem'
    ca.write_bytes(root)
    certificate = tmp_path / 'served.pem'
    certificate.write_bytes(leaf + intermediate)
    runtime = object.__new__(TrustTunnelRuntime)
    runtime.directory = tmp_path
    runtime.test_ca = str(ca)
    runtime.config = write_configuration(tmp_path / 'served', desired(),
        {'certificate_path': certificate, 'key_path': tmp_path / 'key.pem'})
    original = runtime.config['hosts'].read_bytes()
    with runtime.native_hosts() as path:
        host = tomllib.loads(path.read_text())['main_hosts'][0]
        exported_certificate = __import__('pathlib').Path(host['cert_chain_path'])
        assert exported_certificate.read_bytes() == leaf + intermediate + root
        assert exported_certificate.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert host['private_key_path'] == str(tmp_path / 'key.pem')
        assert runtime.config['hosts'].read_bytes() == original
        assert certificate.read_bytes() == leaf + intermediate
    assert not path.exists() and not exported_certificate.exists()
