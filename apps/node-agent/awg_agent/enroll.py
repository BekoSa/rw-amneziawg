"""`python -m awg_agent.enroll <dir> < key`: unpack the node key issued by the extension TUI.

The key is read from stdin (never argv/env, which leak into `ps`, shell history and sudo logs).
Writes ca.crt, agent.crt, agent.key (0600) and node.env with the node/controller identities after
checking that the certificate is issued by the bundled CA for this node and matches the private key.
"""
import base64
import json
import os
import sys
import tempfile
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
    port = payload.get('management_port', 8443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid management port in node key')
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
    # AWG_NODE_MANAGEMENT_PORT is the published host port (the Agent itself always listens on 8443 inside).
    files = [('ca.crt', payload['ca'], 0o644), ('agent.crt', payload['cert'], 0o644),
             ('agent.key', payload['key'], 0o600),
             ('node.env', f'AWG_NODE_ID={node}\nAWG_CONTROLLER_ID={controller}\nAWG_NODE_MANAGEMENT_PORT={port}\n', 0o644)]
    generations = directory / '.enrollment'
    generations.mkdir(exist_ok=True, mode=0o700)
    generations.chmod(0o700)

    def sync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def generation(values):
        target = Path(tempfile.mkdtemp(prefix='generation-', dir=generations))
        for name, value, mode in values:
            fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, 'wb') as output:
                output.write(value.encode() if isinstance(value, str) else value)
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
        sync(target)
        sync(generations)
        return target

    def link(target, path):
        temporary = directory / ('.enroll-link-' + os.urandom(12).hex())
        try:
            temporary.symlink_to(target)
            os.replace(temporary, path)
            sync(directory)
        finally:
            temporary.unlink(missing_ok=True)

    bundle = generation(files)
    current = directory / 'current'
    if not current.exists():
        # Legacy paths are replaced only after current points at byte-identical copies.
        existing = [directory / name for name, _, _ in files]
        if any(path.exists() for path in existing):
            if not all(path.is_file() for path in existing):
                raise ValueError('incomplete existing enrollment')
            legacy = generation([(name, (directory / name).read_bytes(), mode) for name, _, mode in files])
            link(str(legacy.relative_to(directory)), current)
        else:
            link(str(bundle.relative_to(directory)), current)
    for name, _, _ in files:
        path = directory / name
        target = 'current/' + name
        if not path.is_symlink() or os.readlink(path) != target:
            link(target, path)
    # One durable pointer switch publishes the entire CA/cert/key/identity generation.
    link(str(bundle.relative_to(directory)), current)
    return node


if __name__ == '__main__':
    try:
        print(enroll(sys.stdin.read(), Path(sys.argv[1])))
    except (KeyError, ValueError) as error:
        raise SystemExit(f'enrollment failed: {error}')
