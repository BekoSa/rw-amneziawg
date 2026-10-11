"""Independent TT adapters. Native bytes belong to the pinned upstream exporter."""
from __future__ import annotations

import json

import yaml

from awg_contracts.trusttunnel import TrustTunnelSubscriptionEntry
from awg_capabilities import trusttunnel_supported
from . import StrictLoader, Unsupported


def mihomo_proxy(entry: TrustTunnelSubscriptionEntry) -> dict:
    if any(ord(c) < 32 or ord(c) == 127 for c in entry.name):
        raise Unsupported('invalid display name')
    return {'name': entry.name, 'type': 'trusttunnel', 'server': entry.endpoint.host,
            'port': entry.endpoint.port, 'username': entry.username,
            'password': entry.password.get_secret_value(), 'sni': entry.tls_server_name,
            'quic': False, 'udp': True, 'health-check': True}


def enrich(stock: bytes, entries: list[TrustTunnelSubscriptionEntry], user_agent: str) -> bytes:
    if not entries or not trusttunnel_supported(user_agent):
        return stock
    try:
        parsed = yaml.load(stock.decode('utf-8'), Loader=StrictLoader)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('proxies'), list):
            return stock
        proxies = parsed['proxies']
        groups = parsed.get('proxy-groups', [])
        if (any(not isinstance(p, dict) or not isinstance(p.get('name'), str) for p in proxies)
                or not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups)):
            return stock
        names = {p['name'] for p in proxies} | {g.get('name') for g in groups}
        added = []
        for entry in entries:
            if entry.name not in names:
                proxies.append(mihomo_proxy(entry))
                names.add(entry.name)
                added.append(entry.name)
        if not added:
            return stock
        for group in groups:
            if isinstance(group.get('proxies'), list) and (
                    group.get('type') in ('select', 'url-test', 'fallback', 'load-balance') or not group['proxies']):
                group['proxies'].extend(added)
        return yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False, width=4096).encode()
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, RecursionError):
        return stock


def native(entry: TrustTunnelSubscriptionEntry, format: str) -> bytes:
    """Return upstream output verbatim; no TLV or TOML serialization in gateway."""
    secret = entry.native_deeplink if format == 'deeplink' else entry.native_toml if format == 'toml' else None
    if secret is None or not secret.get_secret_value():
        raise Unsupported('native export unavailable')
    content = secret.get_secret_value()
    if format == 'deeplink' and (not content.startswith('tt://?') or any(c in content for c in '\r\n\x00')):
        raise Unsupported('invalid upstream deeplink')
    return content.encode('utf-8')


def enrich_info(stock: bytes, entries: list[TrustTunnelSubscriptionEntry], download_url: str) -> bytes:
    try:
        document = json.loads(stock)
        target = document.get('response', document) if isinstance(document, dict) else None
        if (not isinstance(target, dict) or not isinstance(target.get('links'), list)
                or 'trusttunnel' in target):
            return stock
        links, downloads = [], []
        for index, entry in enumerate(entries):
            if entry.native_deeplink is not None:
                links.append(native(entry, 'deeplink').decode('utf-8'))
            if entry.native_toml is not None:
                downloads.append({'name': entry.name, 'format': 'toml',
                                  'url': f'{download_url}?format=toml&i={index}'})
        if not links and not downloads:
            return stock
        target['links'].extend(links)
        if downloads:
            target['trusttunnel'] = {'downloads': downloads}
        return json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return stock
