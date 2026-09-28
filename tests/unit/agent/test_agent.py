import base64
import re
from uuid import uuid4

import pytest
from awg_contracts import ApplyRequest, DesiredDeployment


def desired(revision=1, peers=True):
    return DesiredDeployment.model_validate({
        'deployment_id': '00000000-0000-0000-0000-000000000001',
        'node_id': '00000000-0000-0000-0000-000000000002', 'revision': revision,
        'profile': {'profile_id': '00000000-0000-0000-0000-000000000003', 'name': 'test',
                    'endpoint': {'host': 'test.invalid', 'port': 51820},
                    'network': {'ipv4_pool': '10.81.0.0/24', 'server_ipv4': '10.81.0.1'},
                    'protocol': {'adapter_id': 'amneziawg-go-v3', 'version': '2',
                                 'parameters': {'Jc': 4, 'Jmin': 40, 'Jmax': 70, 'S1': 12, 'S2': 13, 'S3': 14, 'S4': 15,
                                                'H1': '100-110', 'H2': '200-210', 'H3': '300-310', 'H4': '400-410'}}},
        'peers': [{'peer_id': '00000000-0000-0000-0000-000000000004', 'user_uuid': str(uuid4()),
                   'public_key': base64.b64encode(b'p' * 32).decode(), 'ipv4_address': '10.81.0.2'}] if peers else []})


class Runtime:
    """Stateful failure-injecting test driver; never a production runtime choice."""
    def __init__(self):
        self.current = {}
        self.fail = False
        self.calls = 0
        self.epoch = uuid4()

    def apply(self, deployment, private_key):
        self.calls += 1
        self.current[str(deployment.deployment_id)] = deployment
        if self.fail:
            self.fail = False
            raise RuntimeError('injected partial apply')

    def healthy(self, deployment, public_key):
        return self.current.get(str(deployment.deployment_id)) == deployment

    def stop(self, deployment_id):
        self.current.pop(str(deployment_id), None)

    def counters(self, deployment):
        return self.epoch, {p.public_key: (100, 200, 0) for p in deployment.peers}, {p.public_key: self.epoch for p in deployment.peers}


def service(tmp_path):
    from awg_agent.service import AgentService
    return AgentService(tmp_path, desired().node_id, Runtime())


def test_apply_retry_restart_key_and_digest(tmp_path):
    agent = service(tmp_path)
    first = desired()
    actual = agent.apply(ApplyRequest(desired=first))
    assert actual.state == 'READY'
    assert agent.apply(ApplyRequest(desired=first)).server_public_key == actual.server_public_key
    assert agent.runtime.calls == 1
    changed = first.model_copy(deep=True)
    changed.profile.name = 'changed same revision'
    with pytest.raises(ValueError, match='revision'):
        agent.apply(ApplyRequest(desired=changed))
    agent.close()
    recovered = service(tmp_path)
    recovered.recover()
    assert recovered.state(first.deployment_id).server_public_key == actual.server_public_key
    assert recovered.state(first.deployment_id).state == 'READY'
    recovered.close()


def test_failed_revision_rollback_does_not_resurrect_revoked_peer(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(ApplyRequest(desired=first))
    agent.runtime.fail = True
    revoke = first.model_copy(deep=True)
    revoke.revision = 2
    revoke.peers = []
    failed = agent.apply(ApplyRequest(desired=revoke, expected_applied_revision=1))
    assert failed.state == 'DEGRADED'
    assert failed.applied_revision is None
    assert not agent.runtime.current[str(first.deployment_id)].peers
    with pytest.raises(ValueError, match='revision'):
        agent.apply(ApplyRequest(desired=first))
    agent.close()
    restarted = service(tmp_path)
    restarted.recover()
    assert not restarted.runtime.current[str(first.deployment_id)].peers
    restarted.close()


def test_wrong_node_and_cas_rejected(tmp_path):
    agent = service(tmp_path)
    first = desired()
    wrong = first.model_copy(update={'node_id': uuid4()})
    with pytest.raises(ValueError, match='node'):
        agent.apply(ApplyRequest(desired=wrong))
    with pytest.raises(ValueError, match='revision'):
        agent.apply(ApplyRequest(desired=first, expected_applied_revision=2))
    assert not agent.runtime.current
    agent.close()


def test_unsupported_adapter_and_version_fail_closed(tmp_path):
    agent = service(tmp_path)
    first = desired()
    first.profile.protocol.version = '3.2'
    result = agent.validate(first)
    assert not result.valid
    assert not agent.runtime.current
    agent.close()


def test_compile_strict_fields_and_uapi(tmp_path):
    from awg_config import compile_uapi
    first = desired()
    config = compile_uapi(first, b's' * 32, set())
    assert 'private_key=' + (b's' * 32).hex() in config
    assert 'h1=100-110' in config
    assert 'allowed_ip=10.81.0.2/32' in config
    first.profile.protocol.parameters['PostUp'] = 'bad'
    with pytest.raises(ValueError, match='parameter'):
        compile_uapi(first, b's' * 32, set())


def test_traffic_sequence_survives_agent_restart(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(ApplyRequest(desired=first))
    sample = agent.traffic(first.deployment_id)
    assert sample.peers[0].rx_bytes == '100'
    assert agent.traffic(first.deployment_id).sequence > sample.sequence
    agent.close()
    again = service(tmp_path)
    again.recover()
    assert again.traffic(first.deployment_id).sequence > sample.sequence
    again.close()


def test_controller_certificate_identity_is_exact():
    from awg_agent.__main__ import authorized_controller
    expected = 'spiffe://awg/controller/00000000-0000-0000-0000-000000000099'
    assert authorized_controller({'subjectAltName': [('URI', expected)]}, expected)
    assert not authorized_controller({'subjectAltName': [('DNS', expected)]}, expected)
    assert not authorized_controller({'subjectAltName': [('URI', expected + '/other')]}, expected)
    assert not authorized_controller({}, expected)


def test_no_host_runtime_fallback(monkeypatch):
    from awg_agent.runtime import UserspaceRuntime
    monkeypatch.delenv('AWG_ISOLATED_RUNTIME', raising=False)
    with pytest.raises(RuntimeError, match='isolated Docker'):
        UserspaceRuntime()


def test_state_does_not_report_ready_after_drift(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(ApplyRequest(desired=first))
    agent.runtime.current.clear()
    assert agent.state(first.deployment_id).state == 'DEGRADED'
    agent.close()


def test_single_owner_state_directory(tmp_path):
    agent = service(tmp_path)
    with pytest.raises(RuntimeError, match='another agent'):
        service(tmp_path)
    agent.close()


def test_legacy_awg1_rejects_awg2_fields():
    from awg_config import compile_uapi
    first = desired()
    first.profile.protocol.version = '1.0'
    with pytest.raises(ValueError, match='parameter'):
        compile_uapi(first, b's' * 32, set())
    for key in ('S3', 'S4'):
        first.profile.protocol.parameters.pop(key)
    for key in ('H1', 'H2', 'H3', 'H4'):
        first.profile.protocol.parameters[key] = int(first.profile.protocol.parameters[key].split('-')[0])
    assert 'h1=100' in compile_uapi(first, b's' * 32, set())


def test_tls_rejection_initializes_protocol_lifecycle(monkeypatch):
    from awg_agent.__main__ import ControllerTLSProtocol
    from uvicorn.protocols.http.h11_impl import H11Protocol
    events = []
    monkeypatch.setenv('AWG_CONTROLLER_ID', '00000000-0000-0000-0000-000000000099')
    monkeypatch.setattr(H11Protocol, 'connection_made', lambda self, transport: events.append('initialized'))
    class Transport:
        def get_extra_info(self, name): return None
        def close(self): events.append('closed')
    protocol = object.__new__(ControllerTLSProtocol)
    protocol.connection_made(Transport())
    assert events == ['initialized', 'closed']


def test_overlapping_node_networks_and_ports_are_rejected(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(ApplyRequest(desired=first))
    second = desired()
    second.deployment_id = uuid4()
    assert not agent.validate(second).valid
    agent.close()


def test_egress_rules_cover_only_owned_pool():
    from awg_agent.runtime import egress_rules
    config = desired()
    rules = egress_rules(config)
    assert 'ip saddr 10.81.0.0/24' in rules
    assert 'masquerade' in rules
    assert 'table inet awg000000000000' in rules
    assert 'flush ruleset' not in rules
    assert 'hook postrouting' in rules


def test_idempotent_retry_repairs_runtime_drift(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(ApplyRequest(desired=first))
    agent.runtime.current.clear()
    assert agent.apply(ApplyRequest(desired=first)).state == 'READY'
    assert agent.runtime.calls == 2
    agent.close()


def test_egress_rules_deny_service_networks_and_inbound():
    import ipaddress
    from awg_agent.runtime import egress_rules
    rules = egress_rules(desired(), [ipaddress.ip_network('10.240.3.0/24')])
    lines = [line.strip() for line in rules.splitlines()]
    name = 'awg000000000000'
    deny = lines.index(f'iifname "{name}" ip daddr 10.240.3.0/24 drop')
    assert deny < lines.index(f'iifname "{name}" ip saddr 10.81.0.0/24 accept'), 'deny precedes accept'
    assert lines.index(f'iifname "{name}" oifname "{name}" drop') < deny, 'no client-to-client'
    assert f'oifname "{name}" drop' in lines and f'iifname "{name}" drop' in lines
    assert 'type filter hook input priority filter; policy accept;' in lines, 'agent-local services closed to tunnel'
    assert lines.count(f'iifname "{name}" drop') == 2


def test_generated_parameters_are_valid_and_random():
    from awg_config import V3_FIELDS, compile_uapi, random_parameters
    seen, signatures = set(), set()
    for index in range(300):
        version = ('1.0', '2', '3.1')[index % 3]
        values = random_parameters(version)
        config = desired()
        config.profile.protocol.version = version
        config.profile.protocol.parameters = values
        uapi = compile_uapi(config, b's' * 32, set())  # raises on invalid/overlapping values
        assert values['S1'] + 56 != values['S2'] and 4 <= values['Jc'] <= 8
        seen.add(values['H1'])
        if version == '1.0':
            assert not {'S3', 'S4', 'I1'} & set(values)
            continue
        # I1 mimics a DNS A response: random ID, answer flags, one question and one 4-byte answer.
        packet = bytes.fromhex(re.fullmatch(r'<r 2><b 0x([0-9a-f]+)>', values['I1'])[1])
        assert packet[:10] == bytes.fromhex('81800001000100000000') and packet[-6:-4] == b'\x00\x04'
        signatures.add(values['I1'])
        if version == '3.1':
            assert set(values) >= V3_FIELDS, 'every AWG 3.1 field is configured by default'
            assert 'random_trailers=true' in uapi and 'disable_cookies=true' in uapi
            low, high = map(int, values['ContentPaddingAddition'].split('-'))
            assert 8 <= low < high <= 134
    assert len(seen) > 250 and len(signatures) > 150


def test_awg31_parameters_compile_and_enforce_header_protection_rules():
    from awg_config import compile_uapi
    key = base64.b64encode(b'k' * 32).decode()
    config = desired()
    config.profile.protocol.version = '3.1'
    config.profile.protocol.parameters.update({'HeaderProtectionKey': key, 'ContentPaddingAddition': '0-32',
                                               'RekeyAfterTime': '100-120', 'RandomTrailers': 'true'})
    uapi = compile_uapi(config, b's' * 32, set())
    assert 'header_protection_key=' + (b'k' * 32).hex() in uapi
    assert 'content_padding_addition=0-32' in uapi and 'rekey_after_time=100-120' in uapi and 'random_trailers=true' in uapi
    config.profile.protocol.parameters['S4'] = 11
    with pytest.raises(ValueError, match='S1-S4'):
        compile_uapi(config, b's' * 32, set())
    config.profile.protocol.version = '2'
    config.profile.protocol.parameters['S4'] = 15
    with pytest.raises(ValueError, match='parameter'):
        compile_uapi(config, b's' * 32, set()), 'AWG 3.1 fields are rejected in an AWG 2 profile'


def test_profile_port_must_be_open_on_node(tmp_path, monkeypatch):
    monkeypatch.setenv('AWG_LISTEN_PORTS', '40000-40002')
    agent = service(tmp_path)
    assert agent.capabilities().listen_ports == [40000, 40001, 40002]
    result = agent.validate(desired())  # profile uses 51820
    assert not result.valid and '40000-40002' in result.issues[0].message
    config = desired()
    config.profile.endpoint.port = 40001
    assert agent.validate(config).valid
    monkeypatch.delenv('AWG_LISTEN_PORTS')
    assert service_any_port_ok(agent)
    agent.close()


def service_any_port_ok(agent):
    return agent.capabilities().listen_ports == [] and agent.validate(desired()).valid
