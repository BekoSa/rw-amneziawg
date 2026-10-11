"""TrustTunnel delivery: verified core, stock access first, protected native exports."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from tests.unit.subscription.test_gateway import request, material


PASSWORD = 'A"quoted\\password:<>&' + 'x' * 32
FIXTURES = Path(__file__).resolve().parents[2] / 'golden' / 'fixtures'
NATIVE = (FIXTURES / 'trusttunnel-native.deeplink').read_text()
TOML = (FIXTURES / 'trusttunnel-native.toml').read_text()


def tt_material(native=True):
    return {'user_uuid': '00000000-0000-4000-8000-000000000003',
            'generated_at': datetime.now(timezone.utc).isoformat(), 'entries': [{
        'profile_id': '00000000-0000-4000-8000-000000000001',
        'node_id': '00000000-0000-4000-8000-000000000002',
        'peer_id': '00000000-0000-4000-8000-000000000004',
        'name': 'Москва: "TT" #1', 'endpoint': {'host': 'tt.example.com', 'port': 8443},
        'tls_server_name': 'tt.example.com', 'username': 'alice', 'password': PASSWORD,
        'native_deeplink': NATIVE if native else None, 'native_toml': TOML if native else None}]}


def controller(req):
    if req.url.path.endswith('/trusttunnel/material'):
        assert req.headers['authorization'] == 'Bearer service-secret'
        assert json.loads(req.content) == {'subscription_token': 'valid-token'}
        return httpx.Response(200, json=tt_material())
    data = material()
    data['peers'] = []
    return httpx.Response(200, json=data)


@pytest.mark.asyncio
async def test_verified_core_receives_tt_and_updated_validators(caplog):
    stock_body = b'proxies: []\nproxy-groups: [{name: select, type: select, proxies: []}]\nrules: ["MATCH,select"]\n'
    result = await request(lambda req: httpx.Response(200, content=stock_body,
        headers={'etag': 'stock', 'subscription-userinfo': 'upload=3'}), controller,
        headers={'user-agent': 'mihomo/v1.19.32'})
    parsed = yaml.safe_load(result.content)
    assert parsed['proxies'][0]['type'] == 'trusttunnel'
    assert parsed['proxies'][0]['password'] == PASSWORD
    assert parsed['proxy-groups'][0]['proxies'] == ['Москва: "TT" #1']
    assert parsed['rules'] == ['MATCH,select']
    assert result.headers['subscription-userinfo'] == 'upload=3'
    assert 'etag' not in result.headers and result.headers['cache-control'] == 'private, no-store'
    assert PASSWORD not in caplog.text and NATIVE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('ua', ['mihomo/v1.19.31', 'FlClash/1.19.99', 'TrustTunnel/1.1.5', 'INCY/4', 'Throne/1.3', 'sing-box/1.14'])
async def test_unverified_client_never_gets_tt(ua):
    stock = b'proxies: []\r\n'
    result = await request(lambda req: httpx.Response(200, content=stock, headers={'etag': 'stock'}),
                           controller, headers={'user-agent': ua})
    assert result.content == stock and result.headers['etag'] == 'stock'


@pytest.mark.asyncio
@pytest.mark.parametrize('format,body', [('toml', TOML), ('deeplink', NATIVE)])
async def test_native_download_uses_stock_identity_and_upstream_export(format, body):
    def stock(req):
        assert req.url.path == '/api/sub/valid-token'
        assert req.headers['x-hwid'] == 'phone'
        assert req.url.query == b'foo=one%20two'
        return httpx.Response(200, content=b'vless://stock', headers={'subscription-userinfo': 'upload=3'})
    def exports(req):
        assert req.url.params['native_format'] == format
        return controller(req)
    result = await request(stock, exports, headers={'x-hwid': 'phone'},
                           path=f'/api/sub/valid-token/trusttunnel?format={format}&i=0&foo=one%20two')
    assert result.content == body.encode() and result.status_code == 200
    assert result.headers['cache-control'] == 'private, no-store'
    assert 'attachment;' in result.headers['content-disposition']
    assert result.headers['subscription-userinfo'] == 'upload=3'


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [{'x-hwid-limit': '1'}, {'x-hwid-not-supported': '1'}])
async def test_native_download_cannot_bypass_stock_hwid(blocked):
    result = await request(lambda req: httpx.Response(200, content=b'blocked', headers=blocked),
        lambda req: pytest.fail('blocked stock must not request credentials'),
        path='/api/sub/valid-token/trusttunnel')
    assert result.content == b'blocked' and result.headers.get('cache-control') != 'private, no-store'


@pytest.mark.asyncio
async def test_info_has_native_and_authenticated_toml_download_actions():
    stock = b'{"response":{"isFound":true,"links":["vless://stock"],"user":{"username":"alice"}}}'
    result = await request(lambda req: httpx.Response(200, content=stock,
        headers={'profile-web-page-url': 'https://sub.example.com/api/sub/valid-token'}),
        controller, path='/api/sub/valid-token/info')
    target = result.json()['response']
    assert target['links'] == ['vless://stock', NATIVE]
    assert target['user'] == {'username': 'alice'}
    assert target['trusttunnel']['downloads'][0]['url'] == 'https://sub.example.com/api/sub/valid-token/trusttunnel?format=toml&i=0'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/sub/valid-token', '/api/sub/valid-token/info', '/api/sub/valid-token/trusttunnel'])
async def test_tt_outage_returns_original_stock(path):
    stock = b'{"response":{"links":["vless://stock"]}}' if path.endswith('/info') else b'proxies: []\r\n'
    def outage(req):
        if '/trusttunnel/' in req.url.path:
            raise httpx.ConnectError('secret-password')
        return controller(req)
    result = await request(lambda req: httpx.Response(200, content=stock, headers={'etag': 'stock'}),
                           outage, headers={'user-agent': 'mihomo/v1.19.32'}, path=path)
    assert result.content == stock and result.headers['etag'] == 'stock'


@pytest.mark.asyncio
@pytest.mark.parametrize('ua,count', [('clash.meta/v1.19.32', 1), ('mihomo/v1.19.31', 0), ('FlClash/2', 0)])
async def test_proxy_provider_checks_core_for_tt(ua, count):
    result = await request(lambda req: httpx.Response(200, content=b'stock'), controller,
                           headers={'user-agent': ua, 'x-awg-provider': 'mihomo'})
    assert len(yaml.safe_load(result.content)['proxies']) == count


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['stale', 'empty', 'oversize', 'encoded'])
async def test_unpublished_tt_material_preserves_body_and_cache(mutation):
    data = tt_material()
    if mutation == 'stale':
        data['generated_at'] = '2000-01-01T00:00:00Z'
    if mutation == 'empty':
        data['entries'] = []
    def broken(req):
        if '/trusttunnel/' not in req.url.path:
            return controller(req)
        if mutation == 'oversize':
            return httpx.Response(200, content=b'x' * 10000)
        return httpx.Response(200, json=data, headers={'content-encoding': 'custom'} if mutation == 'encoded' else {})
    result = await request(lambda req: httpx.Response(200, content=b'proxies: []\r\n', headers={'etag': 'stock'}),
        broken, headers={'user-agent': 'mihomo/v1.19.32'}, max_material_bytes=4096)
    assert result.content == b'proxies: []\r\n' and result.headers['etag'] == 'stock'


@pytest.mark.asyncio
async def test_tt_outage_keeps_existing_awg_output():
    def broken(req):
        if '/trusttunnel/' in req.url.path:
            raise httpx.ConnectError('secret')
        return httpx.Response(200, json=material())
    result = await request(lambda req: httpx.Response(200, content=b'proxies: []\n'), broken,
                           headers={'user-agent': 'mihomo/v1.19.32'})
    assert yaml.safe_load(result.content)['proxies'][0]['type'] == 'wireguard'


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{'if-none-match': 'stock'}, {'cache-control': 'public'}])
async def test_native_stock_rules_and_conditions_prevent_export(headers):
    def stock(req):
        return httpx.Response(200, content=b'stock', headers={'cache-control': 'no-transform'})
    result = await request(stock, lambda req: pytest.fail('stock rule forbids export'), headers=headers,
                           path='/api/sub/valid-token/trusttunnel')
    assert result.content == b'stock'


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['i=-1', 'i=999', 'format=evil', 'i=0&i=1', 'format=toml&format=deeplink'])
async def test_native_invalid_controls_never_expose_other_credentials(query):
    result = await request(lambda req: httpx.Response(200, content=b'stock'), controller,
                           path='/api/sub/valid-token/trusttunnel?' + query)
    assert result.status_code == 400 or result.content == b'stock'


@pytest.mark.asyncio
async def test_provider_missing_hwid_cannot_publish_tt():
    result = await request(lambda req: httpx.Response(200, content=b'stock', headers={'x-hwid-not-supported': '1'}),
        controller, headers={'user-agent': 'mihomo/v1.19.32', 'x-awg-provider': 'mihomo'})
    assert yaml.safe_load(result.content)['proxies'] == []


@pytest.mark.asyncio
async def test_tt_awg_controller_outage_does_not_block_independent_tt():
    def only_tt(req):
        if '/trusttunnel/' in req.url.path:
            return controller(req)
        raise httpx.ConnectError('AWG service unavailable')
    result = await request(lambda req: httpx.Response(200, content=b'proxies: []\n'), only_tt,
                           headers={'user-agent': 'mihomo/v1.19.32'})
    assert yaml.safe_load(result.content)['proxies'][0]['type'] == 'trusttunnel'


@pytest.mark.asyncio
async def test_provider_awg_outage_does_not_block_independent_tt():
    def only_tt(req):
        if '/trusttunnel/' in req.url.path:
            return controller(req)
        raise httpx.ConnectError('AWG service unavailable')
    result = await request(lambda req: httpx.Response(200, content=b'stock'), only_tt,
        headers={'user-agent': 'mihomo/v1.19.32', 'x-awg-provider': 'mihomo'})
    assert result.status_code == 200
    assert yaml.safe_load(result.content)['proxies'][0]['type'] == 'trusttunnel'


@pytest.mark.asyncio
@pytest.mark.parametrize('stock_headers,request_headers', [
    ({'cache-control': 'private, NO-TRANSFORM'}, {}),
    ({'content-encoding': 'custom'}, {}),
    ({}, {'if-none-match': 'stock-etag'}),
    ({}, {'if-match': 'stock-etag'}),
    ({}, {'if-modified-since': 'Thu, 01 Oct 2026 00:00:00 GMT'}),
    ({}, {'if-unmodified-since': 'Thu, 01 Oct 2026 00:00:00 GMT'}),
    ({}, {'if-range': 'stock-etag'}),
    ({}, {'range': 'bytes=0-9'}),
])
async def test_provider_stock_transform_gate_prevents_tt_credentials(stock_headers, request_headers):
    def guarded_controller(req):
        if '/trusttunnel/' in req.url.path:
            pytest.fail('stock transform gate must prevent TT credential lookup')
        return controller(req)
    response = await request(lambda req: httpx.Response(200, content=b'stock', headers=stock_headers),
        guarded_controller, headers={'user-agent': 'mihomo/v1.19.32', 'x-awg-provider': 'mihomo',
                                     **request_headers})
    assert response.status_code == 200
    assert yaml.safe_load(response.content) == {'proxies': []}
