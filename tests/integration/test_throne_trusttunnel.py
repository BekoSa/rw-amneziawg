"""Official Throne 1.3.2 imports enriched subscriptions and carries real payload."""
import base64
import os

import pytest

from awg_contracts.trusttunnel import TrustTunnelSubscriptionEntry
from subscription_renderers.trusttunnel import enrich
from tests.integration.test_trusttunnel import SERVER, apply, call, desired, wait_service

pytestmark = [pytest.mark.network, pytest.mark.integration,
    pytest.mark.skipif(os.environ.get('THRONE_LAB') != '1', reason='isolated official Throne lab required')]
THRONE = 'http://10.241.13.5:9000'


@pytest.mark.parametrize('encoding', ['uri', 'base64'])
def test_official_throne_imports_native_tt_in_subscription_and_proxies_tcp_udp(encoding):
    wait_service()
    call(SERVER, '/reset')
    assert not call(THRONE, '/direct')['reachable'], 'Throne bypasses TT endpoint target network'
    snapshot = desired()
    apply(snapshot)
    call(SERVER, '/tick')
    state = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert state['healthy'] and state['certificate_state'] == 'VALID'
    native = call(SERVER, '/export', {'id': snapshot['deployment_id'],
                                    'peer_id': snapshot['clients'][0]['peer_id']})
    peer = snapshot['clients'][0]
    entry = TrustTunnelSubscriptionEntry(profile_id=snapshot['profile']['profile_id'],
        node_id=snapshot['node_id'], peer_id=peer['peer_id'], name='isolated Throne TT',
        endpoint=snapshot['profile']['endpoint'], tls_server_name='vpn.tt.test',
        username=peer['username'], password=peer['password'], native_deeplink=native['deeplink'])
    # Keep a real stock protocol entry: import must accept the enriched whole body.
    stock = b'vless://00000000-0000-4000-8000-000000000002@192.0.2.1:443?security=tls#stock\n'
    if encoding == 'base64':
        stock = base64.b64encode(stock)
    subscription = enrich(stock, [entry], 'Throne/1.3.2')
    assert subscription != stock
    imported = call(THRONE, '/configure', {'subscription': subscription.decode()}, timeout=120)
    assert imported['imported'] and imported['started'] and imported['tls_verified'], imported
    assert imported['server'] == '10.241.10.2' and imported['port'] == 8444
    result = call(THRONE, '/probe')
    assert result == {'http': True, 'udp': True}, (result, call(THRONE, '/diagnostic'))
