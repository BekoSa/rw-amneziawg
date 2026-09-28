"""Client family registry. Format is never a capability claim: each family has its own renderer.

Families and evidence (checked 2026-09):
- mihomo: Mihomo core and apps built on it; `type: wireguard` + `amnezia-wg-option`, `version: 3` for AWG 3.1
  (https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/wireguard.go).
- throne: `wg://` link with `enable_amnezia` and AWG 2/3.1 query fields
  (https://github.com/throneproj/Throne/blob/1.3.1/src/configs/outbounds/wireguard.cpp).
- amneziavpn: `vpn://` key, AWG 2 and 3.1 (amnezia-client 5.0.3.0).
- incy: `amneziawg://<base64url .conf>#name` subscription lines, AWG 2 only (3.x not documented)
  (https://incy.gitbook.io/docs/docs-en/subscription-format).
- happ: documented protocols are VLESS/VMess/Shadowsocks/Socks5/Trojan/Hysteria2 only
  (https://www.happ.su/main/dev-docs/examples-of-links-and-parameters) - no AWG entry is added.
- generic: any other URI-list client gets the canonical `amneziawg://` line; unknown schemes are skipped
  by URI-list clients. YAML/JSON formats of other clients are never rewritten.
AmneziaVPN does not fetch subscriptions; it receives a `vpn://` key via the subscription page (/info).
"""
from dataclasses import dataclass
import re

from awg_contracts import ProtocolSpec

AWG2 = frozenset({('amneziawg-go-v3', '2')})
AWG31 = frozenset({('amneziawg-go-v3', '2'), ('amneziawg-go-v3', '3.1')})


@dataclass(frozen=True)
class ClientCapability:
    family: str
    renderer: str | None
    protocols: frozenset[tuple[str, str]]
    source: str

    def supports(self, protocol: ProtocolSpec) -> bool:
        return (self.renderer is not None and (protocol.adapter_id, protocol.version) in self.protocols
                and not protocol.required_features)


FAMILIES = {
    'mihomo': ClientCapability('mihomo', 'mihomo', AWG31, 'MetaCubeX/mihomo v1.19.31 adapter/outbound/wireguard.go'),
    'throne': ClientCapability('throne', 'throne', AWG31, 'throneproj/Throne 1.3.1 src/configs/outbounds/wireguard.cpp'),
    # INCY documents AWG 2 fields only (Jc..I5); AWG 3.1 support is not confirmed.
    'incy': ClientCapability('incy', 'incy', AWG2, 'incy.gitbook.io subscription-format'),
    # AmneziaVPN >= 5.0.1.5 imports AWG 3.1 (awgProtocolKeys in 5.0.3.0); used for subscription-page keys.
    'amneziavpn': ClientCapability('amneziavpn', 'amneziavpn', AWG31, 'amnezia-client 5.0.3.0 importController.cpp'),
    'happ': ClientCapability('happ', None, frozenset(), 'happ.su dev-docs: no WireGuard/AmneziaWG'),
    'generic': ClientCapability('generic', 'incy', AWG2, 'amneziawg:// canonical line; ignored by clients without AWG'),
}

# Apps that embed the Mihomo core. Plain `Clash`, `ClashX`, `Stash` are legacy/other cores: never matched.
_MIHOMO = re.compile(r'mihomo|clash[.\- ]?meta|clashx[ ]meta|clash-verge|flclash|clash[- ]nyanpasu|koala[- ]clash', re.IGNORECASE)
_PREFIXED = [('throne', re.compile(r'throne/', re.IGNORECASE)), ('incy', re.compile(r'incy/', re.IGNORECASE)),
             ('happ', re.compile(r'happ/', re.IGNORECASE))]


def identify(user_agent: str) -> ClientCapability:
    agent = user_agent.strip()
    for family, pattern in _PREFIXED:
        if pattern.match(agent):
            return FAMILIES[family]
    if _MIHOMO.search(agent):
        return FAMILIES['mihomo']
    return FAMILIES['generic']
