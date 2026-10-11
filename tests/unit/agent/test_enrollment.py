import base64
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from awg_agent.enroll import PREFIX, enroll


def node_key(port):
    node = uuid4()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test')])
    now = datetime.now(timezone.utc)
    def certificate(public, ca=False):
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(public).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)))
        if ca:
            builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        else:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(f'{node}.agents.awg.internal')]), critical=False)
        return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()
    payload = {'node_id': str(node), 'controller_id': str(uuid4()), 'management_port': port,
               'ca': certificate(key.public_key(), True), 'cert': certificate(key.public_key()),
               'key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()}
    return PREFIX + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


@pytest.mark.parametrize('port', [0, 65536, '8443', True, False])
def test_invalid_port_preserves_all_existing_pki(tmp_path, port):
    for name in ('ca.crt', 'agent.crt', 'agent.key', 'node.env'):
        (tmp_path / name).write_bytes(('original ' + name).encode())
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match='management port'):
        enroll(node_key(port), tmp_path)
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()} == before


def test_invalid_port_does_not_create_state_directory(tmp_path):
    target = tmp_path / 'absent'
    with pytest.raises(ValueError, match='management port'):
        enroll(node_key(0), target)
    assert not target.exists()


def test_valid_enrollment_publishes_private_key_and_management_port(tmp_path):
    enroll(node_key(9444), tmp_path)
    assert 'AWG_NODE_MANAGEMENT_PORT=9444\n' in (tmp_path / 'node.env').read_text()
    assert (tmp_path / 'agent.key').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('after_publish', [False, True])
def test_reenrollment_crash_never_exposes_mixed_pki(tmp_path, monkeypatch, after_publish):
    import awg_agent.enroll as module
    first = node_key(9444)
    enroll(first, tmp_path)
    names = ('ca.crt', 'agent.crt', 'agent.key', 'node.env')
    before = {name: (tmp_path / name).read_bytes() for name in names}
    original = os.replace
    injected = False
    def interrupted(source, target):
        nonlocal injected
        assert target == tmp_path / 'current'
        injected = True
        if after_publish:
            original(source, target)
        raise OSError('injected crash at generation publish')
    monkeypatch.setattr(module.os, 'replace', interrupted)
    with pytest.raises(OSError):
        enroll(node_key(9555), tmp_path)
    assert injected, 'fault injection must execute'
    after = {name: (tmp_path / name).read_bytes() for name in names}
    assert after == before or all(after[name] != before[name] for name in names)
    certificate = x509.load_pem_x509_certificate(after['agent.crt'])
    key = serialization.load_pem_private_key(after['agent.key'], None)
    assert certificate.public_key().public_numbers() == key.public_key().public_numbers()
    monkeypatch.setattr(module.os, 'replace', original)
    enroll(node_key(9666), tmp_path)
    assert 'AWG_NODE_MANAGEMENT_PORT=9666\n' in (tmp_path / 'node.env').read_text()


@pytest.mark.parametrize('failure_at', range(1, 7))
def test_legacy_migration_crash_keeps_original_identity(tmp_path, monkeypatch, failure_at):
    import awg_agent.enroll as module
    enroll(node_key(9444), tmp_path)
    names = ('ca.crt', 'agent.crt', 'agent.key', 'node.env')
    before = {name: (tmp_path / name).read_bytes() for name in names}
    for name in names:
        path = tmp_path / name
        path.unlink()
        path.write_bytes(before[name])
    (tmp_path / 'current').unlink()
    original = os.replace
    count = 0
    def interrupted(source, target):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError('injected migration crash')
        return original(source, target)
    monkeypatch.setattr(module.os, 'replace', interrupted)
    with pytest.raises(OSError):
        enroll(node_key(9555), tmp_path)
    assert count == failure_at, 'migration fault injection must execute'
    assert {name: (tmp_path / name).read_bytes() for name in names} == before


@pytest.mark.parametrize('existing', ['fresh', 'atomic', 'legacy'])
def test_root_unpack_publishes_secure_bundle_readable_by_installer_owner(tmp_path, existing):
    """Docker root may migrate legacy PKI, but the invoking installer must read it."""
    if os.geteuid() != 0:
        pytest.skip('ownership reproduction requires isolated root test container')
    import subprocess
    import sys
    target = tmp_path / 'pki'
    target.mkdir()
    if existing != 'fresh':
        enroll(node_key(9444), target)
    if existing == 'atomic':
        (target / '.enrollment').chmod(0o700)
        (target / 'current').resolve().chmod(0o700)
    if existing == 'legacy':
        for name in ('ca.crt', 'agent.crt', 'agent.key', 'node.env'):
            path = target / name
            value = path.read_bytes()
            path.unlink()
            path.write_bytes(value)
            path.chmod(0o600 if name == 'agent.key' else 0o644)
        (target / 'current').unlink()
    # A bind-mounted caller directory is owned by the unprivileged CI runner.
    for parent in [tmp_path.parent.parent, tmp_path.parent, tmp_path]:
        parent.chmod(0o755)
    try:
        os.chown(target, 1001, 1001)
    except PermissionError:
        pytest.skip('ownership reproduction requires CHOWN/SETUID/SETGID in isolated test container')
    enroll(node_key(9555), target)
    def as_installer():
        os.setgroups([])
        os.setgid(1001)
        os.setuid(1001)
    result = subprocess.run([sys.executable, '-c',
        'import pathlib,sys; p=pathlib.Path(sys.argv[1]); '
        'assert "AWG_NODE_MANAGEMENT_PORT=9555\\n" in (p/"node.env").read_text(); '
        'assert (p/"agent.crt").read_bytes()', str(target)],
        preexec_fn=as_installer, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for expression in ['(p/"agent.key").read_bytes()', 'list((p/".enrollment").iterdir())']:
        denied = subprocess.run([sys.executable, '-c',
            'import pathlib,sys; p=pathlib.Path(sys.argv[1]); ' + expression, str(target)],
            preexec_fn=as_installer, capture_output=True, text=True)
        assert denied.returncode != 0
        assert "PermissionError" in denied.stderr
    for path in [target / '.enrollment', (target / 'current').resolve()]:
        assert path.stat().st_uid == 0
        assert path.stat().st_mode & 0o777 == 0o711
    assert (target / 'agent.key').stat().st_uid == 0
    assert (target / 'agent.key').stat().st_mode & 0o777 == 0o600
