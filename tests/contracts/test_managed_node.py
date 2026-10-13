"""Managed resource boundary: forwarding authority cannot escape the install envelope."""
import hashlib
import importlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError


def contracts():
    c = importlib.import_module('awg_contracts')
    assert hasattr(c, 'NodeDesiredConfig'), 'Managed node contracts are not implemented'
    return c


def envelope_payload():
    return {'envelope_id': str(UUID(int=1)), 'node_id': str(UUID(int=2)),
            'backend_ipv4': '10.245.0.2', 'reserved_ports': [2525]}


def binding_payload(**changes):
    return dict({'binding_id': str(UUID(int=3)), 'deployment_id': str(UUID(int=4)),
                 'transport': 'udp', 'public_port': 49008, 'backend_role': 'awg',
                 'backend_port': 20001}, **changes)


def desired_payload(**changes):
    return dict({'node_id': str(UUID(int=2)), 'revision': 1,
                 'envelope_digest': 'a' * 64, 'enabled_protocols': ['amneziawg'],
                 'bindings': [binding_payload()]}, **changes)


def bound_desired(c, **changes):
    envelope = c.NodeInstallEnvelope.model_validate(envelope_payload())
    desired = c.NodeDesiredConfig.model_validate(desired_payload(
        envelope_digest=c.envelope_digest(envelope), **changes))
    return desired, envelope


def test_managed_roundtrip_preserves_public_internal_port_separation():
    c = contracts()
    desired, envelope = bound_desired(c, enabled_protocols=['trusttunnel'], bindings=[
        binding_payload(transport='tcp', public_port=8443, backend_role='trusttunnel', backend_port=8444)])
    c.validate_node_envelope(desired, envelope)
    wire = desired.model_dump(mode='json')
    assert wire['bindings'][0]['public_port'] == 8443
    assert wire['bindings'][0]['backend_port'] == 8444
    assert c.NodeDesiredConfig.model_validate_json(json.dumps(wire)) == desired


@pytest.mark.parametrize('field,value', [('backend_ipv4', '127.0.0.1'),
                                       ('backend_address', '169.254.169.254'),
                                       ('command', 'docker exec stock sh')])
def test_binding_rejects_controller_supplied_destination_or_command(field, value):
    c = contracts()
    with pytest.raises(ValidationError):
        c.IngressBinding.model_validate(binding_payload(**{field: value}))


@pytest.mark.parametrize('role,transport,port', [
    ('awg', 'udp', 8443), ('awg', 'tcp', 20001), ('awg', 'udp', 19999),
    ('awg', 'udp', 30000), ('trusttunnel', 'tcp', 8443), ('trusttunnel', 'udp', 8444),
    ('acme-http', 'tcp', 10080), ('acme-http', 'udp', 8080),
    ('acme-tls-alpn', 'tcp', 10443), ('acme-tls-alpn', 'udp', 8444),
])
def test_role_cannot_forward_to_management_solver_or_wrong_transport(role, transport, port):
    c = contracts()
    with pytest.raises(ValidationError):
        c.IngressBinding.model_validate(binding_payload(backend_role=role, transport=transport, backend_port=port))


@pytest.mark.parametrize('role,transport,port,public_port', [
    ('awg', 'udp', 20000, 1), ('awg', 'udp', 29999, 65535),
    ('trusttunnel', 'tcp', 8444, 2443), ('trusttunnel', 'udp', 9443, 2443),
    ('acme-http', 'tcp', 8080, 80), ('acme-tls-alpn', 'tcp', 8444, 443),
])
def test_supported_role_bindings_validate(role, transport, port, public_port):
    c = contracts()
    protocols = ['amneziawg'] if role == 'awg' else ['trusttunnel']
    desired, envelope = bound_desired(c, enabled_protocols=protocols, bindings=[
        binding_payload(backend_role=role, transport=transport, backend_port=port, public_port=public_port)])
    c.validate_node_envelope(desired, envelope)


@pytest.mark.parametrize('role,port,backend', [('acme-http', 81, 8080), ('acme-tls-alpn', 2443, 8444)])
def test_acme_roles_cannot_replace_standard_validation_port(role, port, backend):
    c = contracts()
    with pytest.raises(ValidationError):
        c.IngressBinding.model_validate(binding_payload(
            backend_role=role, transport='tcp', public_port=port, backend_port=backend))


@pytest.mark.parametrize('address', ['127.0.0.1', '169.254.169.254', '::1',
                                   '224.0.0.1', '8.8.8.8', '0.0.0.0'])
def test_backend_envelope_rejects_non_private_bridge_ipv4(address):
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeInstallEnvelope.model_validate(dict(envelope_payload(), backend_ipv4=address))


@pytest.mark.parametrize('field,value', [('revision', True), ('revision', '1'), ('revision', 0),
                                       ('protocol_version', '2.0'), ('api_version', 'node/v0')])
def test_desired_rejects_version_revision_confusion(field, value):
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeDesiredConfig.model_validate(desired_payload(**{field: value}))


def test_duplicate_binding_id_and_public_socket_are_rejected():
    c = contracts()
    for other in (binding_payload(public_port=49009, backend_port=20002),
                  binding_payload(binding_id=str(UUID(int=5)), backend_port=20002)):
        with pytest.raises(ValidationError):
            c.NodeDesiredConfig.model_validate(desired_payload(bindings=[binding_payload(), other]))


def test_wildcard_overlaps_specific_address_on_same_transport():
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeDesiredConfig.model_validate(desired_payload(bindings=[binding_payload(),
            binding_payload(binding_id=str(UUID(int=5)), public_address='192.0.2.10', backend_port=20002)]))


def test_tcp_udp_same_public_port_are_separate_allocations():
    c = contracts()
    desired, envelope = bound_desired(c, enabled_protocols=['trusttunnel'], bindings=[
        binding_payload(transport='tcp', public_port=2443, backend_role='trusttunnel', backend_port=8444),
        binding_payload(binding_id=str(UUID(int=5)), public_port=2443,
                        backend_role='trusttunnel', backend_port=9443)])
    c.validate_node_envelope(desired, envelope)


def test_duplicate_awg_backend_between_deployments_is_rejected():
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeDesiredConfig.model_validate(desired_payload(bindings=[binding_payload(),
            binding_payload(binding_id=str(UUID(int=5)), deployment_id=str(UUID(int=6)), public_port=49009)]))


def test_disabled_protocol_cannot_retain_forwarding_binding():
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeDesiredConfig.model_validate(desired_payload(enabled_protocols=[]))


def test_envelope_blocks_reserved_external_port_and_unapproved_address():
    c = contracts()
    for changes in ({'public_port': 2525}, {'public_address': '192.0.2.10'}):
        desired, envelope = bound_desired(c, bindings=[binding_payload(**changes)])
        with pytest.raises(ValueError):
            c.validate_node_envelope(desired, envelope)


def test_envelope_identity_digest_and_binding_limit_are_enforced():
    c = contracts()
    desired, envelope = bound_desired(c)
    for bad in (desired.model_copy(update={'node_id': UUID(int=10)}),
                desired.model_copy(update={'envelope_digest': 'b' * 64})):
        with pytest.raises(ValueError):
            c.validate_node_envelope(bad, envelope)
    limited = envelope.model_copy(update={'max_bindings': 1})
    two = c.NodeDesiredConfig.model_validate(desired_payload(envelope_digest=c.envelope_digest(limited), bindings=[
        binding_payload(), binding_payload(binding_id=str(UUID(int=5)), deployment_id=str(UUID(int=6)),
                                          public_port=49009, backend_port=20002)]))
    with pytest.raises(ValueError):
        c.validate_node_envelope(two, limited)


def test_canonical_digest_covers_resource_changes_and_envelope():
    c = contracts()
    desired, envelope = bound_desired(c)
    def canonical(model):
        return hashlib.sha256(json.dumps(model.model_dump(mode='json'), sort_keys=True,
                              separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert c.node_digest(desired) == canonical(desired)
    assert c.envelope_digest(envelope) == canonical(envelope)
    other, _ = bound_desired(c, bindings=[binding_payload(public_port=49009)])
    assert c.node_digest(other) != c.node_digest(desired)


def test_ready_requires_committed_matching_revision_and_healthy_backend():
    c = contracts()
    base = {'node_id': str(UUID(int=2)), 'desired_revision': 1, 'accepted_revision': 1,
            'applied_revision': 1, 'last_known_good_revision': 1,
            'applied_digest': 'a' * 64, 'state': 'READY', 'healthy': True,
            'bindings': [{'binding': binding_payload(), 'state': 'ACTIVE', 'backend_ready': True}]}
    assert c.NodeActualState.model_validate(base).healthy
    for changes in ({'healthy': False}, {'applied_revision': 2}, {'accepted_revision': None},
                    {'applied_digest': None}, {'bindings': [{'binding': binding_payload(),
                        'state': 'PREPARED', 'backend_ready': True}]},
                    {'bindings': [{'binding': binding_payload(), 'state': 'ACTIVE', 'backend_ready': False}]}):
        with pytest.raises(ValidationError):
            c.NodeActualState.model_validate(dict(base, **changes))


def test_capabilities_identity_matches_install_envelope():
    c = contracts()
    with pytest.raises(ValidationError):
        c.NodeCapabilities(node_id=UUID(int=9), envelope=c.NodeInstallEnvelope.model_validate(envelope_payload()),
                           installed_protocols=[])


def test_legacy_contracts_do_not_acquire_resource_fields():
    c = contracts()
    for name in ('AgentCapabilities', 'DesiredDeployment', 'TrustTunnelDesired'):
        assert 'envelope_digest' not in getattr(c, name).model_fields
        assert 'bindings' not in getattr(c, name).model_fields
