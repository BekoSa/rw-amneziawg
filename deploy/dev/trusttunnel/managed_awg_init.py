"""Disposable managed AWG CI configuration; no host-network side effects."""
import json
import os
from pathlib import Path
os.umask(0o077)
root = Path('/control')
root.mkdir(exist_ok=True, mode=0o700)
(root / 'envelope.json').write_text(json.dumps({
    'envelope_id': '00000000-0000-4000-8000-000000000003',
    'node_id': '00000000-0000-4000-8000-000000000001',
    'backend_ipv4': '10.240.7.3', 'bind_addresses': ['0.0.0.0'],
    'reserved_ports': [8443], 'max_bindings': 128,
    'max_udp_flows': 4096, 'max_tcp_connections': 4096}))
if not (root / 'ingress.secret').exists():
    (root / 'ingress.secret').write_text(os.urandom(32).hex())
