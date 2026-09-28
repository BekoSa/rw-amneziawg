"""Real userspace AWG through stock Remnawave, Controller, Agent and Gateway (Docker lab only).

Every assertion about connectivity is made on actual payload inside the isolated client
namespace: HTTP (TCP), UDP echo and DNS reach the target only through the tunnel.
"""
import base64
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from remnawave_client import user_uuid_for

pytestmark = pytest.mark.network

NODE_ID = '00000000-0000-4000-8000-000000000001'
PROFILE_ID = '00000000-0000-4000-8000-0000000000a1'
PROFILE_NAME = 'Lab | AWG'
SQUAD_NAME = 'AWG E2E'
MIHOMO = {'user-agent': 'mihomo/1.19.30'}
# AWG 3.1: header protection (S1-S4 >= 12), content padding and random trailers on a real tunnel.
PROTOCOL = {'adapter_id': 'amneziawg-go-v3', 'version': '3.1', 'parameters': {
    'Jc': 4, 'Jmin': 40, 'Jmax': 70, 'S1': 20, 'S2': 30, 'S3': 14, 'S4': 12,
    'H1': '1000-1100', 'H2': '2000-2100', 'H3': '3000-3100', 'H4': '4000-4100',
    'HeaderProtectionKey': 'bGFiLW9ubHktaGVhZGVyLXByb3RlY3Rpb24ta2V5ISE=',
    'ContentPaddingAddition': '8-32', 'RandomTrailers': 'true'}}
AWG_KEYS = ('Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4', 'I1', 'I2', 'I3', 'I4', 'I5',
            'HeaderProtectionKey', 'ContentPaddingAddition', 'RekeyAfterTime', 'RekeyTimeout', 'RejectAfterTime',
            'KeepaliveTimeout', 'MaxHandshakeAttempts', 'RandomTrailers', 'DisableCookies')


def wait_until(check, timeout=60, interval=1.0, message='condition'):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f'timed out waiting for {message}; last={last!r}')


def stock():
    token = Path('/lab-credentials/stock-token').read_text().strip()
    return httpx.Client(base_url=os.environ['AWG_E2E_STOCK_URL'], headers={'Authorization': 'Bearer ' + token},
                        timeout=20, trust_env=False)


def controller():
    return httpx.Client(base_url=os.environ['AWG_E2E_CONTROLLER_URL'], timeout=30, trust_env=False,
                        headers={'Authorization': 'Bearer ' + os.environ['AWG_E2E_ADMIN_TOKEN']})


def client(path, body=None, timeout=60):
    response = httpx.post(os.environ['AWG_E2E_CLIENT_URL'] + path, json=body or {}, timeout=timeout, trust_env=False)
    assert response.status_code == 200, response.text
    return response.json()


def probe(wait=0):
    return client('/probe', {'wait': wait}, timeout=wait + 30)


def tunnel_works(result):
    return result['http'] and result['udp'] and result['dns'] and result['handshake'] > 0


def tunnel_blocked(result):
    return not (result['http'] or result['udp'] or result['dns'])


def user_by_name(api, username):
    response = api.get(f'/api/users/by-username/{username}')
    return response.json()['response'] if response.status_code == 200 else None


def ensure_squad(api):
    for squad in api.get('/api/internal-squads').raise_for_status().json()['response']['internalSquads']:
        if squad['name'] == SQUAD_NAME:
            return squad['uuid']
    return api.post('/api/internal-squads', json={'name': SQUAD_NAME, 'inbounds': []}).raise_for_status().json()['response']['uuid']


def create_user(api, username, squads):
    existing = user_by_name(api, username)
    if existing:
        api.delete(f'/api/users/{existing["id"]}').raise_for_status()
    expire = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return api.post('/api/users', json={'username': username, 'expireAt': expire,
                                        'activeInternalSquads': squads}).raise_for_status().json()['response']


def subscription(short_uuid, headers=MIHOMO, base=None, path=''):
    return httpx.get((base or os.environ['AWG_E2E_GATEWAY_URL']) + f'/api/sub/{short_uuid}{path}',
                     headers=headers, timeout=20, trust_env=False)


def awg_proxy(response):
    assert response.status_code == 200
    document = yaml.safe_load(response.text)
    proxies = [p for p in (document or {}).get('proxies') or [] if isinstance(p, dict) and p.get('type') == 'wireguard']
    return proxies[0] if proxies else None


def client_config(proxy, routes):
    peer = proxy['peers'][0]
    # Mihomo option names: lower-case AWG 2 keys, kebab-case AWG 3.1 keys.
    options = {re.sub(r'(?<!^)(?=[A-Z])', '-', key).lower(): key for key in AWG_KEYS}
    return {'private_key': proxy['private-key'], 'address': proxy['ip'], 'server_public_key': peer['public-key'],
            'endpoint': f'{peer["server"]}:{peer["port"]}', 'allowed_ips': peer['allowed-ips'], 'mtu': proxy['mtu'],
            'parameters': {options[k]: (str(v).lower() if isinstance(v, bool) else v)
                           for k, v in proxy['amnezia-wg-option'].items() if k in options},
            'routes': routes}


def deployment_ready(api):
    for node in api.get('/api/v1/nodes').raise_for_status().json()['items']:
        if node['registration']['node_id'] == NODE_ID and node['online']:
            return [d for d in node['deployments'] if d['state'] == 'READY' and d['healthy']]
    return None


def peer_rows(api, user):
    # Match the current upstream user exactly; earlier runs may leave rows of deleted namesakes.
    identity = str(user_uuid_for(user['id']))
    return [p for p in api.get('/api/v1/peers').raise_for_status().json()['items'] if p['user_id'] == identity]


# Addresses services receive on the pinned extension subnet (small lab; no Docker socket needed).
EXTENSION_ADDRESSES = [f'10.240.3.{host}' for host in range(1, 12)]


def connect_client(alice, routes=('0.0.0.0/0',)):
    proxy = wait_until(lambda: awg_proxy(subscription(alice['shortUuid'])), 60, message='AWG entry in subscription')
    client('/up', client_config(proxy, list(routes)))
    return proxy


def test_connect():
    with stock() as api, controller() as admin:
        squad = ensure_squad(api)
        alice = create_user(api, 'e2e_alice', [squad])
        bob = create_user(api, 'e2e_bob', [])
        admin.post('/api/v1/nodes', json={'node_id': NODE_ID, 'name': 'lab-node',
                                          'management_url': 'https://awg-agent:8443'}).raise_for_status()
        profile = {'profile_id': PROFILE_ID, 'name': PROFILE_NAME, 'endpoint': {'host': '10.240.4.2', 'port': 51820},
                   'network': {'ipv4_pool': '10.241.0.0/24', 'server_ipv4': '10.241.0.1', 'mtu': 1380},
                   'protocol': PROTOCOL, 'access': {'squad_ids': [squad]}, 'node_ids': [NODE_ID]}
        admin.put(f'/api/v1/profiles/{PROFILE_ID}', json=profile).raise_for_status()
        validation = admin.post(f'/api/v1/profiles/{PROFILE_ID}/validate').raise_for_status().json()
        assert validation['valid'], validation
        admin.post(f'/api/v1/profiles/{PROFILE_ID}/apply',
                   json={'expected_digest': validation['draft_digest']}).raise_for_status()
        wait_until(lambda: deployment_ready(admin), 90, message='deployment READY')
        wait_until(lambda: any(p['present'] for p in peer_rows(admin, alice)), 60, message='alice peer')
        assert not peer_rows(admin, bob), 'bob is not entitled by squad and must not get a peer'

        # Before the tunnel exists the client has no path to the target at all (no bypass).
        client('/down')
        assert tunnel_blocked(probe())

        proxy = connect_client(alice)
        assert proxy['name'] == PROFILE_NAME
        result = probe(wait=30)
        assert tunnel_works(result), result
        assert any(r.get('dst') == 'default' and r.get('dev') == 'awg0' for r in result['routes']), result['routes']

        # Egress isolation: through the full tunnel the client reaches the target but no service network
        # (Controller API, AWG PostgreSQL, Agent mTLS on the extension net) and not the host bridge address.
        services = [[ip, port] for ip in EXTENSION_ADDRESSES for port in (8080, 5432, 8443)]
        assert not any(client('/reach', {'targets': services + [['10.240.5.1', 22], ['10.240.5.1', 18080]]})), services
        assert client('/reach', {'targets': [['10.240.5.3', 8080]]}) == [True]

        assert proxy['amnezia-wg-option']['version'] == 3, 'Mihomo runs its AWG 3 device for a 3.1 profile'

        # Bob and clients without AWG support (Happ) receive the untouched stock subscription.
        assert awg_proxy(subscription(bob['shortUuid'])) is None
        direct = subscription(alice['shortUuid'], {'user-agent': 'Happ/3.1.0'}, os.environ['AWG_E2E_STOCK_URL'])
        via_gateway = subscription(alice['shortUuid'], {'user-agent': 'Happ/3.1.0'})
        assert via_gateway.status_code == direct.status_code
        assert via_gateway.content == direct.content

        # Throne gets its wg:// link with AWG 3.1 fields; the subscription page gets the AmneziaVPN key.
        throne = base64.b64decode(subscription(alice['shortUuid'], {'user-agent': 'Throne/1.3.1'}).content).decode()
        assert any(line.startswith('wg://') and 'header_protection_key=' in line for line in throne.splitlines())
        page = subscription(alice['shortUuid'], {'user-agent': 'Remnawave Subscription Page'}, path='/info')
        assert any(link.startswith('vpn://') for link in page.json()['response']['links'])
        conf = subscription(alice['shortUuid'], {'user-agent': 'AmneziaWG/2.0'}, path='/awg')
        assert conf.status_code == 200 and 'HeaderProtectionKey = ' in conf.text

        wait_until(lambda: any(int(p['rx_total']) > 0 and int(p['tx_total']) > 0 for p in peer_rows(admin, alice)),
                   60, message='traffic counters collected')


def test_lifecycle():
    with stock() as api, controller() as admin:
        alice = user_by_name(api, 'e2e_alice')
        address = peer_rows(admin, alice)[0]['ipv4']
        # Split tunnel: only the target network uses AWG; default route stays untouched.
        connect_client(alice, ['10.240.5.0/24'])
        result = probe(wait=30)
        assert tunnel_works(result), result
        assert not any(r.get('dst') == 'default' for r in result['routes'])

        api.post(f'/api/users/{alice["id"]}/actions/disable').raise_for_status()
        wait_until(lambda: tunnel_blocked(probe()), 60, message='disabled user loses AWG')
        assert awg_proxy(subscription(alice['shortUuid'])) is None

        api.post(f'/api/users/{alice["id"]}/actions/enable').raise_for_status()
        wait_until(lambda: tunnel_works(probe()), 60, message='enabled user regains AWG with same config')
        assert peer_rows(admin, alice)[0]['ipv4'] == address
        old = connect_client(alice)
        assert tunnel_works(probe(wait=30))

        # Key rotation: the old key stops working on the node, the refreshed subscription works.
        admin.post(f'/api/v1/users/{user_uuid_for(alice["id"])}/rotate-key').raise_for_status()
        wait_until(lambda: tunnel_blocked(probe()), 60, message='old client key revoked on node')
        fresh = wait_until(lambda: (p := awg_proxy(subscription(alice['shortUuid']))) and
                           p['private-key'] != old['private-key'] and p, 60, message='rotated key in subscription')
        assert fresh['ip'] == old['ip'], 'rotation keeps the address'
        client('/up', client_config(fresh, ['0.0.0.0/0']))
        assert tunnel_works(probe(wait=30))


def test_controller_down():
    # lab.sh stopped only the Controller: existing tunnels keep working, gateway fails open.
    with stock() as api:
        alice = user_by_name(api, 'e2e_alice')
        assert tunnel_works(probe(wait=20))
        response = subscription(alice['shortUuid'])
        direct = subscription(alice['shortUuid'], MIHOMO, os.environ['AWG_E2E_STOCK_URL'])
        assert response.status_code == direct.status_code == 200
        assert response.content == direct.content


def test_agent_restart():
    # Client keeps the old server public key: success proves server identity survived restart.
    assert tunnel_works(probe(wait=60))


def test_controller_restart():
    with controller() as admin:
        wait_until(lambda: deployment_ready(admin), 60, message='deployment READY after restart')
        before = len(admin.get('/api/v1/revisions').json()['items'])
        admin.post('/api/v1/reconcile')
        time.sleep(12)
        assert len(admin.get('/api/v1/revisions').json()['items']) == before, 'reconcile must be idempotent'
        assert tunnel_works(probe(wait=20))


def test_removed():
    with stock() as api:
        alice = user_by_name(api, 'e2e_alice')
        direct = subscription(alice['shortUuid'], MIHOMO, os.environ['AWG_E2E_STOCK_URL'])
        assert direct.status_code == 200 and awg_proxy(direct) is None
        with pytest.raises(httpx.HTTPError):
            subscription(alice['shortUuid'])


def test_restored():
    with stock() as api, controller() as admin:
        alice = user_by_name(api, 'e2e_alice')
        wait_until(lambda: deployment_ready(admin), 90, message='deployment READY after re-enable')
        # Same client config from before removal: AWG DB and Agent key survived.
        assert tunnel_works(probe(wait=60))
        assert awg_proxy(subscription(alice['shortUuid'])) is not None


def test_delete():
    with stock() as api, controller() as admin:
        alice = user_by_name(api, 'e2e_alice')
        api.delete(f'/api/users/{alice["id"]}').raise_for_status()
        wait_until(lambda: tunnel_blocked(probe()), 60, message='deleted user removed from node')
        wait_until(lambda: not peer_rows(admin, alice), 30, message='deleted user hidden once the node confirmed removal')
