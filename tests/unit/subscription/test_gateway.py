import importlib.util
import base64
from datetime import datetime, timezone
import httpx
import pytest


def test_gateway_exists():
    assert importlib.util.find_spec('awg_gateway') is not None


def material():
    return {'user_uuid': '00000000-0000-4000-8000-000000000003',
            'generated_at': datetime.now(timezone.utc).isoformat(), 'peers': [{
        'profile_id': '00000000-0000-4000-8000-000000000001',
        'node_id': '00000000-0000-4000-8000-000000000002', 'name': 'AWG',
        'endpoint': {'host': 'de.example.com', 'port': 51820},
        'server_public_key': base64.b64encode(b'P' * 32).decode(),
        'client_private_key': base64.b64encode(b'S' * 32).decode(),
        'ipv4_address': '10.81.0.2',
        'protocol': {'adapter_id': 'amneziawg-go-v3', 'version': '2',
                     'parameters': {'Jc': 4, 'Jmin': 40, 'Jmax': 70, 'S1': 30, 'S2': 40,
                                    'H1': '123456', 'H2': '234567', 'H3': '345678', 'H4': '456789'}}}]}


async def request(stock_handler, controller_handler, headers=None, path='/api/sub/valid-token', **settings):
    from awg_gateway import GatewaySettings, create_app
    async with httpx.AsyncClient(transport=httpx.MockTransport(stock_handler)) as stock:
        async with httpx.AsyncClient(transport=httpx.MockTransport(controller_handler)) as controller:
            app = create_app(GatewaySettings(stock_url='http://stock', controller_url='http://controller',
                                              controller_token='service-secret', **settings), stock, controller)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
                return await client.get(path, headers=headers or {'user-agent': 'mihomo/1.19.30'})


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 301, 304, 403, 404, 429, 500])
async def test_controller_failure_preserves_stock(status):
    body = b'proxies: []\r\n' if status != 304 else b''
    def stock(req):
        return httpx.Response(status, content=body, headers={'subscription-userinfo': 'upload=1; download=2',
                              'profile-title': 'base64:YWJj', 'etag': 'stock-etag', 'content-type': 'text/yaml'})
    def unavailable(req):
        raise httpx.ConnectError('internal secret must never leak')
    result = await request(stock, unavailable)
    assert result.status_code == status
    assert result.content == body
    assert result.headers['etag'] == 'stock-etag'
    assert result.headers['subscription-userinfo'] == 'upload=1; download=2'
    assert 'secret' not in result.text


@pytest.mark.asyncio
async def test_authoritative_stock_first_and_hwid_identity_passthrough():
    calls = []
    def stock(req):
        calls.append('stock')
        assert req.headers['x-hwid'] == 'device-1'
        assert 'x-user-uuid' not in req.headers
        assert 'x-forwarded-for' not in req.headers
        assert req.url.query == b'foo=one%20two&foo=three'
        return httpx.Response(403, content=b'hwid denied')
    def controller(req):
        calls.append('controller')
        return httpx.Response(200, json=material())
    response = await request(stock, controller, headers={'user-agent': 'mihomo/1.19.30', 'x-hwid': 'device-1',
                             'x-user-uuid': 'victim', 'x-forwarded-for': '127.0.0.1'},
                             path='/api/sub/valid-token?foo=one%20two&foo=three')
    assert response.content == b'hwid denied'
    assert calls == ['stock']


@pytest.mark.asyncio
async def test_enrichment_invalidates_stock_validators_and_never_forwards_service_secret():
    def stock(req):
        assert req.headers.get('authorization') != 'Bearer service-secret'
        return httpx.Response(200, content=b'proxies: []\n', headers={'etag': 'stock', 'last-modified': 'yesterday',
            'cache-control': 'public,max-age=3600', 'content-type': 'text/yaml', 'subscription-userinfo': 'upload=1'})
    def controller(req):
        import json
        assert req.headers['authorization'] == 'Bearer service-secret'
        assert json.loads(req.content) == {'subscription_token': 'valid-token'}
        return httpx.Response(200, json=material())
    response = await request(stock, controller)
    assert b'private-key' in response.content
    assert 'etag' not in response.headers
    assert 'last-modified' not in response.headers
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['subscription-userinfo'] == 'upload=1'
    assert int(response.headers['content-length']) == len(response.content)


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{'if-none-match': 'x'}, {'range': 'bytes=0-9'}, {'user-agent': 'Clash/1'}])
async def test_conditional_range_unknown_never_materialized(headers):
    def stock(req):
        return httpx.Response(200, content=b'proxies: []\n')
    def controller(req):
        pytest.fail('material called for request that cannot be enriched')
    response = await request(stock, controller, headers={'user-agent': 'mihomo/1.19.30', **headers})
    assert response.content == b'proxies: []\n'


@pytest.mark.asyncio
async def test_empty_not_ready_material_preserves_stock():
    data = material()
    data['peers'] = []
    response = await request(lambda req: httpx.Response(200, content=b'proxies: []'),
                             lambda req: httpx.Response(200, json=data))
    assert response.content == b'proxies: []'


@pytest.mark.asyncio
async def test_stale_material_rejected():
    data = material()
    data['generated_at'] = '2000-01-01T00:00:00Z'
    response = await request(lambda req: httpx.Response(200, content=b'proxies: []'),
                             lambda req: httpx.Response(200, json=data))
    assert response.content == b'proxies: []'


@pytest.mark.asyncio
async def test_large_stock_streams_unchanged_without_material():
    body = b'x' * 2000
    response = await request(lambda req: httpx.Response(200, content=body),
        lambda req: pytest.fail('oversized stock must skip material'), max_enrichment_bytes=100)
    assert response.content == body


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/users', '/api/auth/login', '/foo', '/api/sub/../../api/users'])
async def test_non_subscription_paths_are_not_exposed(path):
    response = await request(lambda req: pytest.fail('stock admin must not be proxied'),
                             lambda req: pytest.fail('no material'), path=path)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_metrics_not_public():
    response = await request(lambda req: pytest.fail('no upstream'), lambda req: pytest.fail('no material'), path='/metrics')
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_subscription_page_info_links_enriched():
    import json
    stock_body = json.dumps({'response': {'links': ['vless://a#one'], 'user': {'username': 'u'}}}).encode()
    response = await request(lambda req: httpx.Response(200, content=stock_body, headers={'content-type': 'application/json'}),
                             lambda req: httpx.Response(200, json=material()), path='/api/sub/valid-token/info',
                             headers={'user-agent': 'Remnawave Subscription Page'})
    links = response.json()['response']['links']
    assert links[0] == 'vless://a#one' and links[1].startswith('amneziawg://') and links[2].startswith('vpn://')


@pytest.mark.asyncio
async def test_browser_view_of_subscription_url_gets_awg_links(caplog):
    """Stock answers a browser (Accept: text/html) on /api/sub/<id> with the info JSON, not the app list."""
    import json
    stock_body = json.dumps({'isFound': True, 'links': ['vless://a#one'], 'user': {'username': 'u'}}).encode()
    browser = {'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0', 'accept': 'text/html'}
    caplog.set_level('INFO', logger='uvicorn.error')
    response = await request(lambda req: httpx.Response(200, content=stock_body, headers={'content-type': 'application/json'}),
                             lambda req: httpx.Response(200, json=material()), headers=browser)
    links = response.json()['links']
    assert links[0] == 'vless://a#one' and links[-1].startswith('vpn://') and links[-1].endswith('#AWG')
    assert 'awg=added ready_peers=1' in caplog.text and 'valid-token' not in caplog.text
    # Other JSON formats (e.g. sing-box) are not info documents: stock bytes, no private material fetched.
    singbox = json.dumps({'outbounds': [{'type': 'vless'}]}).encode()
    response = await request(lambda req: httpx.Response(200, content=singbox, headers={'content-type': 'application/json'}),
                             lambda req: pytest.fail('no material'), headers={'user-agent': 'SFA/1.12'})
    assert response.content == singbox


@pytest.mark.asyncio
async def test_empty_material_is_logged_without_secrets(caplog):
    caplog.set_level('INFO', logger='uvicorn.error')
    empty = material() | {'peers': []}
    response = await request(lambda req: httpx.Response(200, content=b'proxies: []\n', headers={'content-type': 'text/yaml'}),
                             lambda req: httpx.Response(200, json=empty))
    assert response.content == b'proxies: []\n'
    assert 'client=mihomo awg=unchanged ready_peers=0' in caplog.text and 'service-secret' not in caplog.text


STOCK_MIHOMO = b'proxies:\n- name: stock\n  type: direct\nproxy-groups:\n- name: select\n  type: select\n  proxies: [stock]\n'


@pytest.mark.asyncio
async def test_mihomo_app_gets_provider_at_stock_public_url():
    import yaml
    def stock(req):
        return httpx.Response(200, content=STOCK_MIHOMO, headers={'content-type': 'text/yaml',
                              'profile-web-page-url': 'https://sub.example.com/api/sub/valid-token'})
    response = await request(stock, lambda req: httpx.Response(200, json=material()),
                             headers={'user-agent': 'clash-verge/v2.4.2'})
    parsed = yaml.safe_load(response.content)
    assert parsed['proxy-providers']['AmneziaWG']['url'] == 'https://sub.example.com/api/sub/valid-token'
    assert [p['name'] for p in parsed['proxies']] == ['stock']
    plain = await request(lambda req: httpx.Response(200, content=STOCK_MIHOMO, headers={'content-type': 'text/yaml',
                          'profile-web-page-url': 'http://sub.example.com/x'}),
                          lambda req: pytest.fail('no material without a usable public URL'),
                          headers={'user-agent': 'clash-verge/v2.4.2'})
    assert plain.content == STOCK_MIHOMO


@pytest.mark.asyncio
@pytest.mark.parametrize('ua,count', [('clash.meta/v1.19.31', 1), ('clash.meta/v1.19.13', 0), ('mihomo/unknown', 1)])
async def test_mihomo_provider_matches_core_version(ua, count):
    import yaml
    seen = []
    def stock(req):
        seen.append(req)
        return httpx.Response(200, content=STOCK_MIHOMO, headers={'x-hwid-not-supported': 'true'})
    response = await request(stock, lambda req: httpx.Response(200, json=material()),
                             headers={'user-agent': ua, 'x-awg-provider': 'mihomo'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-store'
    assert len(yaml.safe_load(response.content)['proxies']) == count
    assert seen[0].url.path == '/api/sub/valid-token' and 'x-awg-provider' not in seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers', [(200, {'x-hwid-max-devices-reached': 'true'}), (404, {})])
async def test_mihomo_provider_respects_stock_denial(status, headers):
    response = await request(lambda req: httpx.Response(status, content=b'denied', headers=headers),
                             lambda req: pytest.fail('no material'),
                             headers={'user-agent': 'clash.meta/v1.19.31', 'x-awg-provider': 'mihomo'})
    assert response.status_code in (403, 404) and b'wireguard' not in response.content


@pytest.mark.asyncio
async def test_mihomo_provider_keeps_core_cache_on_controller_failure():
    def broken(req):
        raise httpx.ConnectError('down')
    response = await request(lambda req: httpx.Response(200, content=STOCK_MIHOMO), broken,
                             headers={'user-agent': 'clash.meta/v1.19.31', 'x-awg-provider': 'mihomo'})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_awg_conf_download_requires_stock_access_first():
    seen = []
    def stock(req):
        seen.append(req.url.path)
        return httpx.Response(200, content=b'vless://a#x\n')
    response = await request(stock, lambda req: httpx.Response(200, json=material()), path='/api/sub/valid-token/awg',
                             headers={'user-agent': 'AmneziaWG/1.0'})
    assert seen == ['/api/sub/valid-token'], 'access decided by stock subscription, never forwarded as /awg'
    assert response.status_code == 200 and '[Interface]' in response.text
    assert response.headers['content-disposition'] == 'attachment; filename="AWG.conf"'
    assert response.headers['cache-control'] == 'private, no-store'
    denied = await request(lambda req: httpx.Response(403, content=b'hwid'), lambda req: pytest.fail('no material'),
                           path='/api/sub/valid-token/awg')
    assert denied.status_code == 403 and 'Interface' not in denied.text


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,status', [(False, 404), (True, 200)])
async def test_subpage_api_passthrough_only_when_enabled(enabled, status):
    def stock(req):
        assert req.headers['authorization'] == 'Bearer subpage-token'
        return httpx.Response(200, json={'response': {}})
    response = await request(stock, lambda req: pytest.fail('no material'), path='/api/system/metadata',
                             headers={'authorization': 'Bearer subpage-token'}, subpage_passthrough=enabled)
    assert response.status_code == status


@pytest.mark.asyncio
async def test_admin_api_never_passed_even_in_subpage_mode():
    response = await request(lambda req: pytest.fail('admin API must not be proxied'), lambda req: pytest.fail('no material'),
                             path='/api/users', subpage_passthrough=True)
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('trusted', [False, True])
async def test_forwarded_headers_only_behind_trusted_proxy(trusted):
    def stock(req):
        if trusted:
            assert req.headers['x-forwarded-for'] == '203.0.113.7'
            assert req.headers['x-forwarded-proto'] == 'https'
        else:
            assert 'x-forwarded-for' not in req.headers and 'x-forwarded-proto' not in req.headers
        return httpx.Response(404)
    await request(stock, lambda req: pytest.fail('no material'), headers={'x-forwarded-for': '203.0.113.7'},
                  trust_forwarded=trusted)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/subscription-page-configs/', '/api/subscription-page-configs/00000000-0000-0000-0000-000000000000',
                                  '/api/subscriptions/subpage-config/AbC123', '/api/system/metadata'])
async def test_subpage_routes_of_subscription_page_8(path):
    response = await request(lambda req: httpx.Response(200, json={'response': {}}), lambda req: pytest.fail('no material'),
                             path=path, subpage_passthrough=True)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('header', ['x-hwid-limit', 'x-hwid-max-devices-reached', 'x-hwid-not-supported'])
async def test_hwid_blocked_device_never_gets_awg(header):
    # Stock 3.4.x: blocked device → HTTP 200 + x-hwid-* headers + placeholder entries.
    body = b'vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1#Device%20limit\n'
    stock = lambda req: httpx.Response(200, content=body, headers={header: 'true'})
    response = await request(stock, lambda req: pytest.fail('no material for a blocked device'),
                             headers={'user-agent': 'v2rayNG/1.9'})
    assert response.content == body
    conf = await request(stock, lambda req: pytest.fail('no material for a blocked device'), path='/api/sub/valid-token/awg')
    assert conf.status_code == 403 and 'Interface' not in conf.text


@pytest.mark.asyncio
async def test_real_ip_never_trusted():
    def stock(req):
        assert 'x-real-ip' not in req.headers
        return httpx.Response(404)
    await request(stock, lambda req: pytest.fail('no material'), headers={'x-real-ip': '198.51.100.1'}, trust_forwarded=True)


@pytest.mark.asyncio
async def test_subpage_get_with_json_body_is_forwarded():
    import json
    from awg_gateway import GatewaySettings, create_app
    def stock(req):
        assert json.loads(req.content) == {'requestHeaders': {'user-agent': 'Firefox'}}
        return httpx.Response(200, json={'response': {'subpageConfigUuid': None, 'webpageAllowed': True}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(stock)) as upstream:
        app = create_app(GatewaySettings(stock_url='http://stock', controller_url='http://controller',
                                         controller_token='t', subpage_passthrough=True), upstream, upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            response = await client.request('GET', '/api/subscriptions/subpage-config/AbC123',
                                            json={'requestHeaders': {'user-agent': 'Firefox'}})
    assert response.status_code == 200
