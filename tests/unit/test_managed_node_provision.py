"""Immutable node provisioning must survive updates without replacing identity/secrets."""
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest

spec = importlib.util.spec_from_file_location('managed_provision', 'scripts/provision_managed_node.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NODE = UUID('00000000-0000-4000-8000-000000000031')


def test_fresh_and_repeat_keep_envelope_and_secret(tmp_path):
    first = module.provision(tmp_path, NODE, 2525, '172.29.64.0/24')
    assert first['NODE_BACKEND_IP'] == '172.29.64.2'
    envelope = json.loads((tmp_path / 'node-config/envelope.json').read_text())
    assert envelope['node_id'] == str(NODE)
    assert envelope['reserved_ports'] == [2525]
    assert (tmp_path / 'node-config/ingress.secret').stat().st_mode & 0o777 == 0o600
    before = {path.name: path.read_bytes() for path in (tmp_path / 'node-config').iterdir()}
    assert module.provision(tmp_path, NODE, 2525, '172.29.64.0/24') == first
    assert {path.name: path.read_bytes() for path in (tmp_path / 'node-config').iterdir()} == before


@pytest.mark.parametrize('kwargs', [dict(node_id=UUID(int=32)), dict(management_port=8443),
    dict(subnet='172.29.65.0/24'), dict(subnet='172.29.0.0/16'), dict(backend_ipv4='172.29.64.3')])
def test_update_rejects_changed_install_envelope(tmp_path, kwargs):
    module.provision(tmp_path, NODE, 2525, '172.29.64.0/24')
    before = {path.name: path.read_bytes() for path in (tmp_path / 'node-config').iterdir()}
    arguments = dict(node_id=NODE, management_port=2525, subnet='172.29.64.0/24')
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        module.provision(tmp_path, **arguments)
    assert {path.name: path.read_bytes() for path in (tmp_path / 'node-config').iterdir()} == before


@pytest.mark.parametrize('subnet,backend', [('127.0.0.0/24', None), ('8.8.8.0/24', None),
    ('172.29.64.0/24', '172.29.64.1'), ('172.29.64.0/24', '172.29.64.255'),
    ('172.29.64.0/24', '172.29.65.2')])
def test_invalid_backend_never_creates_configuration(tmp_path, subnet, backend):
    with pytest.raises(ValueError):
        module.provision(tmp_path, NODE, 2525, subnet, backend_ipv4=backend)
    assert not (tmp_path / 'node-config').exists()


def test_secret_symlink_is_rejected(tmp_path):
    module.provision(tmp_path, NODE, 2525, '172.29.64.0/24')
    secret = tmp_path / 'node-config/ingress.secret'
    secret.unlink()
    secret.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        module.provision(tmp_path, NODE, 2525, '172.29.64.0/24')


def test_private_bind_survives_repeat(tmp_path):
    module.provision(tmp_path, NODE, 2525, '172.29.64.0/24', bind_addresses=['192.168.3.4'])
    module.provision(tmp_path, NODE, 2525, '172.29.64.0/24')
    envelope = json.loads((tmp_path / 'node-config/envelope.json').read_text())
    assert envelope['bind_addresses'] == ['192.168.3.4']
    with pytest.raises(ValueError):
        module.provision(tmp_path, NODE, 2525, '172.29.64.0/24', bind_addresses=['0.0.0.0'])
