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
            if other.prefixlen == 0:
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
