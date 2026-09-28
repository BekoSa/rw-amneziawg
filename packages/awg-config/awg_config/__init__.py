"""Strict adapter for the pinned amneziawg-go v3.1 runtime: AWG 1.0, 2 and 3.1 profiles.

AWG 3.1 (amneziawg-go README, v3.1.20260828) adds server-side HeaderProtectionKey (needs S1-S4 >= 12)
and client-side ContentPaddingAddition, timings (uint32 ranges), RandomTrailers and DisableCookies.
"""
import base64
import re
import secrets
from awg_contracts import DesiredDeployment

ADAPTER_ID = 'amneziawg-go-v3'
VERSIONS = ['1.0', '2', '3.1']
FEATURES = ['i_fields', 'header_ranges', 's3_s4', 'header_protection', 'content_padding', 'timings',
            'random_trailers', 'disable_cookies']
# AWG 3.1-only parameters: config-file name -> UAPI key.
V3_RANGES = {'ContentPaddingAddition': 'content_padding_addition', 'RekeyAfterTime': 'rekey_after_time',
             'RekeyTimeout': 'rekey_timeout', 'RejectAfterTime': 'reject_after_time',
             'KeepaliveTimeout': 'keepalive_timeout', 'MaxHandshakeAttempts': 'max_handshake_attempts'}
V3_BOOLS = {'RandomTrailers': 'random_trailers', 'DisableCookies': 'disable_cookies'}
V3_FIELDS = {'HeaderProtectionKey', *V3_RANGES, *V3_BOOLS}
# Keys amneziawg-go reports back on UAPI get (the 3.1 additions are write-only there).
READABLE = {'jc', 'jmin', 'jmax', 's1', 's2', 's3', 's4', 'h1', 'h2', 'h3', 'h4', 'i1', 'i2', 'i3', 'i4', 'i5'}
_FIELDS = {'Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4', 'I1', 'I2', 'I3', 'I4', 'I5'}


def parameters(deployment: DesiredDeployment) -> dict[str, str]:
    protocol = deployment.profile.protocol
    if protocol.adapter_id != ADAPTER_ID or protocol.version not in VERSIONS:
        raise ValueError('unsupported protocol adapter/version')
    if set(protocol.required_features) - set(FEATURES):
        raise ValueError('unsupported protocol feature')
    source = protocol.parameters
    if set(source) - _FIELDS - (V3_FIELDS if protocol.version == '3.1' else set()):
        raise ValueError('unsupported protocol parameter')
    if protocol.version == '1.0':
        if set(source) & {'S3', 'S4', 'I1', 'I2', 'I3', 'I4', 'I5'}:
            raise ValueError('AWG 1.0 parameter not supported')
        if any('-' in str(source.get(f'H{i}', i)) for i in range(1, 5)):
            raise ValueError('AWG 1.0 range parameter not supported')
    values = {k: str(source.get(k, 0)) for k in ('Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4')}
    for key, value in values.items():
        maximum = 128 if key == 'Jc' else 65535
        if not re.fullmatch(r'[0-9]+', value) or not 0 <= int(value) <= maximum:
            raise ValueError('invalid numeric protocol parameter')
    if int(values['Jmin']) > int(values['Jmax']):
        raise ValueError('Jmin exceeds Jmax')
    ranges = []
    for index in range(1, 5):
        key = f'H{index}'
        value = str(source.get(key, index))
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?', value):
            raise ValueError('invalid header range parameter')
        ends = list(map(int, value.split('-')))
        lo, hi = ends[0], ends[-1]
        if not 1 <= lo <= hi <= 4294967295 or any(lo <= b and a <= hi for a, b in ranges):
            raise ValueError('overlapping or invalid header parameter')
        ranges.append((lo, hi))
        values[key] = value
    # Accept only the documented AWG 2 signature grammar; never arbitrary UAPI.
    token = re.compile(r'<(?:b 0x(?:[0-9a-fA-F]{2})+|r [0-9]+|rd [0-9]+|rc [0-9]+|t)>')
    for index in range(1, 6):
        key = f'I{index}'
        value = str(source.get(key, ''))
        if token.sub('', value) != '':
            raise ValueError('invalid signature parameter')
        total = 0
        for part in token.findall(value):
            if part.startswith('<b '): total += (len(part) - 6) // 2
            elif part == '<t>': total += 4
            else: total += int(part.rsplit(' ', 1)[1][:-1])
        if total > 1400:
            raise ValueError('signature parameter exceeds safe packet size')
        values[key] = value
    result = {key.lower(): value for key, value in values.items()}
    if protocol.version == '3.1':
        result.update(_v3(source, values))
    return result


def _v3(source, values) -> dict[str, str]:
    extra = {}
    if 'HeaderProtectionKey' in source:
        try:
            key = base64.b64decode(str(source['HeaderProtectionKey']), validate=True)
        except ValueError:
            key = b''
        if len(key) != 32:
            raise ValueError('HeaderProtectionKey must be a base64 32-byte key')
        if any(int(values[k]) < 12 for k in ('S1', 'S2', 'S3', 'S4')):
            raise ValueError('header protection requires S1-S4 >= 12')
        extra['header_protection_key'] = key.hex()
    for name, uapi in V3_RANGES.items():
        if name in source:
            value = str(source[name])
            if not re.fullmatch(r'[0-9]{1,10}(?:-[0-9]{1,10})?', value):
                raise ValueError('invalid AWG 3.1 range parameter')
            bounds = [int(v) for v in value.split('-')]
            if bounds[-1] < bounds[0] or bounds[-1] > 4294967295:
                raise ValueError('invalid AWG 3.1 range parameter')
            if name != 'ContentPaddingAddition' and bounds[0] < 1:
                raise ValueError('AWG 3.1 timings and handshake attempts must be at least 1')
            extra[uapi] = value
    for name, uapi in V3_BOOLS.items():
        if name in source:
            if str(source[name]).lower() not in ('true', 'false'):
                raise ValueError('AWG 3.1 toggles must be true or false')
            extra[uapi] = str(source[name]).lower()
    return extra


def compile_uapi(deployment: DesiredDeployment, private_key: bytes, existing_keys: set[str]) -> str:
    if len(private_key) != 32:
        raise ValueError('invalid local key')
    values = parameters(deployment)
    lines = ['set=1', f'private_key={private_key.hex()}', f'listen_port={deployment.profile.endpoint.port}']
    lines.extend(f'{key}={value}' for key, value in values.items())
    desired_keys = {p.public_key for p in deployment.peers} if deployment.profile.enabled else set()
    for public_key in sorted(existing_keys - desired_keys):
        lines.extend([f'public_key={base64.b64decode(public_key, validate=True).hex()}', 'remove=true'])
    for peer in deployment.peers if deployment.profile.enabled else []:
        lines.extend([f'public_key={base64.b64decode(peer.public_key, validate=True).hex()}', 'replace_allowed_ips=true',
                      f'allowed_ip={peer.ipv4_address}/32', f'persistent_keepalive_interval={peer.persistent_keepalive}'])
        if peer.ipv6_address:
            lines.append(f'allowed_ip={peer.ipv6_address}/128')
    return '\n'.join(lines) + '\n\n'


def random_parameters(version: str = '3.1') -> dict[str, int | str]:
    """Fresh AWG 2 obfuscation values that pass `parameters()`: disjoint header ranges and junk sizes
    kept small enough for the default MTU. S1 + 56 != S2 keeps init/response sizes distinguishable."""
    rng = secrets.SystemRandom()
    jmin = rng.randint(40, 80)
    s1 = rng.randint(15, 150)
    s2 = rng.choice([v for v in range(15, 151) if s1 + 56 != v])
    # Four disjoint H ranges inside 5..2^31, each ~1-16 million values wide.
    starts = sorted(rng.sample(range(5, 2 ** 31 - 2 ** 24, 2 ** 24), 4))
    ranges = [f'{start}-{start + rng.randint(2 ** 20, 2 ** 24 - 1)}' for start in starts]
    rng.shuffle(ranges)
    values = {'Jc': rng.randint(3, 6), 'Jmin': jmin, 'Jmax': jmin + rng.randint(20, 100), 'S1': s1, 'S2': s2,
              'S3': rng.randint(20, 64), 'S4': rng.randint(12, 32) if version == '3.1' else rng.randint(0, 16),
              'H1': ranges[0], 'H2': ranges[1], 'H3': ranges[2], 'H4': ranges[3]}
    if version == '3.1':
        # Server-side shared key; S1-S4 above are all >= 12 as header protection requires.
        values['HeaderProtectionKey'] = base64.b64encode(secrets.token_bytes(32)).decode()
    return values
