import importlib.util
import base64
from pathlib import Path
import pytest
import yaml


def test_renderers_exist():
    assert importlib.util.find_spec('subscription_renderers') is not None


def test_mihomo_golden_and_stock_semantics(peer):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    stock = b'proxies:\n- name: stock\n  type: direct\nproxy-groups:\n- name: select\n  type: select\n  proxies: [stock]\nrules: ["MATCH,select"]\n'
    output = enrich(stock, 'application/yaml', identify('mihomo/1.19.30'), [peer])
    expected = Path(__file__).with_name('fixtures').joinpath('mihomo.yaml').read_bytes()
    assert output == expected
    parsed = yaml.safe_load(output)
    assert parsed['proxies'][0] == yaml.safe_load(stock)['proxies'][0]
    assert parsed['rules'] == ['MATCH,select']
    assert parsed['proxy-groups'][0]['proxies'] == ['stock', peer.name]


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_throne_base64_preserves_stock_lines_exactly(peer, newline):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    plain = ('vless://unchanged#%D0%B0' + newline + 'trojan://unchanged#two' + newline).encode()
    output = enrich(base64.b64encode(plain), 'text/plain', identify('Throne/1.1.5'), [peer])
    decoded = base64.b64decode(output)
    assert decoded.startswith(plain)
    added = decoded[len(plain):].decode()
    expected = Path(__file__).with_name('fixtures').joinpath('throne.uri').read_text().strip()
    assert added == expected + newline


@pytest.mark.parametrize('stock', [b'not a subscription', b'!!!!', b'<html>blocked</html>', b'proxies: [', b'proxies: []\nproxies: []', b'proxies: &x [*x]', b'proxies: []\nsecret: !!python/object:evil {}'])
def test_malformed_stock_never_rewritten(peer, stock):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    assert enrich(stock, 'text/plain', identify('mihomo/1.19.30'), [peer]) == stock


def test_empty_or_unsupported_material_exact_passthrough(peer):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    stock = b'proxies: []\r\n'
    cap = identify('mihomo/1.19.30')
    assert enrich(stock, 'text/yaml', cap, []) == stock
    assert enrich(stock, 'text/yaml', None, [peer]) == stock
    peer.protocol.version = '999'
    assert enrich(stock, 'text/yaml', cap, [peer]) == stock


def test_no_parameter_silently_dropped(peer):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    peer.protocol.parameters['UnknownNewOption'] = 1
    stock = b'proxies: []\n'
    assert enrich(stock, 'text/yaml', identify('mihomo/1.19.30'), [peer]) == stock


def test_incy_documented_uri_payload(peer):
    from subscription_renderers import IncyRenderer, RawAWGRenderer
    from urllib.parse import unquote
    uri = IncyRenderer().entry(peer)
    payload, name = uri.removeprefix('amneziawg://').split('#')
    assert base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode() == RawAWGRenderer().entry(peer)
    assert unquote(name) == peer.name


def test_raw_awg_exact_golden(peer):
    from subscription_renderers import RawAWGRenderer
    expected = Path(__file__).with_name('fixtures').joinpath('raw-awg.conf').read_text()
    assert RawAWGRenderer().entry(peer) == expected


def test_throne_split_routes_not_silently_widened(peer):
    from subscription_renderers import enrich
    from awg_capabilities import identify
    peer.allowed_ips = ['192.0.2.0/24']
    stock = b'vless://unchanged'
    assert enrich(stock, 'text/plain', identify('Throne/1.1.5'), [peer]) == stock
