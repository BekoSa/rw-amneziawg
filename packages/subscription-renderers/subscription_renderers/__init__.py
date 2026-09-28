"""Pure subscription adapters: unchanged stock bytes on unsupported inputs."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import struct
import zlib
from typing import Protocol
from urllib.parse import quote, urlencode

import yaml
from yaml.events import AliasEvent
from awg_contracts import SubscriptionPeer
from awg_capabilities import FAMILIES, ClientCapability

AWG2_FIELDS = ('Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4',
               'I1', 'I2', 'I3', 'I4', 'I5')
# AWG 3.1 additions (amneziawg-go v3.1 README); ranges are "a" or "a-b" uint32, toggles true/false.
V3_RANGES = ('ContentPaddingAddition', 'RekeyAfterTime', 'RekeyTimeout', 'RejectAfterTime',
             'KeepaliveTimeout', 'MaxHandshakeAttempts')
V3_BOOLS = ('RandomTrailers', 'DisableCookies')
V3_FIELDS = ('HeaderProtectionKey', *V3_RANGES, *V3_BOOLS)
AWG_FIELDS = AWG2_FIELDS + V3_FIELDS
PROTOCOLS = {('amneziawg-go-v3', '2'): set(AWG2_FIELDS), ('amneziawg-go-v3', '3.1'): set(AWG_FIELDS)}


def snake(field: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', field).lower() if field in V3_FIELDS else field.lower()


def conf_value(field: str, value) -> str:
    # awg-tools and AmneziaVPN read toggles as on/off (AmneziaVPN treats anything but "off" as enabled).
    if field in V3_BOOLS:
        return 'on' if str(value).lower() == 'true' else 'off'
    return str(value)


def option_value(field: str, value):
    return str(value).lower() == 'true' if field in V3_BOOLS else value
URI_SCHEMES = frozenset({'vless', 'vmess', 'trojan', 'ss', 'ssr', 'hysteria', 'hysteria2',
                          'hy2', 'tuic', 'socks', 'socks5', 'http', 'https', 'wireguard', 'wg', 'amneziawg', 'awg'})


class Unsupported(ValueError):
    pass


def validate_peer(peer: SubscriptionPeer) -> None:
    allowed = PROTOCOLS.get((peer.protocol.adapter_id, peer.protocol.version))
    if allowed is None:
        raise Unsupported('unverified protocol')
    if peer.protocol.required_features or set(peer.protocol.parameters) - allowed:
        raise Unsupported('unverified parameters or features')
    for value in (peer.client_private_key.get_secret_value(), peer.server_public_key):
        if len(base64.b64decode(value, validate=True)) != 32:
            raise Unsupported('invalid key')
    if any(ord(c) < 32 or ord(c) == 127 for c in peer.name) or len(peer.name) > 256:
        raise Unsupported('invalid display name')
    if ipaddress.ip_address(peer.ipv4_address).version != 4:
        raise Unsupported('invalid IPv4')
    if peer.ipv6_address and ipaddress.ip_address(peer.ipv6_address).version != 6:
        raise Unsupported('invalid IPv6')
    for address in peer.dns_servers:
        ipaddress.ip_address(address)
    if not peer.allowed_ips or not 576 <= peer.mtu <= 9000:
        raise Unsupported('invalid network settings')
    for network in peer.allowed_ips:
        ipaddress.ip_network(network, strict=True)
    for name, value in peer.protocol.parameters.items():
        if name == 'HeaderProtectionKey':
            try:
                if len(base64.b64decode(str(value), validate=True)) != 32:
                    raise ValueError
            except ValueError:
                raise Unsupported('invalid header protection key') from None
        elif name in V3_RANGES:
            if not re.fullmatch(r'\d{1,10}(?:-\d{1,10})?', str(value)):
                raise Unsupported('invalid AWG 3.1 range')
        elif name in V3_BOOLS:
            if str(value).lower() not in ('true', 'false'):
                raise Unsupported('invalid AWG 3.1 toggle')
        elif name.startswith('I'):
            if not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
                raise Unsupported('invalid signature')
        elif name.startswith('H'):
            if not re.fullmatch(r'\d+(?:-\d+)?', str(value)):
                raise Unsupported('invalid header')
            bounds = [int(v) for v in str(value).split('-')]
            if max(bounds) > 4294967295 or bounds[-1] < bounds[0]:
                raise Unsupported('invalid header bounds')
        elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
            raise Unsupported('invalid numeric option')


def endpoint(peer: SubscriptionPeer) -> str:
    host = peer.endpoint.host
    return f'[{host}]:{peer.endpoint.port}' if ':' in host else f'{host}:{peer.endpoint.port}'


def addresses(peer: SubscriptionPeer) -> list[str]:
    return [peer.ipv4_address + '/32'] + ([peer.ipv6_address + '/128'] if peer.ipv6_address else [])


class EntryRenderer(Protocol):
    def entry(self, peer: SubscriptionPeer) -> str: ...


class RawAWGRenderer:
    def entry(self, peer: SubscriptionPeer) -> str:
        validate_peer(peer)
        lines = ['[Interface]', f'PrivateKey = {peer.client_private_key.get_secret_value()}',
                 'Address = ' + ', '.join(addresses(peer)), f'MTU = {peer.mtu}']
        if peer.dns_servers:
            lines.append('DNS = ' + ', '.join(peer.dns_servers))
        for field in AWG_FIELDS:
            if field in peer.protocol.parameters:
                lines.append(f'{field} = {conf_value(field, peer.protocol.parameters[field])}')
        lines += ['', '[Peer]', f'PublicKey = {peer.server_public_key}', f'Endpoint = {endpoint(peer)}',
                  'AllowedIPs = ' + ', '.join(peer.allowed_ips), 'PersistentKeepalive = 25']
        return '\n'.join(lines) + '\n'


class IncyRenderer:
    """INCY subscription line, also the canonical `amneziawg://` form for other URI-list clients."""
    def entry(self, peer: SubscriptionPeer) -> str:
        payload = base64.urlsafe_b64encode(RawAWGRenderer().entry(peer).encode()).decode().rstrip('=')
        return 'amneziawg://' + payload + '#' + quote(peer.name, safe='')


class AmneziaVpnRenderer:
    """AmneziaVPN `vpn://` key: base64url(qCompress(JSON)). The JSON mirrors what the client itself
    builds when importing an AWG .conf (ImportController::extractWireGuardConfig), plus a display name."""
    def entry(self, peer: SubscriptionPeer) -> str:
        config = RawAWGRenderer().entry(peer)
        last = {'config': config, 'hostName': peer.endpoint.host, 'port': peer.endpoint.port,
                'client_priv_key': peer.client_private_key.get_secret_value(), 'client_ip': ', '.join(addresses(peer)),
                'server_pub_key': peer.server_public_key, 'mtu': str(peer.mtu), 'persistent_keep_alive': '25',
                'allowed_ips': list(peer.allowed_ips)}
        last.update({field: conf_value(field, peer.protocol.parameters[field]) for field in AWG_FIELDS if field in peer.protocol.parameters})
        awg = {'last_config': json.dumps(last, ensure_ascii=False, indent=4), 'isThirdPartyConfig': True,
               'port': str(peer.endpoint.port), 'transport_proto': 'udp'}
        document = {'containers': [{'container': 'amnezia-awg', 'awg': awg}], 'defaultContainer': 'amnezia-awg',
                    'description': peer.name, 'hostName': peer.endpoint.host}
        if len(peer.dns_servers) >= 2:
            document.update(dns1=peer.dns_servers[0], dns2=peer.dns_servers[1])
        data = json.dumps(document, ensure_ascii=False, indent=4).encode()
        packed = struct.pack('>I', len(data)) + zlib.compress(data, 8)  # Qt qCompress framing
        return 'vpn://' + base64.urlsafe_b64encode(packed).decode().rstrip('=')


class ThroneRenderer:
    def entry(self, peer: SubscriptionPeer) -> str:
        validate_peer(peer)
        # Throne (1.1.5 … 1.3.1) ignores AllowedIPs on import and hardcodes full routes in Build().
        if set(peer.allowed_ips) not in ({'0.0.0.0/0'}, {'0.0.0.0/0', '::/0'}):
            raise Unsupported('Throne cannot preserve split routing')
        query = {'private_key': peer.client_private_key.get_secret_value(), 'public_key': peer.server_public_key,
                 'local_address': '-'.join(addresses(peer)), 'mtu': str(peer.mtu),
                 'persistent_keepalive_interval': '25', 'enable_amnezia': 'true'}
        query.update({snake(field): str(peer.protocol.parameters[field]).lower() if field in V3_BOOLS else str(peer.protocol.parameters[field])
                      for field in AWG_FIELDS if field in peer.protocol.parameters})
        return 'wg://' + endpoint(peer) + '?' + urlencode(query, quote_via=quote) + '#' + quote(peer.name, safe='')


# Remnawave's stand-ins for "no hosts" (→ No hosts found, → Check Hosts tab …): zero UUID at 0.0.0.0:1.
# They are dropped only when real AWG entries are added; responses that stay stock keep them.
PLACEHOLDER_URI = re.compile(r'^[a-z0-9]+://[^@\s]*@0\.0\.0\.0:1(?:[/?#]|$)')


def is_placeholder(line: str) -> bool:
    return PLACEHOLDER_URI.match(line) is not None


def append_lines(stock: bytes, entries: list[str]) -> bytes:
    text = stock.decode('utf-8')
    lines = text.splitlines()
    if not lines or any(line and (re.match(r'^([a-z0-9]+)://\S+$', line) is None
                                  or line.split(':', 1)[0] not in URI_SCHEMES) for line in lines):
        raise Unsupported('not URI subscription')
    newline = '\r\n' if '\r\n' in text else '\n'
    if '\r' in text.replace('\r\n', ''):
        raise Unsupported('ambiguous line endings')
    if entries and any(is_placeholder(line) for line in lines):
        kept = [line for line in lines if not is_placeholder(line)]
        return newline.join(kept + entries).encode() + newline.encode()
    separator = '' if text.endswith(('\n', '\r')) else newline
    return (text + separator + newline.join(entries) + newline).encode()


class Base64Renderer:
    def render(self, stock: bytes, entries: list[str]) -> bytes:
        encoded = stock.strip()
        # Strict decode avoids transforming arbitrary HTML/error bodies.
        decoded = base64.b64decode(encoded, validate=True)
        return base64.b64encode(append_lines(decoded, entries))


class StrictLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        self.nodes += 1
        self.depth += 1
        try:
            if self.check_event(AliasEvent) or self.depth > 40 or self.nodes > 30000:
                raise Unsupported('YAML aliases or complexity exceed safe subset')
            node = super().compose_node(parent, index)
            if node.tag == 'tag:yaml.org,2002:timestamp':
                raise Unsupported('ambiguous YAML timestamp')
            if node.tag == 'tag:yaml.org,2002:bool' and node.value.lower() not in ('true', 'false'):
                raise Unsupported('ambiguous YAML boolean')
            return node
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            if key.tag != 'tag:yaml.org,2002:str' or key.value in keys:
                raise Unsupported('duplicate or non-string YAML key')
            keys.add(key.value)
        return super().construct_mapping(node, deep)


class MihomoRenderer:
    def render(self, stock: bytes, peers: list[SubscriptionPeer]) -> bytes:
        parsed = yaml.load(stock.decode('utf-8'), Loader=StrictLoader)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('proxies'), list):
            raise Unsupported('not Mihomo subscription')
        existing = parsed['proxies']
        if any(not isinstance(p, dict) or not isinstance(p.get('name'), str) for p in existing):
            raise Unsupported('invalid stock proxies')
        groups = parsed.get('proxy-groups', [])
        if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
            raise Unsupported('invalid groups')
        names = {p['name'] for p in existing} | {g.get('name') for g in groups}
        added = []
        for peer in peers:
            validate_peer(peer)
            if peer.name in names:
                continue
            # Mihomo (v1.19.31 adapter/outbound/wireguard.go) runs its AWG 3 device only for version == 3;
            # 3.x options use kebab-case names and real booleans.
            options = {snake(field).replace('_', '-'): option_value(field, peer.protocol.parameters[field])
                       for field in AWG_FIELDS if field in peer.protocol.parameters}
            options = {'version': 3 if peer.protocol.version == '3.1' else 2, **options}
            proxy = {'name': peer.name, 'type': 'wireguard', 'ip': peer.ipv4_address + '/32',
                     'private-key': peer.client_private_key.get_secret_value(), 'udp': True, 'mtu': peer.mtu,
                     'persistent-keepalive': 25,
                     'peers': [{'server': peer.endpoint.host, 'port': peer.endpoint.port,
                                'public-key': peer.server_public_key, 'allowed-ips': peer.allowed_ips}],
                     'amnezia-wg-option': options}
            if peer.ipv6_address:
                proxy['ipv6'] = peer.ipv6_address + '/128'
            if peer.dns_servers:
                proxy.update({'remote-dns-resolve': True, 'dns': peer.dns_servers})
            existing.append(proxy)
            names.add(peer.name)
            added.append(peer.name)
        if not added:
            return stock
        placeholders = {p['name'] for p in existing if p.get('server') == '0.0.0.0' and p.get('port') == 1}
        rules = parsed.get('rules') if isinstance(parsed.get('rules'), list) else []
        # Keep placeholders if a rule targets one directly (not a same-named group, as in the stock
        # template where group "→ Remnawave" and a placeholder share a name): removal would break it.
        targets = placeholders - {g.get('name') for g in groups}
        if placeholders and not any(isinstance(r, str) and r.rsplit(',', 1)[-1].strip() in targets for r in rules):
            existing[:] = [p for p in existing if p['name'] not in placeholders]
            for group in groups:
                if isinstance(group.get('proxies'), list):
                    group['proxies'] = [name for name in group['proxies'] if name not in placeholders]
        for group in groups:
            if isinstance(group.get('proxies'), list) and (
                    group.get('type') in ('select', 'url-test', 'fallback', 'load-balance') or not group['proxies']):
                group['proxies'].extend(added)
        return yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False, width=4096).encode()


LINE_RENDERERS = {'throne': ThroneRenderer, 'incy': IncyRenderer}


def accepts(stock: bytes, capability: ClientCapability | None) -> bool:
    """Cheap pre-check: can this client's stock body take AWG at all? Avoids fetching private material
    for bodies that would be returned unchanged (sing-box JSON, legacy Clash YAML, HTML, errors)."""
    if capability is None or capability.renderer is None or len(stock) > 2 * 1024 * 1024:
        return False
    try:
        if capability.renderer == 'mihomo':
            return b'proxies' in stock and not stock.lstrip().startswith((b'{', b'['))
        try:
            append_lines(stock, ['probe://x'])
            return True
        except (ValueError, UnicodeError):
            append_lines(base64.b64decode(stock.strip(), validate=True), ['probe://x'])
            return True
    except (ValueError, UnicodeError):
        return False


def enrich(stock: bytes, content_type: str, capability: ClientCapability | None,
           peers: list[SubscriptionPeer]) -> bytes:
    """Add AWG in the client's own format; any doubt returns the stock bytes unchanged."""
    if capability is None or capability.renderer is None or len(stock) > 2 * 1024 * 1024:
        return stock
    compatible = [p for p in peers if capability.supports(p.protocol)]
    if not compatible:
        return stock
    try:
        if capability.renderer == 'mihomo':
            return MihomoRenderer().render(stock, compatible)
        renderer = LINE_RENDERERS[capability.renderer]()
        entries = []
        for peer in compatible:
            try:
                entries.append(renderer.entry(peer))
            except Unsupported:
                continue  # e.g. Throne cannot keep split routes: skip only that entry
        if not entries:
            return stock
        try:
            return append_lines(stock, entries)
        except (ValueError, UnicodeError):
            return Base64Renderer().render(stock, entries)
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, RecursionError):
        return stock


def info_links(peer: SubscriptionPeer) -> list[str]:
    """Links shown on the subscription page: INCY/`amneziawg://` (AWG 2 only) and the AmneziaVPN key."""
    validate_peer(peer)
    links = [IncyRenderer().entry(peer)] if FAMILIES['incy'].supports(peer.protocol) else []
    if FAMILIES['amneziavpn'].supports(peer.protocol):
        # The page names a link by its #fragment ("Unknown" otherwise). AmneziaVPN's decoder skips it:
        # verified with Qt 6.11 QByteArray::fromBase64(Base64Url|OmitTrailingEquals) + qUncompress.
        links.append(AmneziaVpnRenderer().entry(peer) + '#' + quote(peer.name, safe=''))
    return links


def enrich_info(stock: bytes, peers: list[SubscriptionPeer]) -> bytes:
    """`/api/sub/<id>/info` (subscription page data): append AWG links to `response.links`."""
    try:
        document = json.loads(stock)
        target = document.get('response', document) if isinstance(document, dict) else None
        if not isinstance(target, dict) or not isinstance(target.get('links'), list):
            return stock
        added = []
        for peer in peers:
            try:
                added.extend(info_links(peer))
            except (Unsupported, ValueError):
                continue
        if not added:
            return stock
        target['links'] = [link for link in target['links'] if not (isinstance(link, str) and is_placeholder(link))] + added
        return json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return stock
