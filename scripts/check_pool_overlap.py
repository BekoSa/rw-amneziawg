"""Preflight Docker subnets against existing host LAN/VPN routes; container only."""
import ipaddress
import json
from pathlib import Path
import sys


def conflicts(config, routes):
    errors = []
    pools = []
    for name, network in config.get('networks', {}).items():
        for item in network.get('ipam', {}).get('config', []):
            pools.append((name, ipaddress.ip_network(item['subnet'])))
    for index, (name, pool) in enumerate(pools):
        for other_name, other in pools[index + 1:]:
            if pool.version == other.version and pool.overlaps(other):
                errors.append(f'{name} overlaps {other_name}')
        for route in routes:
            dev = route.get('dev', '')
            if dev == 'docker0' or dev.startswith(('br-', 'veth')):
                continue  # Existing Docker network ownership checked by Compose/Engine.
            dst = route.get('dst', 'default')
            if dst == 'default':
                continue
            other = ipaddress.ip_network(dst, strict=False)
            # A full-tunnel VPN often splits its default route into /1../7 pieces (0.0.0.0/2, 64.0.0.0/3 …).
            # Those are defaults, not networks: a Docker bridge's connected /24 is more specific and wins.
            # Anything /8 or narrower is a real LAN/VPN network and still blocks the lab.
            if other.prefixlen < (8 if other.version == 4 else 16):
                continue
            if other.version == pool.version and pool.overlaps(other):
                errors.append(f'{name} subnet {pool} overlaps host {dev} route {other}')
    return errors


if __name__ == '__main__':
    config = json.load(sys.stdin)
    snapshot = Path(sys.argv[1])
    routes = json.loads((snapshot / 'routes4.json').read_text()) + json.loads((snapshot / 'routes6.json').read_text())
    errors = conflicts(config, routes)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Docker subnet / host VPN and LAN overlap preflight: PASS')
