"""Exact TT rendering and forwarding of pinned endpoint exporter fixtures."""
import json
import base64
from pathlib import Path

import pytest
import yaml

from awg_contracts.trusttunnel import TrustTunnelSubscriptionEntry
from tests.unit.subscription.test_trusttunnel import tt_material, FIXTURES


@pytest.fixture
def tt_entry():
    return TrustTunnelSubscriptionEntry.model_validate(tt_material()['entries'][0])


def test_tt_mihomo_exact_unicode_escaping_golden(tt_entry):
    from subscription_renderers.trusttunnel import enrich
    stock = b'proxies:\n- {name: stock, type: direct}\nproxy-groups:\n- {name: select, type: select, proxies: [stock]}\nrules: ["MATCH,select"]\n'
    body = enrich(stock, [tt_entry], 'mihomo/v1.19.32')
    assert body == Path(__file__).with_name('fixtures').joinpath('trusttunnel-mihomo.yaml').read_bytes()
    proxy = yaml.safe_load(body)['proxies'][1]
    assert 'private-key' not in proxy and 'ip' not in proxy and 'amnezia-wg-option' not in proxy


def test_tt_info_exact_unicode_golden(tt_entry):
    from subscription_renderers.trusttunnel import enrich_info
    stock = b'{"response":{"links":["vless://stock"],"user":{"username":"alice"}}}'
    body = enrich_info(stock, [tt_entry], '/api/sub/valid-token/trusttunnel')
    assert body == Path(__file__).with_name('fixtures').joinpath('trusttunnel-info.json').read_bytes().rstrip(b'\n')
    assert json.loads(body)['response']['user'] == {'username': 'alice'}


@pytest.mark.parametrize('ua', ['mihomo/v1.19.31', 'FlClash/99', 'Clash/1.20', 'Throne/2', 'INCY/5',
                               'sing-box/1.14', 'TrustTunnel/1.1.5', 'mihomo/v1.19.32evil'])
def test_tt_capability_does_not_follow_format_or_unverified_ua(tt_entry, ua):
    from subscription_renderers.trusttunnel import enrich
    stock = b'proxies: []\r\n'
    assert enrich(stock, [tt_entry], ua) == stock


@pytest.mark.parametrize('stock', [b'proxies: [', b'proxies: []\nproxies: []', b'proxies: &x [*x]',
                                  b'proxies: []\nx: !!python/object:bad {}', b'{"outbounds":[]}',
                                  b'proxies: [{name: stock}]\nproxy-groups: wrong'])
def test_tt_malformed_stock_is_preserved(tt_entry, stock):
    from subscription_renderers.trusttunnel import enrich
    assert enrich(stock, [tt_entry], 'mihomo/v1.19.32') == stock


def test_tt_duplicates_do_not_replace_stock_or_rewrite_unchanged_body(tt_entry):
    from subscription_renderers.trusttunnel import enrich
    stock = yaml.safe_dump({'proxies': [{'name': tt_entry.name, 'type': 'direct'}]}, allow_unicode=True).encode()
    assert enrich(stock, [tt_entry, tt_entry], 'mihomo/v1.19.32') == stock


def test_native_exports_are_opaque_verbatim_not_generated(tt_entry):
    from subscription_renderers.trusttunnel import native
    assert native(tt_entry, 'deeplink') == (FIXTURES / 'trusttunnel-native.deeplink').read_bytes()
    assert native(tt_entry, 'toml') == (FIXTURES / 'trusttunnel-native.toml').read_bytes()
    tt_entry.native_deeplink = None
    with pytest.raises(ValueError):
        native(tt_entry, 'deeplink')


@pytest.mark.parametrize('encoded', [False, True])
@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_throne_native_link_preserves_stock_and_awg(tt_entry, encoded, newline):
    from subscription_renderers.trusttunnel import enrich
    plain = newline.join([b'vless://stock#Original', b'wg://existing?enable_amnezia=true']) + newline
    stock = base64.b64encode(plain) if encoded else plain
    result = enrich(stock, [tt_entry], 'Throne/1.3.2')
    expected = plain + tt_entry.native_deeplink.get_secret_value().encode() + newline
    assert result == (base64.b64encode(expected) if encoded else expected)


@pytest.mark.parametrize('ua', ['Throne/1.2.4', 'Throne/1.3.0-beta.4', 'Throne/1.3', 'Throne/1.3.2evil', 'Custom/1.3.2'])
def test_throne_unverified_versions_preserve_uri_bytes(tt_entry, ua):
    from subscription_renderers.trusttunnel import enrich
    stock = b'vless://stock\r\n'
    assert enrich(stock, [tt_entry], ua) == stock


def test_throne_missing_native_export_preserves_stock(tt_entry):
    from subscription_renderers.trusttunnel import enrich
    tt_entry.native_deeplink = None
    stock = b'vless://stock\n'
    assert enrich(stock, [tt_entry], 'Throne/1.3.2') == stock


def test_throne_existing_tt_scheme_does_not_block_other_native_entries(tt_entry):
    from subscription_renderers.trusttunnel import enrich
    stock = b'vless://stock\ntt://existing:secret@other.example:443\n'
    assert enrich(stock, [tt_entry], 'Throne/1.3.0') == stock + tt_entry.native_deeplink.get_secret_value().encode() + b'\n'
