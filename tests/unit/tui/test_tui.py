"""Terminal UI against a fake Controller: rendering, Save → Validate → Apply, rotation, node bundles."""
import json
from uuid import UUID

import httpx
import pytest


class FakeController:
    def __init__(self):
        self.calls = []
        self.profiles = []
        self.data = {
            'nodes': [{'registration': {'node_id': '00000000-0000-4000-8000-000000000001', 'name': 'de-1',
                                        'management_url': 'https://de:8443', 'enabled': True},
                       'online': True, 'last_seen_at': None,
                       'deployments': [{'deployment_id': 'd1', 'state': 'READY', 'applied_revision': 3,
                                        'desired_revision': 3, 'listen_port': 51820}]}],
            'peers': [{'id': 'p1', 'user_id': 'u1', 'username': 'alice', 'present': True, 'user_status': 'ACTIVE',
                       'ipv4': '10.8.0.2', 'latest_handshake': None, 'rx_total': '1048576', 'tx_total': '2048'},
                      {'id': 'p2', 'user_id': 'u2', 'username': 'bob', 'present': False, 'user_status': 'DISABLED',
                       'ipv4': '10.8.0.3', 'latest_handshake': None, 'rx_total': '0', 'tx_total': '0'}],
            'errors': [], 'remnawave/squads': [{'uuid': 'a9204f4f-aa4a-4dab-9028-d4fabeaca43c', 'name': 'AWG', 'members': 2}]}

    def __call__(self, request: httpx.Request):
        path = request.url.path.removeprefix('/api/v1/')
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.headers['authorization'] == 'Bearer admin'
        if request.method == 'GET' and path == 'profiles':
            return httpx.Response(200, json={'items': self.profiles})
        if request.method == 'GET':
            return httpx.Response(200, json={'items': self.data[path]})
        if request.method == 'PUT':
            self.profiles = [{'id': body['profile_id'], 'draft': body, 'active': None, 'validated_digest': None}]
            return httpx.Response(200, json={'saved': True})
        if path.endswith('/validate'):
            return httpx.Response(200, json={'valid': True, 'draft_digest': 'a' * 64, 'results': [{'valid': True, 'issues': []}]})
        return httpx.Response(202, json={'accepted': True})


@pytest.fixture
def app():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    fake = FakeController()
    application = AWGApp(ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(fake)), refresh_seconds=3600)
    application.fake = fake
    return application


@pytest.mark.asyncio
async def test_overview_and_peer_states(app):
    from textual.widgets import DataTable, Static
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        summary = str(app.query_one('#summary', Static).render())
        assert 'Ноды READY: 1 / 1' in summary and 'Активные пользователи AWG: 1' in summary
        table = app.query_one('#peers-table', DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert rows[0][:3] == ['alice', 'активен', '10.8.0.2'] and rows[0][4] == '1.0 MiB'
        assert rows[1][1] == 'отключён'


@pytest.mark.asyncio
async def test_profile_save_validate_apply_with_squad_and_generated_31(app):
    from textual.widgets import Button, Input, Select, SelectionList, TextArea
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        screen = app.screen
        assert screen.query_one('#apply', Button).disabled and screen.query_one('#validate', Button).disabled
        assert 'HeaderProtectionKey' in screen.query_one('#parameters', TextArea).text, 'new profiles default to AWG 3.1'
        screen.query_one('#name', Input).value = 'DE · AWG'
        screen.query_one('#host', Input).value = 'de.example.com'
        screen.query_one('#node', Select).value = '00000000-0000-4000-8000-000000000001'
        screen.query_one('#squads', SelectionList).select('a9204f4f-aa4a-4dab-9028-d4fabeaca43c')
        for button in ('save', 'validate', 'apply'):
            screen.query_one(f'#{button}', Button).press()
            await pilot.pause()
        methods = [(m, p) for m, p, _ in app.fake.calls if m != 'GET']
        profile_id = app.fake.profiles[0]['id']
        assert methods == [('PUT', f'profiles/{profile_id}'), ('POST', f'profiles/{profile_id}/validate'),
                           ('POST', f'profiles/{profile_id}/apply')]
        saved = next(b for m, _, b in app.fake.calls if m == 'PUT')
        assert saved['protocol']['version'] == '3.1' and saved['access']['squad_ids'] == ['a9204f4f-aa4a-4dab-9028-d4fabeaca43c']
        assert isinstance(saved['protocol']['parameters']['Jc'], int)
        assert next(b for m, p, b in app.fake.calls if p.endswith('/apply')) == {'expected_digest': 'a' * 64}


@pytest.mark.asyncio
async def test_rotate_key_for_selected_user(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.query_one('#tabs').active = 'peers'
        await pilot.pause()
        await pilot.press('k')
        await pilot.pause()
        assert ('POST', 'users/u1/rotate-key', None) in app.fake.calls


def test_parameters_text_roundtrip():
    from awg_tui.app import format_parameters, parse_parameters
    values = {'Jc': 4, 'S1': 20, 'H1': '100-200', 'HeaderProtectionKey': 'a2V5', 'RandomTrailers': 'true'}
    assert parse_parameters(format_parameters(values) + '\n# comment\n') == values
    with pytest.raises(ValueError):
        parse_parameters('Jc = 1\nJc = 2')


def test_node_bundle_is_signed_by_extension_ca(tmp_path):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from awg_tui.pki import node_bundle, parse_bundle
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test CA')])
    now = datetime.now(timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), True).sign(key, hashes.SHA256()))
    (tmp_path / 'ca.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
    (tmp_path / 'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    node = UUID('00000000-0000-4000-8000-0000000000bb')
    bundle = parse_bundle(node_bundle(tmp_path, '00000000-0000-4000-8000-000000000002', node, management_port=2525))
    assert bundle['management_port'] == 2525, 'the port chosen in the TUI travels with the key'
    cert = x509.load_pem_x509_certificate(bundle['cert'].encode())
    cert.verify_directly_issued_by(ca)
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) \
        == [f'{node}.agents.awg.internal']
    with pytest.raises(ValueError):
        parse_bundle('not-a-bundle')


def test_quit_closes_http_client_on_the_app_loop():
    # Regression: closing the client after App.run() returned raised "Event loop is closed".
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    api = ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(FakeController()))
    async def quit_after_load(pilot):
        await pilot.pause()
        await pilot.press('q')
    AWGApp(api, refresh_seconds=3600).run(headless=True, auto_pilot=quit_after_load)
    assert api.http.is_closed



@pytest.mark.asyncio
async def test_register_already_installed_node_by_id(app):
    from textual.widgets import Button, Input
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app.query_one('#tabs').active = 'nodes'
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        screen = app.screen
        screen.query_one('#name', Input).value = 'de-2'
        screen.query_one('#address', Input).value = '203.0.113.5'
        screen.query_one('#node-id', Input).value = '00000000-0000-4000-8000-0000000000cc'
        screen.query_one('#register', Button).press()
        await pilot.pause()
        body = next(b for m, p, b in app.fake.calls if m == 'POST' and p == 'nodes')
        assert body['node_id'] == '00000000-0000-4000-8000-0000000000cc'
        assert body['management_url'] == 'https://203.0.113.5:8443'


def test_suggest_port_uses_node_ports_and_skips_taken():
    from awg_tui.app import suggest_port
    node = {'registration': {'node_id': 'n1'}, 'capabilities': {'listen_ports': [41000, 41001]}}
    taken = [{'id': 'other', 'draft': {'endpoint': {'port': 41000}, 'node_ids': ['n1']}}]
    assert suggest_port(node, taken, 'new') == 41001
    assert suggest_port(node, taken, 'other') == 41000, 'editing keeps its own port available'
    unrestricted = suggest_port({'registration': {'node_id': 'n2'}, 'capabilities': {}}, taken, None)
    assert 20000 <= unrestricted <= 59999


@pytest.mark.asyncio
async def test_new_profile_takes_the_port_published_by_the_node(app):
    from textual.widgets import Input
    app.fake.data['nodes'][0]['capabilities'] = {'agent_version': 't', 'listen_ports': [43210]}
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        assert app.screen.query_one('#port', Input).value == '43210', 'single node: its published port'



def test_init_pki_reissues_controller_certificate_for_a_new_controller_id(tmp_path, monkeypatch):
    import os
    from cryptography import x509
    from awg_tui import setup
    monkeypatch.setattr(os, 'fchown', lambda *a: None)  # tests run unprivileged
    ca, ctl = tmp_path / 'ca', tmp_path / 'ctl'
    ca.mkdir(); ctl.mkdir()
    def uris():
        cert = x509.load_pem_x509_certificate((ctl / 'controller.crt').read_bytes())
        return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.UniformResourceIdentifier)
    setup.init_pki(ca, ctl, '00000000-0000-4000-8000-000000000001', controller_uid=os.getuid())
    first_ca = (ca / 'ca.crt').read_bytes()
    setup.init_pki(ca, ctl, '00000000-0000-4000-8000-000000000002', controller_uid=os.getuid())
    assert uris() == ['spiffe://awg/controller/00000000-0000-4000-8000-000000000002']
    assert (ca / 'ca.crt').read_bytes() == first_ca, 'the CA (and every enrolled node) is kept'
