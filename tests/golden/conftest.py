import base64
import pytest


@pytest.fixture
def peer():
    from awg_contracts import SubscriptionPeer
    return SubscriptionPeer(
        profile_id='00000000-0000-4000-8000-000000000001',
        node_id='00000000-0000-4000-8000-000000000002', name='Германия: "AWG" #1',
        endpoint={'host': 'de.example.com', 'port': 51820},
        server_public_key=base64.b64encode(b'P' * 32).decode(),
        client_private_key=base64.b64encode(b'S' * 32).decode(),
        ipv4_address='10.81.0.2', dns_servers=['1.1.1.1'],
        protocol={'adapter_id': 'amneziawg-go-v3', 'version': '2',
                  'parameters': {'Jc': 4, 'Jmin': 40, 'Jmax': 70, 'S1': 30, 'S2': 40,
                                 'H1': '123456', 'H2': '234567', 'H3': '345678', 'H4': '456789'}})
