"""`python -m awg_agent.enroll <dir> < key`: unpack the node key issued by the extension TUI.

The key is read from stdin (never argv/env, which leak into `ps`, shell history and sudo logs).
Writes ca.crt, agent.crt, agent.key (0600) and node.env with the node/controller identities after
checking that the certificate is issued by the bundled CA for this node and matches the private key.
"""
import base64
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import serialization

PREFIX = 'awgnode1:'


def enroll(secret: str, directory: Path):
    secret = secret.strip()
    if not secret.startswith(PREFIX):
        raise ValueError('not an AWG node key (expected awgnode1:...)')
    payload = json.loads(base64.urlsafe_b64decode(secret[len(PREFIX):]))
    node, controller = str(UUID(payload['node_id'])), str(UUID(payload['controller_id']))
    ca = x509.load_pem_x509_certificate(payload['ca'].encode())
    cert = x509.load_pem_x509_certificate(payload['cert'].encode())
    key = serialization.load_pem_private_key(payload['key'].encode(), None)
    try:
        cert.verify_directly_issued_by(ca)
    except Exception:
        raise ValueError('node certificate is not issued by the bundled CA') from None
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise ValueError('node key does not match its certificate')
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    if names != [f'{node}.agents.awg.internal']:
        raise ValueError('certificate is issued for another node')
    directory.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    for name, field, mode in (('ca.crt', 'ca', 0o644), ('agent.crt', 'cert', 0o644), ('agent.key', 'key', 0o600)):
        path = directory / name
        path.write_text(payload[field])
        path.chmod(mode)
    port = payload.get('management_port', 8443)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid management port in node key')
    # AWG_NODE_MANAGEMENT_PORT is the published host port (the Agent itself always listens on 8443 inside).
    (directory / 'node.env').write_text(f'AWG_NODE_ID={node}\nAWG_CONTROLLER_ID={controller}\nAWG_NODE_MANAGEMENT_PORT={port}\n')
    (directory / 'node.env').chmod(0o644)  # identities only, read by the compose client
    return node


if __name__ == '__main__':
    try:
        print(enroll(sys.stdin.read(), Path(sys.argv[1])))
    except (KeyError, ValueError) as error:
        raise SystemExit(f'enrollment failed: {error}')
