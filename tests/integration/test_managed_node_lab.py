"""Real production ingress to production TT runtime; only control is lab-specific."""
import os
import time
from uuid import uuid4
import httpx
import pytest
from awg_contracts import NodeInstallEnvelope, envelope_digest

pytestmark = [pytest.mark.network, pytest.mark.integration,
    pytest.mark.skipif(os.environ.get('MANAGED_NODE_LAB') != '1', reason='isolated managed-node lab required')]
SERVER = 'http://10.242.22.3:9000'
CLIENT = 'http://10.242.23.3:9000'
NODE = '00000000-0000-4000-8000-000000000001'


def call(base, path, body=None):
    response = httpx.post(base + path, json=body or {}, timeout=180, trust_env=False)
    assert response.status_code == 200, response.text
    return response.json()


def test_real_managed_dynamic_ingress_h2_quic_native_and_revocation():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if call(SERVER, '/health')['ready']:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.25)
    else:
        pytest.fail('actual ingress/CA/runtime did not become ready')
    envelope = NodeInstallEnvelope.model_validate(call(SERVER, '/envelope'))
    assert call(SERVER, '/auth-denial')['status'] == 401
    deployment, profile, peer, user = map(lambda _: str(uuid4()), range(4))
    snapshot = {'deployment_id': deployment, 'node_id': NODE, 'revision': 1,
        'profile': {'profile_id': profile, 'name': 'managed lab', 'node_ids': [NODE],
            'endpoint': {'host': '10.242.20.2', 'port': 2443},
            'tls': {'domain': 'vpn.tt.test', 'email': 'lab@example.org', 'environment': 'staging', 'challenge': 'http-01'}},
        'clients': [{'peer_id': peer, 'user_uuid': user, 'username': 'lab-user',
                     'password': 'isolated-only-' + 'm' * 43, 'credential_generation': 1}]}
    plan = {'node_id': NODE, 'revision': 1, 'envelope_digest': envelope_digest(envelope),
        'enabled_protocols': ['trusttunnel', 'amneziawg'], 'bindings': [
            {'binding_id': str(uuid4()), 'deployment_id': deployment, 'transport': transport,
             'public_address': '0.0.0.0', 'public_port': port, 'backend_role': role, 'backend_port': backend}
            for transport, port, role, backend in [('tcp',2443,'trusttunnel',8444),('udp',2443,'trusttunnel',9443),('tcp',80,'acme-http',8080)]]}
    plan['bindings'].append({'binding_id': str(uuid4()), 'deployment_id': str(uuid4()),
        'transport': 'udp', 'public_address': '0.0.0.0', 'public_port': 40000,
        'backend_role': 'awg', 'backend_port': 20000})
    call(SERVER, '/udp-echo')
    # Validation does not publish a listener or mutate accepted watermark.
    assert call(SERVER, '/node/validate', plan)['valid']
    before = call(SERVER, '/node/state')
    assert before['accepted_revision'] is None
    assert call(SERVER, '/node/apply', {'desired': plan})['applied_revision'] == 1
    assert call(CLIENT, '/udp-isolation', {'port': 40000})['isolated']
    assert call(SERVER, '/apply', {'desired': snapshot})['state'] == 'APPLYING'
    call(SERVER, '/tick')
    state = call(SERVER, '/state', {'id': deployment})
    assert state['healthy'] and state['certificate_state'] == 'VALID', state
    assert not call(CLIENT, '/direct')['reachable']
    root = call(SERVER, '/ca')['ca']
    assert call(CLIENT, '/tls', {'domain': 'vpn.tt.test', 'ca': root, 'alpn': ['h2'],
        'port': 2443, 'fragmented': True}) == {'connected': True, 'alpn': 'h2'}
    def configure(port, quic=False):
        call(CLIENT, '/configure', {'username': 'lab-user', 'password': snapshot['clients'][0]['password'],
            'domain': 'vpn.tt.test', 'ca': root, 'port': port, 'quic': quic})
    for quic in (False, True):
        configure(2443, quic)
        assert call(CLIENT, '/probe') == {'http': True, 'udp': True}
    native = call(SERVER, '/export', {'id': deployment, 'peer_id': peer})
    assert call(CLIENT, '/native', {'toml': native['toml']})['configured']
    assert call(CLIENT, '/probe') == {'http': True, 'udp': True}
    # Management backend cannot be expressed even through authenticated Agent API.
    bad = dict(plan, revision=2, bindings=[dict(plan['bindings'][0], backend_port=8443)])
    response = httpx.post(SERVER + '/node/validate', json=bad, timeout=10, trust_env=False)
    assert response.status_code == 500 and response.json()['error'] == 'ValidationError'
    # Arbitrary new public port needs no compose edit or container recreation.
    plan['revision'] = 2
    for binding in plan['bindings']:
        if binding['backend_role'] == 'trusttunnel':
            binding['public_port'] = 35443
    assert call(SERVER, '/node/apply', {'desired': plan, 'expected_applied_revision': 1})['applied_revision'] == 2
    snapshot['revision'] = 2
    snapshot['profile']['endpoint']['port'] = 35443
    call(SERVER, '/apply', {'desired': snapshot, 'expected_applied_revision': 1})
    configure(35443, True)
    assert call(CLIENT, '/probe') == {'http': True, 'udp': True}
    configure(2443)
    assert call(CLIENT, '/probe') == {'http': False, 'udp': False}
    configure(35443)
    call(CLIENT, '/session/open', {'id': 'revoke'})
    snapshot['revision'] = 3
    snapshot['clients'] = []
    call(SERVER, '/apply', {'desired': snapshot, 'expected_applied_revision': 2})
    assert not call(CLIENT, '/session/probe', {'id': 'revoke'})['alive']
    call(SERVER, '/recover')
    assert call(CLIENT, '/probe') == {'http': False, 'udp': False}
    # A second issuance uses real Pebble TLS-ALPN-01 through ingress, not HTTP.
    plan['revision'] = 3
    for binding in plan['bindings']:
        if binding['backend_role'] == 'acme-http':
            binding.update(public_port=443, backend_role='acme-tls-alpn', backend_port=8444)
    call(SERVER, '/node/apply', {'desired': plan, 'expected_applied_revision': 2})
    snapshot['revision'] = 4
    snapshot['profile']['tls'].update(domain='alpn.tt.test', challenge='tls-alpn-01')
    snapshot['clients'] = [{'peer_id': peer, 'user_uuid': user, 'username': 'lab-user',
        'password': 'isolated-only-' + 'm' * 43, 'credential_generation': 2}]
    # A zero-client revoke may intentionally have no healthy applied runtime.
    # CAS uses observed actual, never an assumed accepted revision.
    previous = call(SERVER, '/state', {'id': deployment})['applied_revision']
    call(SERVER, '/apply', {'desired': snapshot, 'expected_applied_revision': previous})
    call(SERVER, '/tick')
    state = call(SERVER, '/state', {'id': deployment})
    assert state['healthy'] and state['certificate_state'] == 'VALID', state
    assert call(CLIENT, '/tls', {'domain': 'alpn.tt.test', 'ca': root,
        'alpn': ['h2'], 'port': 443, 'fragmented': True}) == {'connected': True, 'alpn': 'h2'}
    # Transition an existing ACME socket to the TT data role without rebinding it.
    plan['revision'] = 4
    plan['bindings'] = [binding for binding in plan['bindings']
        if binding['backend_role'] != 'acme-tls-alpn']
    for binding in plan['bindings']:
        if binding['backend_role'] == 'trusttunnel':
            binding['public_port'] = 443
    call(SERVER, '/node/apply', {'desired': plan, 'expected_applied_revision': 3})
    snapshot['revision'] = 5
    snapshot['profile']['endpoint']['port'] = 443
    call(SERVER, '/apply', {'desired': snapshot, 'expected_applied_revision': 4})
    for quic in (False, True):
        call(CLIENT, '/configure', {'username': 'lab-user',
            'password': snapshot['clients'][0]['password'], 'domain': 'alpn.tt.test',
            'ca': root, 'port': 443, 'quic': quic})
        assert call(CLIENT, '/probe') == {'http': True, 'udp': True}
