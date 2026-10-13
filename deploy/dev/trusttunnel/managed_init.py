"""Isolated lab provisioning. Never used by production installer."""
import json
import os
from pathlib import Path
os.umask(0o077)
root = Path('/control')
root.mkdir(exist_ok=True, mode=0o700)
root.chmod(0o700)
envelope = {'envelope_id': '00000000-0000-4000-8000-000000000002',
    'node_id': '00000000-0000-4000-8000-000000000001', 'backend_ipv4': '10.242.21.3',
    'bind_addresses': ['0.0.0.0'], 'reserved_ports': [8443, 2525],
    'max_bindings': 128, 'max_udp_flows': 32, 'max_tcp_connections': 128}
(root / 'envelope.json').write_text(json.dumps(envelope))
if not (root / 'ingress.secret').exists():
    (root / 'ingress.secret').write_text(os.urandom(32).hex())
