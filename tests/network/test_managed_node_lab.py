"""Managed ingress is exercised in bridges, never on the developer network."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_managed_lab_isolates_backend_and_target_from_public_client():
    config = yaml.safe_load((ROOT / 'deploy/dev/compose.managed-node.yaml').read_text())
    services = config['services']
    assert set(services['managed-client']['networks']).isdisjoint(services['managed-agent']['networks'])
    assert set(services['managed-client']['networks']).isdisjoint(services['managed-target']['networks'])
    assert 'backend' in services['ingress']['networks']
    assert 'backend' in services['managed-agent']['networks']
    assert all(network['internal'] and network['driver'] == 'bridge' for network in config['networks'].values())
    for service in services.values():
        assert not service.get('ports') and not service.get('devices')
        assert not service.get('cap_add') and not service.get('privileged')
        assert service.get('network_mode') != 'host'
        assert service['cap_drop'] == ['ALL']


def test_managed_lab_checks_host_and_retains_state():
    script = (ROOT / 'scripts/managed-node-lab.sh').read_text()
    for boundary in ('check_network_safety.py', 'check_pool_overlap.py', 'host-before', 'host-after', 'compare_host.py', 'artifacts/lab.lock'):
        assert boundary in script
    assert 'down -v' not in script and 'down --volumes' not in script


def test_managed_awg_overlay_keeps_client_away_from_backend():
    # Docker Compose resolves !override; the overlay deliberately removes underlay
    # from the Agent, so the existing E2E endpoint can reach only the relay.
    content = (ROOT / 'deploy/dev/compose.managed-awg.yaml').read_text()
    assert 'networks: !override' in content
    assert 'managed-awg-backend: {ipv4_address: 10.240.7.3}' in content
    assert 'underlay: {ipv4_address: 10.240.4.2}' in content
    assert 'NODE_INSTALL_ENVELOPE:' in content
    assert 'network_mode: host' not in content
    script = (ROOT / 'scripts/managed-awg-lab.sh').read_text()
    assert 'scripts/live-preflight.sh' in script
    assert 'phase removed' in script and 'phase restored' in script
    assert 'compose stop gateway controller awg-agent managed-awg-ingress' in script
    assert 'compare_host.py' in script and 'check_pool_overlap.py' in script
