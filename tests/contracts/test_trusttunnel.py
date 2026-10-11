"""TrustTunnel boundary regressions: strict input, secret handling and publication safety."""
import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError


def contract():
    module = importlib.import_module('awg_contracts')
    assert hasattr(module, 'TrustTunnelDesired'), 'TrustTunnel contracts are not implemented'
    return module


def profile_payload():
    return {'profile_id': str(UUID(int=1)), 'name': 'Тест TT', 'endpoint': {'host': 'tt.example.org', 'port': 443},
            'node_ids': [str(UUID(int=2))], 'tls': {'domain': 'tt.example.org', 'email': 'ops@example.org'}}


def credential_payload():
    return {'peer_id': str(UUID(int=3)), 'user_uuid': str(UUID(int=4)), 'username': 'opaque-123',
            'password': 'a-very-private-password-with-32-bytes', 'credential_generation': 1}


def desired_payload():
    return {'deployment_id': str(UUID(int=5)), 'node_id': str(UUID(int=2)), 'revision': 1,
            'profile': profile_payload(), 'clients': [credential_payload()]}


def test_password_wire_roundtrip_and_redacted_default_serialization():
    c = contract()
    desired = c.TrustTunnelDesired.model_validate(desired_payload())
    secret = credential_payload()['password']
    assert secret not in repr(desired)
    assert secret not in desired.model_dump_json()
    assert secret not in repr(desired.model_dump())
    wire = c.wire_payload(desired)
    assert wire['clients'][0]['password'] == secret
    restored = c.TrustTunnelDesired.model_validate_json(json.dumps(wire))
    assert c.wire_payload(restored) == wire


def test_digest_hashes_the_real_wire_password_not_the_mask():
    c = contract()
    desired = c.TrustTunnelDesired.model_validate(desired_payload())
    wire = c.wire_payload(desired)
    expected = hashlib.sha256(json.dumps(wire, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert c.trusttunnel_digest(desired) == expected
    payload = desired_payload()
    payload['clients'][0]['password'] = 'another-private-password-with-32-bytes'
    rotated = c.TrustTunnelDesired.model_validate(payload)
    assert c.trusttunnel_digest(rotated) != expected


@pytest.mark.parametrize('domain', ['*.example.org', 'https://example.org', 'example.org/path',
                                   '../example.org', '-example.org', 'example.org\n--exec', '127.0.0.1',
                                   'localhost', 'example..org', 'example.org:443'])
def test_tls_rejects_non_dns_and_injection_domains(domain):
    c = contract()
    with pytest.raises(ValidationError):
        c.TrustTunnelTLS(domain=domain, email='ops@example.org')


def test_tls_normalizes_idna_and_rejects_unsafe_email():
    c = contract()
    assert c.TrustTunnelTLS(domain='ПРИМЕР.РФ', email='ops@example.org').domain == 'xn--e1afmkfd.xn--p1ai'
    for email in ('--server=evil', 'ops@example.org\n--exec', 'https://ops@example.org'):
        with pytest.raises(ValidationError):
            c.TrustTunnelTLS(domain='example.org', email=email)


@pytest.mark.parametrize('attribute', ['peer_id', 'user_uuid', 'username'])
def test_desired_rejects_duplicate_credential_identities(attribute):
    c = contract()
    payload = desired_payload()
    other = dict(credential_payload(), peer_id=str(UUID(int=6)), user_uuid=str(UUID(int=7)), username='other')
    other[attribute] = payload['clients'][0][attribute]
    payload['clients'].append(other)
    with pytest.raises(ValidationError):
        c.TrustTunnelDesired.model_validate(payload)


@pytest.mark.parametrize('field,value', [('revision', True), ('revision', '1'), ('revision', 0)])
def test_desired_rejects_revision_confusion(field, value):
    c = contract()
    payload = desired_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        c.TrustTunnelDesired.model_validate(payload)


def test_profile_does_not_accept_awg_fields_or_multiple_nodes():
    c = contract()
    payload = profile_payload()
    payload['network'] = {'ipv4_pool': '10.1.0.0/24'}
    with pytest.raises(ValidationError):
        c.TrustTunnelProfile.model_validate(payload)
    payload = profile_payload()
    payload['node_ids'].append(str(UUID(int=8)))
    with pytest.raises(ValidationError):
        c.TrustTunnelProfile.model_validate(payload)
    payload = desired_payload()
    payload['node_id'] = str(UUID(int=8))
    with pytest.raises(ValidationError):
        c.TrustTunnelDesired.model_validate(payload)


def test_credential_rejects_short_secret_and_argv_control_injection():
    c = contract()
    for field, value in [('password', 'short'), ('password', 'x' * 32 + '\n'),
                         ('username', '--exec=evil'), ('credential_generation', True)]:
        payload = credential_payload()
        payload[field] = value
        with pytest.raises(ValidationError):
            c.TrustTunnelCredential.model_validate(payload)


def ready_payload():
    now = datetime.now(UTC)
    return {'deployment_id': str(UUID(int=5)), 'state': 'READY', 'healthy': True, 'desired_revision': 1,
            'applied_revision': 1, 'applied_digest': 'a' * 64, 'certificate_state': 'VALID',
            'certificate_generation': 1, 'certificate_environment': 'production',
            'not_after': now + timedelta(days=1), 'fingerprint': 'b' * 64, 'served_fingerprint': 'b' * 64,
            'observed_at': now}


@pytest.mark.parametrize('update', [{'certificate_environment': 'staging'}, {'served_fingerprint': 'c' * 64},
                                   {'not_after': datetime(2000, 1, 1, tzinfo=UTC)},
                                   {'applied_revision': 2}, {'applied_digest': None},
                                   {'certificate_state': 'EXPIRED'}])
def test_ready_excludes_unpublished_or_expired_certificate(update):
    c = contract()
    c.TrustTunnelActual.model_validate(ready_payload())
    with pytest.raises(ValidationError):
        c.TrustTunnelActual.model_validate(dict(ready_payload(), **update))


def test_material_export_is_explicit_and_secret_redacted():
    c = contract()
    entry = c.TrustTunnelSubscriptionEntry(profile_id=UUID(int=1), node_id=UUID(int=2), peer_id=UUID(int=3),
        name='Тест', endpoint={'host': 'tt.example.org', 'port': 443}, tls_server_name='tt.example.org',
        username='opaque-123', password=credential_payload()['password'], credential_generation=1,
        native_deeplink='tt://?secret-credential', native_toml='password="secret-credential"')
    material = c.TrustTunnelMaterial(user_uuid=UUID(int=4), entries=[entry])
    assert 'secret-credential' not in repr(material)
    assert 'secret-credential' not in material.model_dump_json()
    assert c.wire_payload(material)['entries'][0]['native_deeplink'] == 'tt://?secret-credential'
    response = c.TrustTunnelExportResponse(peer_id=UUID(int=3), format='deeplink', content='tt://?private')
    assert 'tt://?private' not in response.model_dump_json()
    assert c.wire_payload(response)['content'] == 'tt://?private'


def test_published_tt_schemas_are_complete_and_match_export(tmp_path):
    contract()
    from awg_contracts.export import export
    export(tmp_path)
    published = Path(__file__).resolve().parents[2] / 'packages/contracts/v1'
    for name in ('TrustTunnelTLS', 'TrustTunnelProfile', 'TrustTunnelCredential', 'TrustTunnelDesired',
                 'TrustTunnelApplyRequest', 'TrustTunnelActual', 'TrustTunnelValidationResult',
                 'TrustTunnelMaterial', 'TrustTunnelSubscriptionEntry', 'TrustTunnelExportRequest',
                 'TrustTunnelExportResponse'):
        schema = json.loads((tmp_path / f'{name}.schema.json').read_text())
        assert schema['additionalProperties'] is False
        assert schema['properties']
        assert (tmp_path / f'{name}.schema.json').read_text() == (published / f'{name}.schema.json').read_text()
    document = json.loads((tmp_path / 'openapi.json').read_text())
    assert document['paths']['/v1/trusttunnel/deployments/{deployment_id}/apply']['put']['requestBody']
    assert document['paths']['/v1/internal/subscriptions/trusttunnel/material']['post']['security'] == [{'serviceToken': []}]
