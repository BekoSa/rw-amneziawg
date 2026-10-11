"""The optional TT lab must provide real challenge validation without host access."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_trusttunnel_lab_has_separate_internal_networks_and_no_client_bypass():
    config = yaml.safe_load((ROOT / 'deploy/dev/compose.trusttunnel.yaml').read_text())
    assert all(n['driver'] == 'bridge' and n['internal'] for n in config['networks'].values())
    server = set(config['services']['tt-server']['networks'])
    client = set(config['services']['tt-client']['networks'])
    target = set(config['services']['tt-target']['networks'])
    assert server & client and server & target and not client & target
    for service in config['services'].values():
        assert not service.get('ports')
        assert not service.get('devices')
        assert not service.get('cap_add')
        assert not service.get('privileged')
        assert service.get('network_mode') != 'host'
        assert service.get('cap_drop') == ['ALL']


def test_pebble_validates_real_public_challenge_ports():
    import json
    config = json.loads((ROOT / 'deploy/dev/trusttunnel/pebble.json').read_text())['pebble']
    assert config['httpPort'] == 80 and config['tlsPort'] == 443
    compose = yaml.safe_load((ROOT / 'deploy/dev/compose.trusttunnel.yaml').read_text())
    assert compose['services']['pebble']['environment'].get('PEBBLE_VA_ALWAYS_VALID', '0') == '0'
    assert compose['services']['pebble']['environment']['PEBBLE_AUTHZREUSE'] == '0'


def test_tt_lab_cleanup_keeps_state_and_checks_host():
    script = (ROOT / 'scripts/trusttunnel-lab.sh').read_text()
    assert 'check_network_safety.py' in script and 'check_pool_overlap.py' in script
    assert 'host-before' in script and 'host-after' in script and 'compare_host.py' in script
    assert 'down --volumes' not in script and 'down -v' not in script
    assert 'artifacts/lab.lock' in script


def test_native_client_and_stock_smoke_never_select_tun_runtime():
    client = (ROOT / 'deploy/dev/trusttunnel/client.py').read_text()
    assert "[listener.socks]" in client and 'listener\\.tun' in client
    compose = yaml.safe_load((ROOT / 'deploy/dev/compose.trusttunnel.yaml').read_text())
    assert 'TT_ALLOW_PRIVATE' not in compose['services']['tt-server']['environment']
    stock = (ROOT / 'scripts/stock-http-smoke.sh').read_text()
    assert 'compose.live.yaml' not in stock
    assert 'check_network_safety.py' in stock and 'check_pool_overlap.py' in stock
    assert 'host-before' in stock and 'host-after' in stock and 'compare_host.py' in stock
    assert 'phase removed' in stock and 'phase restored' in stock
    assert 'down -v' not in stock and 'down --volumes' not in stock
