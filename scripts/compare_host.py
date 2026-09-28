"""Read snapshots made on host, compare inside tooling; never inspects container routes."""
import json
from pathlib import Path
import sys


def normalized(directory):
    def read(name):
        return json.loads((Path(directory) / f'{name}.json').read_text())
    def docker_interface(name):
        return name == 'docker0' or name.startswith(('br-', 'veth'))
    result = {}
    for family in ('4', '6'):
        # Inspect every non-Docker route, including VPN policy tables, not only defaults.
        result[f'routes{family}'] = [{k: v for k, v in route.items() if k not in {'expires', 'cache'}} for route in read(f'routes{family}') if not docker_interface(route.get('dev', ''))]
        result[f'rules{family}'] = read(f'rules{family}')
    result['links'] = [{k: link.get(k) for k in ('ifname', 'mtu', 'flags', 'operstate', 'link_type', 'linkinfo')} for link in read('links') if not docker_interface(link['ifname'])]
    result['addresses'] = [{'ifname': link['ifname'], 'addresses': [{k: a.get(k) for k in ('family', 'local', 'prefixlen', 'scope')} for a in link.get('addr_info', [])]} for link in read('addresses') if not docker_interface(link['ifname'])]
    return {key: sorted(value, key=lambda item: json.dumps(item, sort_keys=True)) for key, value in result.items()}


if __name__ == '__main__':
    before, after = map(normalized, sys.argv[1:3])
    changed = [key for key in before if before[key] != after[key]]
    if changed:
        raise SystemExit('HOST INTEGRITY FAILED: ' + ', '.join(changed))
    print('Host VPN/interfaces/routes/policy rules unchanged: PASS')
    statuses = [Path(path) / 'firewall-status.txt' for path in sys.argv[1:3]]
    if any(not path.exists() or path.read_text().strip() != 'nft' for path in statuses):
        print('Firewall verification: UNAVAILABLE (read permissions/tool); not certified')
