"""Container-only creation of immutable managed-node configuration; prints no secrets."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from uuid import UUID, uuid4

from awg_contracts import NodeInstallEnvelope


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('configuration must be a regular file')
            return source.read(65537)
    except OSError:
        raise ValueError('cannot read managed configuration safely') from None


def _write(path, payload, mode):
    fd, temporary = tempfile.mkstemp(prefix='.provision-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def provision(directory, node_id, management_port, subnet, *, backend_ipv4=None, bind_addresses=None):
    network = ipaddress.ip_network(subnet, strict=True)
    if network.version != 4 or network.num_addresses < 4:
        raise ValueError('managed backend needs an IPv4 bridge subnet')
    config = Path(directory) / 'node-config'
    envelope_path, secret_path = config / 'envelope.json', config / 'ingress.secret'
    network_path = config / 'bridge-subnet'
    if network_path.exists() and _read(network_path).decode().strip() != str(network):
        raise ValueError('installed bridge subnet changed')
    if config.is_symlink():
        raise ValueError('managed configuration directory must not be a symlink')
    existing = None
    if envelope_path.exists() or envelope_path.is_symlink():
        existing = NodeInstallEnvelope.model_validate_json(_read(envelope_path))
    address = ipaddress.ip_address(backend_ipv4 or (existing.backend_ipv4 if existing else str(network.network_address + 2)))
    if address not in network or address in {network.network_address, network.network_address + 1, network.broadcast_address}:
        raise ValueError('backend must be an available bridge host address')
    candidate = NodeInstallEnvelope(envelope_id=existing.envelope_id if existing else uuid4(),
        node_id=node_id, backend_ipv4=str(address), reserved_ports=[management_port],
        bind_addresses=bind_addresses or (existing.bind_addresses if existing else ['0.0.0.0', '::']))
    if existing is not None and candidate != existing:
        raise ValueError('installed envelope changed; explicit reprovisioning is required')
    # Do not rotate the ingress credential on image updates or partial install retry.
    if secret_path.exists() or secret_path.is_symlink():
        secret = _read(secret_path)
        if not re.fullmatch(rb'[a-f0-9]{64}\n?', secret) or secret_path.stat().st_mode & 0o077:
            raise ValueError('invalid or insecure ingress credential')
    elif existing is not None:
        raise ValueError('installed ingress credential is missing; restore the backup')
    else:
        secret = (secrets.token_hex(32) + '\n').encode()
    config.mkdir(parents=True, exist_ok=True)
    config.chmod(0o711)
    if not secret_path.exists():
        _write(secret_path, secret, 0o600)
    if not network_path.exists():
        _write(network_path, (str(network) + '\n').encode(), 0o644)
    if existing is None:
        _write(envelope_path, candidate.model_dump_json().encode() + b'\n', 0o644)
    return {'NODE_BACKEND_IP': str(address), 'NODE_BRIDGE_SUBNET': str(network)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('node_id', type=UUID)
    parser.add_argument('management_port', type=int)
    parser.add_argument('subnet')
    parser.add_argument('--backend-ip')
    parser.add_argument('--bind-address', action='append')
    args = parser.parse_args()
    try:
        values = provision(args.directory, args.node_id, args.management_port, args.subnet,
                           backend_ipv4=args.backend_ip, bind_addresses=args.bind_address)
    except (ValueError, OSError):
        raise SystemExit('Managed node provisioning failed: invalid or changed install configuration') from None
    for name, value in values.items():
        print(name + '=' + value)


if __name__ == '__main__':
    main()
