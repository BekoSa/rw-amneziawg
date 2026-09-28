"""Node enrollment bundle: a node certificate signed by the extension CA, packed like Remnawave's SECRET_KEY.

The bundle carries the node's private key, so it is shown once for the operator to paste into the node
installer and is never stored by the Controller.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PREFIX = 'awgnode1:'


def node_bundle(ca_dir: Path, controller_id: str, node_id: UUID, *, days: int = 825) -> str:
    ca_key = serialization.load_pem_private_key((ca_dir / 'ca.key').read_bytes(), None)
    ca_pem = (ca_dir / 'ca.crt').read_bytes()
    ca = x509.load_pem_x509_certificate(ca_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'awg-node-{node_id}')]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(f'{node_id}.agents.awg.internal')]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
    payload = {'node_id': str(node_id), 'controller_id': str(UUID(controller_id)), 'ca': ca_pem.decode(),
               'cert': cert.public_bytes(serialization.Encoding.PEM).decode(),
               'key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()}
    return PREFIX + base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()


def parse_bundle(text: str) -> dict:
    text = text.strip()
    if not text.startswith(PREFIX):
        raise ValueError('not an AWG node bundle')
    payload = json.loads(base64.urlsafe_b64decode(text[len(PREFIX):]))
    UUID(payload['node_id'])
    UUID(payload['controller_id'])
    for field in ('ca', 'cert', 'key'):
        if '-----BEGIN' not in payload[field]:
            raise ValueError('malformed bundle')
    return payload
