"""Installer helpers, run inside the extension image: `python -m awg_tui.setup <command>`.

init-pki           create the extension CA and the Controller client certificate (idempotent)
check-remnawave    verify the API token against stock Remnawave and list Internal Squads
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _write(path: Path, data: bytes, mode: int, owner: int):
    # Created with its final mode (a key is never briefly world-readable); replacing instead of
    # rewriting also works when a previous run already handed the file to the Controller user.
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchown(fd, owner, owner)
        os.write(fd, data)
    finally:
        os.close(fd)


def init_pki(ca_dir: Path, controller_dir: Path, controller_id: str, controller_uid: int = 10001):
    UUID(controller_id)
    now = datetime.now(timezone.utc)
    if (ca_dir / 'ca.key').exists():
        ca_key = serialization.load_pem_private_key((ca_dir / 'ca.key').read_bytes(), None)
        ca = x509.load_pem_x509_certificate((ca_dir / 'ca.crt').read_bytes())
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'AWG extension management CA')])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
              .not_valid_after(now + timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
              .sign(ca_key, hashes.SHA256()))
        _write(ca_dir / 'ca.key', _pem_key(ca_key), 0o600, 0)
        _write(ca_dir / 'ca.crt', ca.public_bytes(serialization.Encoding.PEM), 0o644, 0)
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    _write(controller_dir / 'ca.crt', ca_pem, 0o644, controller_uid)
    if not (controller_dir / 'controller.key').exists():
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'awg-controller')]))
                .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1825))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(f'spiffe://awg/controller/{controller_id}')]), False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
                .sign(ca_key, hashes.SHA256()))
        _write(controller_dir / 'controller.key', _pem_key(key), 0o600, controller_uid)
        _write(controller_dir / 'controller.crt', cert.public_bytes(serialization.Encoding.PEM), 0o644, controller_uid)
    print('PKI ready: extension CA and Controller certificate')


async def check_remnawave(url: str, token: str) -> int:
    from remnawave_client import RemnawaveClient, UpstreamError
    client = RemnawaveClient(url, token)
    try:
        squads = await client.squads()
        await client._get('users', params={'start': 0, 'size': 1})
    except UpstreamError:
        print('Remnawave API rejected the token or is unreachable', file=sys.stderr)
        return 1
    finally:
        await client.close()
    for squad in squads:
        print(f'{squad["uuid"]}\t{squad["members"]}\t{squad["name"]}')
    return 0


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command == 'init-pki':
        init_pki(Path(os.environ.get('AWG_PKI_CA_DIR', '/pki-ca')), Path(os.environ.get('AWG_PKI_CONTROLLER_DIR', '/pki-controller')),
                 os.environ['AWG_CONTROLLER_ID'])
    elif command == 'check-remnawave':
        raise SystemExit(asyncio.run(check_remnawave(os.environ['REMNAWAVE_API_URL'], os.environ['REMNAWAVE_API_TOKEN'])))
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
