import importlib.util


def test_contract_package_is_available():
    assert importlib.util.find_spec('awg_contracts') is not None


def test_unknown_fields_and_secret_server_key_are_rejected():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import Endpoint
    with pytest.raises(ValidationError):
        Endpoint(host='node.example', port=51820, server_private_key='secret')


def test_protocol_parameters_cannot_contain_config_injection():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import ProtocolSpec
    with pytest.raises(ValidationError):
        ProtocolSpec(adapter_id='amneziawg', version='test', parameters={'Jc': '1\nPrivateKey = stolen'})


def test_ready_requires_matching_revision_and_health():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import ActualDeployment
    with pytest.raises(ValidationError):
        ActualDeployment(deployment_id='00000000-0000-4000-8000-000000000001', desired_revision=2,
                         applied_revision=1, state='READY', healthy=True)


def test_digest_is_canonical_and_changes_with_content():
    from awg_contracts import content_digest
    assert content_digest({'a': 1, 'b': 2}) == content_digest({'b': 2, 'a': 1})
    assert content_digest({'a': 1}) != content_digest({'a': 2})


def test_network_and_broadcast_cannot_be_server_addresses():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import NetworkSpec
    for address in ('10.81.0.0', '10.81.0.255'):
        with pytest.raises(ValidationError):
            NetworkSpec(ipv4_pool='10.81.0.0/24', server_ipv4=address)


def test_counter_timestamps_require_timezone():
    import pytest
    from datetime import datetime
    from pydantic import ValidationError
    from awg_contracts import PeerTraffic, TrafficSnapshot
    from uuid import UUID
    identifier = UUID(int=1)
    with pytest.raises(ValidationError):
        PeerTraffic(peer_id=identifier, public_key='test', key_generation=1,
                    rx_bytes='0', tx_bytes='0', latest_handshake=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        TrafficSnapshot(node_id=identifier, deployment_id=identifier, runtime_epoch=identifier,
                        sequence=0, peers=[], observed_at=datetime(2026, 1, 1))


def desired_fixture():
    import base64
    from awg_contracts import AWGProfile, DesiredDeployment, PeerSpec
    from uuid import UUID
    profile = AWGProfile(profile_id=UUID(int=1), name='Test',
        endpoint={'host': 'node.example', 'port': 51820},
        network={'ipv4_pool': '10.81.0.0/24', 'server_ipv4': '10.81.0.1',
                 'ipv6_pool': 'fd81::/64', 'server_ipv6': 'fd81::1'},
        protocol={'adapter_id': 'amneziawg', 'version': '2'})
    peer = PeerSpec(peer_id=UUID(int=2), user_uuid=UUID(int=3),
                    public_key=base64.b64encode(bytes(range(32))).decode(),
                    ipv4_address='10.81.0.2', ipv6_address='fd81::2')
    return DesiredDeployment(deployment_id=UUID(int=4), node_id=UUID(int=5),
                             revision=1, profile=profile, peers=[peer]).model_dump(mode='json')


def test_peer_allocation_rejects_reserved_or_foreign_addresses():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import DesiredDeployment
    for address in ('10.81.0.0', '10.81.0.255'):
        payload = desired_fixture()
        payload['peers'][0]['ipv4_address'] = address
        with pytest.raises(ValidationError):
            DesiredDeployment.model_validate(payload)
    for address in ('fd82::2', 'fd81::1'):
        payload = desired_fixture()
        payload['peers'][0]['ipv6_address'] = address
        with pytest.raises(ValidationError):
            DesiredDeployment.model_validate(payload)


def test_revocations_validate_public_keys():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import DesiredDeployment
    payload = desired_fixture()
    payload['revoked_public_keys'] = ['not-a-key']
    with pytest.raises(ValidationError):
        DesiredDeployment.model_validate(payload)


def test_published_schemas_match_runtime_contract(tmp_path):
    from pathlib import Path
    from awg_contracts.export import export
    export(tmp_path)
    published = Path(__file__).resolve().parents[2] / 'packages/contracts/v1'
    for artifact in tmp_path.glob('*.json'):
        assert artifact.read_text() == (published / artifact.name).read_text()


def test_desired_roundtrip_and_revision_confusion_rejected():
    import pytest
    from pydantic import ValidationError
    from awg_contracts import DesiredDeployment, content_digest
    original = DesiredDeployment.model_validate(desired_fixture())
    assert content_digest(original) == content_digest(DesiredDeployment.model_validate_json(original.model_dump_json()))
    for invalid_revision in (True, '1', 0, -1):
        payload = desired_fixture()
        payload['revision'] = invalid_revision
        with pytest.raises(ValidationError):
            DesiredDeployment.model_validate(payload)


def test_material_keys_are_redacted_in_normal_serialization():
    from awg_contracts import SubscriptionPeer
    from uuid import UUID
    peer = SubscriptionPeer(profile_id=UUID(int=1), node_id=UUID(int=2), name='Secret test',
        endpoint={'host': 'node.example', 'port': 51820}, server_public_key='public',
        client_private_key='must-not-leak', ipv4_address='10.81.0.2',
        protocol={'adapter_id': 'amneziawg', 'version': '2'})
    assert 'must-not-leak' not in repr(peer)
    assert 'must-not-leak' not in peer.model_dump_json()
    assert peer.client_private_key.get_secret_value() == 'must-not-leak'
