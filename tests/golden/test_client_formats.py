"""Per-client AWG formats (AmneziaVPN, INCY, Throne, Mihomo family, generic, Happ) and the subscription page."""
import base64
import json
import struct
import zlib
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

URI_STOCK = b'vless://unchanged@host:443?security=reality#Stock%20one\ntrojan://pass@host:443#two\n'


def decode_vpn_key(key):
    payload = key.removeprefix('vpn://')
    packed = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
    (length,) = struct.unpack('>I', packed[:4])
    data = zlib.decompress(packed[4:])
    assert len(data) == length, 'Qt qCompress length prefix'
    return json.loads(data)


def test_amneziavpn_key_mirrors_client_import(peer):
    from subscription_renderers import AmneziaVpnRenderer, RawAWGRenderer
    document = decode_vpn_key(AmneziaVpnRenderer().entry(peer))
    assert document['defaultContainer'] == 'amnezia-awg'
    assert document['description'] == peer.name, 'server shows under the profile name'
    assert document['hostName'] == 'de.example.com'
    container = document['containers'][0]
    assert container['container'] == 'amnezia-awg'
    awg = container['awg']
    assert awg['isThirdPartyConfig'] is True and awg['port'] == '51820' and awg['transport_proto'] == 'udp'
    last = json.loads(awg['last_config'])
    assert last['config'] == RawAWGRenderer().entry(peer)
    assert last['client_priv_key'] == peer.client_private_key.get_secret_value()
    assert last['server_pub_key'] == peer.server_public_key
    assert last['client_ip'] == '10.81.0.2/32' and last['port'] == 51820
    assert last['Jc'] == '4' and last['H1'] == '123456'


@pytest.mark.parametrize('encoded', [False, True])
def test_incy_and_generic_lines_keep_stock_bytes(peer, encoded):
    from awg_capabilities import identify
    from subscription_renderers import RawAWGRenderer, enrich
    stock = base64.b64encode(URI_STOCK) if encoded else URI_STOCK
    for ua in ('INCY/3.8.8 (iOS)', 'v2rayNG/1.9.30', 'Hiddify/2.5'):
        output = enrich(stock, 'text/plain', identify(ua), [peer])
        plain = base64.b64decode(output) if encoded else output
        assert plain.startswith(URI_STOCK), ua
        line = plain[len(URI_STOCK):].decode().strip()
        payload, name = line.removeprefix('amneziawg://').split('#')
        assert base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode() == RawAWGRenderer().entry(peer)
        assert unquote(name) == peer.name


def test_throne_any_release_gets_amnezia_wg_link(peer):
    from awg_capabilities import identify
    from subscription_renderers import enrich
    output = enrich(URI_STOCK, 'text/plain', identify('Throne/1.2.0'), [peer])
    link = output[len(URI_STOCK):].decode().strip()
    parts = urlsplit(link)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.scheme == 'wg' and parts.hostname == 'de.example.com' and parts.port == 51820
    assert query['enable_amnezia'] == 'true' and query['jc'] == '4' and query['h4'] == '456789'
    assert query['private_key'] == peer.client_private_key.get_secret_value()


@pytest.mark.parametrize('ua', ['FlClash/0.8.80', 'clash-verge/v2.2.3', 'mihomo/1.20.1'])
def test_mihomo_family_gets_wireguard_proxy(peer, ua):
    from awg_capabilities import identify
    from subscription_renderers import enrich
    stock = b'proxies:\n- name: stock\n  type: direct\nproxy-groups:\n- name: select\n  type: select\n  proxies: [stock]\n'
    parsed = yaml.safe_load(enrich(stock, 'text/yaml', identify(ua), [peer]))
    awg = parsed['proxies'][1]
    assert awg['name'] == peer.name and awg['type'] == 'wireguard' and awg['amnezia-wg-option']['jc'] == 4
    assert parsed['proxy-groups'][0]['proxies'] == ['stock', peer.name]


@pytest.mark.parametrize('ua,stock', [
    ('Happ/3.1.0', URI_STOCK),
    ('sing-box/1.13.0', b'{"outbounds": []}'),
    ('Clash/1.18.0', b'proxies:\n- name: stock\n  type: direct\n'),
    ('Stash/2.4', b'proxies:\n- name: stock\n  type: direct\n'),
])
def test_unsupported_formats_left_byte_for_byte(peer, ua, stock):
    from awg_capabilities import identify
    from subscription_renderers import accepts, enrich
    assert enrich(stock, 'text/plain', identify(ua), [peer]) == stock
    assert not accepts(stock, identify(ua)), 'no private material is fetched for these'


def test_subscription_page_info_gets_incy_and_amneziavpn_links(peer):
    from subscription_renderers import enrich_info
    stock = json.dumps({'response': {'isFound': True, 'user': {'username': 'alice'},
                                     'links': ['vless://a#one'], 'ssConfLinks': {}}}).encode()
    document = json.loads(enrich_info(stock, [peer]))
    links = document['response']['links']
    assert links[0] == 'vless://a#one'
    assert links[1].startswith('amneziawg://') and links[2].startswith('vpn://')
    assert document['response']['user'] == {'username': 'alice'}
    assert decode_vpn_key(links[2])['description'] == peer.name


@pytest.mark.parametrize('stock', [b'<html>', b'{"response": {"links": "x"}}', b'[]'])
def test_malformed_info_unchanged(peer, stock):
    from subscription_renderers import enrich_info
    assert enrich_info(stock, [peer]) == stock



@pytest.fixture
def peer31(peer):
    peer.protocol.version = '3.1'
    peer.protocol.parameters.update({'S3': 14, 'S4': 12, 'HeaderProtectionKey': base64.b64encode(b'k' * 32).decode(),
                                     'ContentPaddingAddition': '0-32', 'RandomTrailers': 'true'})
    return peer


def test_awg31_mihomo_uses_v3_device_and_kebab_options(peer31):
    from awg_capabilities import identify
    from subscription_renderers import enrich
    parsed = yaml.safe_load(enrich(b'proxies: []\n', 'text/yaml', identify('mihomo/1.19.31'), [peer31]))
    options = parsed['proxies'][0]['amnezia-wg-option']
    assert options['version'] == 3
    assert options['header-protection-key'] == base64.b64encode(b'k' * 32).decode()
    assert options['content-padding-addition'] == '0-32' and options['random-trailers'] is True


def test_awg31_throne_and_amneziavpn(peer31):
    from awg_capabilities import identify
    from subscription_renderers import AmneziaVpnRenderer, enrich
    link = enrich(URI_STOCK, 'text/plain', identify('Throne/1.3.1'), [peer31])[len(URI_STOCK):].decode().strip()
    query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert query['header_protection_key'] == base64.b64encode(b'k' * 32).decode()
    assert query['content_padding_addition'] == '0-32' and query['random_trailers'] == 'true'
    last = json.loads(decode_vpn_key(AmneziaVpnRenderer().entry(peer31))['containers'][0]['awg']['last_config'])
    assert last['HeaderProtectionKey'] and last['RandomTrailers'] == 'on', 'AmneziaVPN detects 3.1 by these markers'
    assert 'RandomTrailers = on' in last['config'], 'awg-tools accepts only on/off or 0/1'
    assert 'HeaderProtectionKey = ' in last['config']


def test_generated_awg31_profile_reaches_every_client_intact(peer):
    """Every field of a default (generated) 3.1 profile survives each client format."""
    from awg_capabilities import identify
    from awg_config import random_parameters
    from subscription_renderers import AmneziaVpnRenderer, RawAWGRenderer, enrich, snake
    peer.protocol.version = '3.1'
    peer.protocol.parameters = random_parameters('3.1')
    params = peer.protocol.parameters
    options = yaml.safe_load(enrich(b'proxies: []\n', 'text/yaml', identify('mihomo/1.19.31'), [peer]))['proxies'][0]['amnezia-wg-option']
    link = enrich(URI_STOCK, 'text/plain', identify('Throne/1.3.1'), [peer])[len(URI_STOCK):].decode().strip()
    query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    conf = RawAWGRenderer().entry(peer)
    last = json.loads(decode_vpn_key(AmneziaVpnRenderer().entry(peer))['containers'][0]['awg']['last_config'])
    for field, value in params.items():
        boolean = field in ('RandomTrailers', 'DisableCookies')
        assert options[snake(field).replace('_', '-')] == (value == 'true' if boolean else value), field
        assert query[snake(field)] == str(value), field
        expected = ('on' if value == 'true' else 'off') if boolean else str(value)
        assert f'{field} = {expected}\n' in conf and last[field] == expected, field
    assert options['i1'].startswith('<r 2><b 0x8180') and options['disable-cookies'] is True


def test_awg31_not_sent_to_clients_without_confirmed_support(peer31):
    import json as _json
    from awg_capabilities import identify
    from subscription_renderers import enrich, enrich_info
    for ua in ('INCY/3.8.8 (iOS)', 'v2rayNG/1.9.30'):
        assert enrich(URI_STOCK, 'text/plain', identify(ua), [peer31]) == URI_STOCK
    info = _json.dumps({'response': {'links': []}}).encode()
    links = _json.loads(enrich_info(info, [peer31]))['response']['links']
    assert len(links) == 1 and links[0].startswith('vpn://'), 'only AmneziaVPN key for a 3.1 profile'


PLACEHOLDERS = ('vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?encryption=none&type=tcp&security=none#%E2%86%92%20No%20hosts%20found\n'
                'vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?encryption=none&type=tcp&security=none#%E2%86%92%20Check%20Hosts%20tab\n')


def test_placeholders_dropped_only_when_awg_added(peer):
    from awg_capabilities import identify
    from subscription_renderers import enrich
    stock = PLACEHOLDERS.encode() + b'vless://real@host:443#Real\n'
    output = enrich(base64.b64encode(stock), 'text/plain', identify('INCY/3.8.8'), [peer])
    lines = base64.b64decode(output).decode().splitlines()
    assert lines[0] == 'vless://real@host:443#Real' and lines[1].startswith('amneziawg://') and len(lines) == 2
    only_placeholders = enrich(PLACEHOLDERS.encode(), 'text/plain', identify('v2rayNG/1.9'), [peer]).decode().splitlines()
    assert len(only_placeholders) == 1 and only_placeholders[0].startswith('amneziawg://')
    assert enrich(PLACEHOLDERS.encode(), 'text/plain', identify('Happ/3.1.0'), [peer]) == PLACEHOLDERS.encode(), \
        'no AWG added → stock placeholders stay'


def test_mihomo_placeholders_removed_from_proxies_and_groups(peer):
    from awg_capabilities import identify
    from subscription_renderers import enrich
    stock = ('proxies:\n- {name: "→ No hosts found", type: vless, server: 0.0.0.0, port: 1, uuid: 00000000-0000-0000-0000-000000000000}\n'
             'proxy-groups:\n- {name: select, type: select, proxies: ["→ No hosts found"]}\nrules: ["MATCH,select"]\n').encode()
    parsed = yaml.safe_load(enrich(stock, 'text/yaml', identify('mihomo/1.19.31'), [peer]))
    assert [p['name'] for p in parsed['proxies']] == [peer.name]
    assert parsed['proxy-groups'][0]['proxies'] == [peer.name]


def test_subscription_page_names_amneziavpn_key_and_drops_placeholders(peer):
    from urllib.parse import unquote
    from subscription_renderers import enrich_info
    stock = json.dumps({'response': {'links': PLACEHOLDERS.split()}}).encode()
    links = json.loads(enrich_info(stock, [peer]))['response']['links']
    assert len(links) == 2
    vpn = links[1]
    assert unquote(vpn.rsplit('#', 1)[1]) == peer.name, 'page shows the profile name, not "Unknown"'
    assert decode_vpn_key(vpn.split('#', 1)[0])['description'] == peer.name



def test_mihomo_stock_template_group_named_like_placeholder(peer):
    # Stock 3.4.4 "no hosts" template: group "→ Remnawave" shares its name with a placeholder proxy.
    from awg_capabilities import identify
    from subscription_renderers import enrich
    names = ['→ Remnawave', '→ No hosts found']
    stock = yaml.safe_dump({'proxies': [{'name': n, 'type': 'vless', 'server': '0.0.0.0', 'port': 1,
                                         'uuid': '00000000-0000-0000-0000-000000000000'} for n in names],
                            'proxy-groups': [{'name': '→ Remnawave', 'type': 'select', 'proxies': names}],
                            'rules': ['MATCH,→ Remnawave']}, allow_unicode=True).encode()
    parsed = yaml.safe_load(enrich(stock, 'text/yaml', identify('mihomo/1.19.31'), [peer]))
    assert [p['name'] for p in parsed['proxies']] == [peer.name]
    assert parsed['proxy-groups'][0]['proxies'] == [peer.name] and parsed['rules'] == ['MATCH,→ Remnawave']
