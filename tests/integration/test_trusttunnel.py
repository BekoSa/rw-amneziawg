"""Real ACME and TT proxy payload; no TUN. Run scripts/trusttunnel-lab.sh test."""
import os
import time
from uuid import uuid4
import httpx
import pytest

pytestmark = [pytest.mark.network, pytest.mark.integration,
    pytest.mark.skipif(os.environ.get('TT_LAB') != '1', reason='isolated TT lab required')]
SERVER = 'http://10.241.12.2:9000'
CLIENT = 'http://10.241.13.3:9000'
NODE = '00000000-0000-4000-8000-000000000001'
PASSWORD = 'isolated-only-' + 't' * 43


def call(base, path, body=None, timeout=180):
    response = httpx.post(base + path, json=body or {}, timeout=timeout, trust_env=False)
    assert response.status_code == 200, response.text
    return response.json()


def wait_service():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status = call(SERVER, '/health', timeout=3)
            if status.get('ca_ready') is True:
                return status
        except httpx.HTTPError:
            pass
        time.sleep(.25)
    raise AssertionError('TT lab server or Pebble CA did not become ready')


def desired(method='http-01', domain='vpn.tt.test'):
    return {'deployment_id': str(uuid4()), 'node_id': NODE, 'revision': 1,
        'profile': {'profile_id': str(uuid4()), 'name': 'isolated lab',
            'endpoint': {'host': '10.241.10.2', 'port': 8444}, 'node_ids': [NODE],
            'tls': {'domain': domain, 'email': 'lab@example.org',
                    'environment': 'staging', 'challenge': method}},
        'clients': [{'peer_id': str(uuid4()), 'user_uuid': str(uuid4()),
                     'username': 'lab-user', 'password': PASSWORD, 'credential_generation': 1}]}


def apply(snapshot, expected=None):
    return call(SERVER, '/apply', {'desired': snapshot, 'expected_applied_revision': expected})


def configure(snapshot, quic=False, password=None):
    root = call(SERVER, '/ca')['ca']
    call(CLIENT, '/configure', {'username': snapshot['clients'][0]['username'],
        'password': password or snapshot['clients'][0]['password'],
        'domain': snapshot['profile']['tls']['domain'], 'ca': root, 'quic': quic})


def assert_payload():
    result = call(CLIENT, '/probe')
    assert result == {'http': True, 'udp': True}, str((result, call(CLIENT, '/diagnostic')))


@pytest.mark.parametrize('method,domain', [('http-01', 'vpn.tt.test'), ('tls-alpn-01', 'alpn.tt.test'), ('auto', 'vpn.tt.test')])
def test_real_challenges_tcp_udp_rotation_renewal_and_recovery(method, domain):
    wait_service()
    call(SERVER, '/reset')
    assert not call(CLIENT, '/direct')['reachable'], 'client bypasses endpoint target network'
    snapshot = desired(method, domain)
    first = apply(snapshot)
    assert first['state'] == 'APPLYING'
    call(SERVER, '/tick')
    state = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert state['certificate_state'] == 'VALID', state
    assert state['state'] == 'DEGRADED' and state['healthy'], state
    assert state['fingerprint'] == state['served_fingerprint']
    assert state['certificate_environment'] == 'staging'
    configure(snapshot)
    assert_payload()
    native = call(SERVER, '/export', {'id': snapshot['deployment_id'],
        'peer_id': snapshot['clients'][0]['peer_id']})
    assert native['deeplink'].startswith('tt://?')
    assert '\n' not in native['deeplink'], 'export includes terminal QR decoration'
    imported = call(CLIENT, '/native', {'toml': native['toml']})
    assert imported['configured'], imported.get('diagnostic')
    assert_payload()  # Pinned official client consumes actual endpoint native export.
    configure(snapshot)
    root = call(SERVER, '/ca')['ca']
    tls_probe = {'domain': domain, 'ca': root, 'fragmented': True}
    assert call(CLIENT, '/tls', tls_probe | {'alpn': ['h2']}) == {'connected': True, 'alpn': 'h2'}
    assert not call(CLIENT, '/tls', tls_probe | {'alpn': ['acme-tls/1']})['connected']
    call(SERVER, '/solver-start', {'id': snapshot['deployment_id']})
    try:
        assert call(CLIENT, '/tls', tls_probe | {'alpn': ['acme-tls/1']}) == {
            'connected': True, 'alpn': 'acme-tls/1'}
        assert_payload()
    finally:
        call(SERVER, '/solver-stop')
    configure(snapshot, quic=True)
    assert_payload()  # Actual UDP QUIC bind and SOCKS UDP relay, not generated config.
    configure(snapshot, password='wrong-password-' + 'x' * 43)
    assert call(CLIENT, '/probe') == {'http': False, 'udp': False}
    configure(snapshot)
    assert_payload()
    for host, port in [('10.241.12.2', 9000), ('127.0.0.1', 10080), ('192.0.0.14', 22)]:
        assert not call(CLIENT, '/reach', {'host': host, 'port': port})['reachable']
    call(CLIENT, '/session/open', {'id': 'renew'})
    assert call(CLIENT, '/session/probe', {'id': 'renew'})['alive']
    renewed = call(SERVER, '/renew', {'id': snapshot['deployment_id']})
    assert renewed['certificate_generation'] > state['certificate_generation'], str(call(SERVER, '/diagnostic'))
    assert renewed['fingerprint'] != state['fingerprint']
    assert renewed['fingerprint'] == renewed['served_fingerprint']
    assert call(CLIENT, '/session/probe', {'id': 'renew'})['alive'], 'renewal interrupted session'
    assert_payload()
    call(SERVER, '/recover')
    recovered = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert recovered['fingerprint'] == renewed['fingerprint']
    assert recovered['certificate_generation'] == renewed['certificate_generation']
    assert_payload()
    call(CLIENT, '/session/open', {'id': 'revoke'})
    snapshot['revision'] = 2
    snapshot['clients'][0]['password'] = 'rotated-only-' + 'r' * 43
    snapshot['clients'][0]['credential_generation'] = 2
    apply(snapshot, 1)
    assert not call(CLIENT, '/session/probe', {'id': 'revoke'})['alive']
    assert call(CLIENT, '/probe') == {'http': False, 'udp': False}
    configure(snapshot)
    assert_payload()
    snapshot['revision'] = 3
    snapshot['clients'] = []
    apply(snapshot, 2)
    call(SERVER, '/recover')
    assert call(CLIENT, '/probe') == {'http': False, 'udp': False}, 'restart resurrected revoked credentials'


def test_real_failed_challenge_persists_backoff_across_restart():
    wait_service()
    call(SERVER, '/reset')
    # DNS resolves to tt-client, which has no TCP80 challenge server.
    snapshot = desired(domain='fail.tt.test')
    apply(snapshot)
    call(SERVER, '/tick')
    before = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert before['certificate_state'] == 'ERROR' and not before['healthy'], before
    assert before['next_attempt_at'] and before['certificate_error_code'] == 'ACME_FAILED'
    call(SERVER, '/recover')
    call(SERVER, '/tick')
    after = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert after['next_attempt_at'] == before['next_attempt_at'], 'restart created order before backoff'
    assert not after['healthy']


def test_auto_real_http_failure_falls_back_to_tls_and_remembers_method():
    wait_service()
    call(SERVER, '/reset', {'http_failure': True})
    snapshot = desired('auto')
    apply(snapshot)
    call(SERVER, '/tick')
    state = call(SERVER, '/state', {'id': snapshot['deployment_id']})
    assert state['certificate_state'] == 'VALID' and state['healthy'], str(call(SERVER, '/diagnostic'))
    assert state['fingerprint'] == state['served_fingerprint']
    assert call(SERVER, '/method', {'id': snapshot['deployment_id']}) == {'method': 'tls-alpn-01'}
    configure(snapshot)
    assert_payload()
    call(SERVER, '/recover')
    assert call(SERVER, '/method', {'id': snapshot['deployment_id']}) == {'method': 'tls-alpn-01'}
    renewed = call(SERVER, '/renew', {'id': snapshot['deployment_id']})
    assert renewed['fingerprint'] != state['fingerprint']
    assert renewed['fingerprint'] == renewed['served_fingerprint']
    assert_payload()
