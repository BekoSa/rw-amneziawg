"""TrustTunnel management via real Textual UI and HTTP client boundary."""
import json

import httpx
import pytest
from textual.widgets import Button, DataTable, Input, Select, SelectionList, Static, TabbedContent

NODE = '00000000-0000-4000-8000-000000000001'
USER = '00000000-0000-4000-8000-000000000002'
PROFILE = '00000000-0000-4000-8000-000000000003'
SQUAD = '00000000-0000-4000-8000-000000000004'
SECRET = 'private-password-must-never-appear'


class Controller:
    def __init__(self):
        self.calls = []
        self.valid = True
        self.profiles = []
        self.status = []
        self.peers = []
        self.data = {
            'nodes': [{'registration': {'node_id': NODE, 'name': 'de-1', 'enabled': True,
                                        'management_url': 'https://de.example.com:8443'},
                       'online': True, 'deployments': [], 'capabilities': {
                           'agent_version': 'test', 'protocols': [{'adapter_id': 'trusttunnel',
                           'versions': ['1.0'], 'features': ['acme-http01', 'acme-tls-alpn01']}]}}],
            'profiles': [], 'peers': [], 'errors': [],
            'remnawave/squads': [{'uuid': SQUAD, 'name': 'VPN', 'members': 2}],
        }

    def __call__(self, request):
        path = request.url.path.removeprefix('/api/v1/')
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.headers['authorization'] == 'Bearer admin'
        if request.method == 'GET':
            data = {'trusttunnel/profiles': self.profiles, 'trusttunnel/status': self.status,
                    'trusttunnel/peers': self.peers} | self.data
            return httpx.Response(200, json={'items': data[path]})
        if request.method == 'PUT':
            self.profiles = [{'id': body['profile_id'], 'draft': body, 'active': None, 'validated_digest': None}]
            return httpx.Response(200, json={'saved': True})
        if path.endswith('/validate'):
            return httpx.Response(200, json={'valid': self.valid, 'draft_digest': 'b' * 64,
                'results': [{'valid': self.valid, 'issues': [] if self.valid else [
                    {'code': 'PORT_CONFLICT', 'message': 'port conflict'}]}]})
        return httpx.Response(202, json={'accepted': True})


@pytest.fixture
def app():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    fake = Controller()
    application = AWGApp(ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(fake)),
                         refresh_seconds=3600)
    application.fake = fake
    return application


async def new_profile(app, pilot):
    tabs = app.query_one('#tabs', TabbedContent)
    # Fails before implementation by asserting user-facing menu availability.
    assert any(pane.id == 'trusttunnel' for pane in tabs.query('TabPane')), 'TrustTunnel must be reachable from menu'
    tabs.active = 'trusttunnel'
    await pilot.pause()
    await pilot.press('n')
    await pilot.pause()
    screen = app.screen
    assert not screen.query('#pool, #server, #parameters, #version, #allowed'), 'TT has no WG or IPAM fields'
    for key, value in {'name': 'DE · TrustTunnel', 'host': '203.0.113.8', 'port': '8444',
                       'domain': 'vpn.example.com', 'email': 'vpn@example.com'}.items():
        screen.query_one('#' + key, Input).value = value
    screen.query_one('#squads', SelectionList).select(SQUAD)
    return screen


@pytest.mark.asyncio
async def test_tt_menu_save_validate_apply_uses_separate_contract_and_api(app):
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        screen = await new_profile(app, pilot)
        assert screen.query_one('#validate', Button).disabled
        assert screen.query_one('#apply', Button).disabled
        screen.query_one('#challenge', Select).value = 'http-01'
        screen.query_one('#environment', Select).value = 'staging'
        for button in ('save', 'validate', 'apply'):
            screen.query_one('#' + button, Button).press()
            await pilot.pause()
        saved = app.fake.profiles[0]['draft']
        assert saved['endpoint'] == {'host': '203.0.113.8', 'port': 8444}
        assert saved['tls'] == {'domain': 'vpn.example.com', 'email': 'vpn@example.com',
                                'environment': 'staging', 'challenge': 'http-01'}
        assert saved['node_ids'] == [NODE] and saved['access']['squad_ids'] == [SQUAD]
        assert not {'network', 'protocol', 'dns_servers'} & saved.keys()
        path = 'trusttunnel/profiles/' + saved['profile_id']
        assert [(m, p) for m, p, _ in app.fake.calls if m != 'GET'] == [
            ('PUT', path), ('POST', path + '/validate'), ('POST', path + '/apply')]
        assert app.fake.calls[-1][0] == 'GET'  # modal dismissed and refreshed
        assert next(b for _, p, b in app.fake.calls if p.endswith('/apply')) == {'expected_digest': 'b' * 64}


@pytest.mark.asyncio
async def test_tt_edits_invalidate_checked_draft_and_failed_validation_blocks_apply(app):
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        screen = await new_profile(app, pilot)
        for button in ('save', 'validate'):
            screen.query_one('#' + button, Button).press()
            await pilot.pause()
        assert not screen.query_one('#apply', Button).disabled
        screen.query_one('#domain', Input).focus()
        await pilot.press('end', 'x')
        await pilot.pause()
        assert screen.query_one('#apply', Button).disabled and screen.query_one('#validate', Button).disabled
        app.fake.valid = False
        for button in ('save', 'validate'):
            screen.query_one('#' + button, Button).press()
            await pilot.pause()
        assert screen.query_one('#apply', Button).disabled
        assert 'PORT_CONFLICT' in str(screen.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_tt_status_expiry_renewal_and_rotation_hide_credentials(app):
    app.fake.profiles = [{'id': PROFILE, 'draft': {'profile_id': PROFILE, 'name': 'DE TT',
        'node_ids': [NODE], 'endpoint': {'host': 'vpn.example.com', 'port': 443},
        'tls': {'domain': 'vpn.example.com', 'email': 'vpn@example.com', 'environment': 'production',
                'challenge': 'auto'}, 'access': {'squad_ids': [SQUAD], 'user_ids': []}}, 'active': None}]
    app.fake.status = [{'id': 'deployment', 'profile_id': PROFILE, 'node_id': NODE,
        'actual': {'state': 'DEGRADED', 'desired_revision': 3, 'applied_revision': 2,
            'healthy': False, 'certificate_state': 'ERROR', 'not_after': '2026-10-20T00:00:00Z',
            'next_attempt_at': '2026-10-10T00:00:00Z', 'certificate_error_code': 'ACME_RENEWAL_FAILED'}}]
    app.fake.peers = [{'id': 'peer', 'user_id': USER, 'profile_id': PROFILE, 'node_id': NODE,
        'credential_generation': 2, 'credential_username': 'opaque-username', 'present': True,
        'user_status': 'ACTIVE', 'password': SECRET, 'native_deeplink': 'tt://?' + SECRET}]
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        tabs = app.query_one('#tabs', TabbedContent)
        assert any(pane.id == 'trusttunnel' for pane in tabs.query('TabPane'))
        tabs.active = 'trusttunnel'
        await pilot.pause()
        table = app.query_one('#trusttunnel-table', DataTable)
        text = ' '.join(str(cell) for cell in table.get_row_at(0))
        assert '3 / 2' in text and 'DEGRADED' in text and '20.10' in text
        assert 'ACME_RENEWAL_FAILED' in text and '10.10' in text and 'acme-http01' in text
        assert 'trusttunnel 1.0' in text, 'operator sees negotiated Agent protocol version'
        tabs.active = 'trusttunnel-peers'
        await pilot.pause()
        peers = app.query_one('#trusttunnel-peers-table', DataTable)
        text = ' '.join(str(cell) for cell in peers.get_row_at(0))
        assert USER in text and '2' in text and 'активен' in text
        assert SECRET not in text and 'tt://' not in text
        await pilot.press('k')
        await pilot.pause()
        assert ('POST', f'trusttunnel/users/{USER}/credentials/rotate', None) in app.fake.calls


@pytest.mark.asyncio
async def test_tt_rejects_invalid_contact_without_api_mutation(app):
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        screen = await new_profile(app, pilot)
        screen.query_one('#domain', Input).value = 'https://vpn.example.com/'
        screen.query_one('#save', Button).press()
        await pilot.pause()
        assert not any(m == 'PUT' for m, _, _ in app.fake.calls)
        assert screen.query_one('#apply', Button).disabled


@pytest.mark.asyncio
async def test_tt_edit_preserves_explicit_access_and_disabled_profile(app):
    app.fake.profiles = [{'id': PROFILE, 'draft': {'profile_id': PROFILE, 'name': 'Existing TT',
        'enabled': False, 'node_ids': [NODE], 'endpoint': {'host': 'vpn.example.com', 'port': 8443},
        'tls': {'domain': 'vpn.example.com', 'email': 'vpn@example.com', 'environment': 'production',
                'challenge': 'tls-alpn-01'}, 'access': {'squad_ids': [SQUAD], 'user_ids': [USER]}},
        'active': None}]
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app.query_one('#tabs', TabbedContent).active = 'trusttunnel'
        await pilot.pause()
        await pilot.press('e')
        await pilot.pause()
        screen = app.screen
        assert screen.query_one('#challenge', Select).value == 'tls-alpn-01'
        assert not screen.query_one('#validate', Button).disabled
        assert screen.query_one('#apply', Button).disabled, 'opening never trusts stale validation'
        screen.query_one('#save', Button).press()
        await pilot.pause()
        saved = next(b for m, _, b in app.fake.calls if m == 'PUT')
        assert saved['profile_id'] == PROFILE and saved['enabled'] is False
        assert saved['access']['user_ids'] == [USER] and saved['access']['squad_ids'] == [SQUAD]


@pytest.mark.asyncio
async def test_tt_requires_one_node_before_saving(app):
    app.fake.data['nodes'] = []
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        screen = await new_profile(app, pilot)
        screen.query_one('#save', Button).press()
        await pilot.pause()
        assert not any(m == 'PUT' for m, _, _ in app.fake.calls)
        assert 'ноду' in str(screen.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_tt_error_response_is_sanitized_and_awg_still_usable():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    fake = Controller()
    def controller(request):
        if '/trusttunnel/' in request.url.path:
            return httpx.Response(503, json={'detail': SECRET + ' tt://?private'})
        return fake(request)
    app = AWGApp(ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(controller)),
                 refresh_seconds=3600)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app.query_one('#tabs', TabbedContent).active = 'trusttunnel'
        await pilot.pause()
        status = str(app.query_one('#trusttunnel-status', Static).render())
        assert '503' in status and SECRET not in status and 'tt://' not in status
        app.query_one('#tabs', TabbedContent).active = 'profiles'
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        assert app.screen.query('#pool, #parameters'), 'AWG editor remains usable'


@pytest.mark.asyncio
@pytest.mark.parametrize('export_format,content', [('deeplink', 'tt://?' + SECRET),
    ('toml', 'username = "opaque"\npassword = "' + SECRET + '"\n')])
async def test_tt_export_fetches_secret_only_after_explicit_action(export_format, content):
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    from textual.widgets import TextArea
    fake = Controller()
    fake.peers = [{'id': PROFILE, 'user_id': USER, 'profile_id': PROFILE, 'node_id': NODE,
        'credential_generation': 2, 'present': True, 'user_status': 'ACTIVE'}]
    export_calls = []
    def controller(request):
        if request.url.path.endswith('/export'):
            assert request.headers['authorization'] == 'Bearer admin'
            export_calls.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={'peer_id': PROFILE, 'format': export_format, 'content': content})
        return fake(request)
    app = AWGApp(ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(controller)),
                 refresh_seconds=3600)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app.query_one('#tabs', TabbedContent).active = 'trusttunnel-peers'
        await pilot.pause()
        assert not export_calls, 'ordinary peer table never fetches credentials'
        await pilot.press('x')
        await pilot.pause()
        assert app.screen.query('#export'), 'native export must be reachable for selected peer'
        assert not export_calls, 'opening dialog is not consent to show secret'
        screen = app.screen
        screen.query_one('#format', Select).value = export_format
        screen.query_one('#export', Button).press()
        await pilot.pause()
        assert screen.query_one('#export-content', TextArea).text == content
        assert export_calls == [('POST', f'/api/v1/trusttunnel/peers/{PROFILE}/export', {'format': export_format})]
        screen.query_one('#close', Button).press()
        await pilot.pause()
        assert app.screen is not screen
        table = app.query_one('#trusttunnel-peers-table', DataTable)
        assert SECRET not in str(table.get_row_at(0)) and 'tt://' not in str(table.get_row_at(0))


@pytest.mark.asyncio
async def test_tt_export_errors_never_echo_controller_secret_body():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    from textual.widgets import TextArea
    fake = Controller()
    fake.peers = [{'id': PROFILE, 'user_id': USER, 'profile_id': PROFILE, 'node_id': NODE,
        'credential_generation': 2, 'present': True, 'user_status': 'ACTIVE'}]
    def controller(request):
        if request.url.path.endswith('/export'):
            return httpx.Response(401, json={'detail': SECRET})
        return fake(request)
    app = AWGApp(ControllerAPI('http://controller', 'admin', transport=httpx.MockTransport(controller)),
                 refresh_seconds=3600)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        app.query_one('#tabs', TabbedContent).active = 'trusttunnel-peers'
        await pilot.pause()
        await pilot.press('x')
        await pilot.pause()
        assert app.screen.query('#export'), 'native export dialog is reachable'
        app.screen.query_one('#export', Button).press()
        await pilot.pause()
        assert app.screen.query_one('#export-content', TextArea).text == ''
        status = str(app.screen.query_one('#status', Static).render())
        assert '401' in status and SECRET not in status
