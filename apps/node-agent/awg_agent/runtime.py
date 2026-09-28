"""Real userspace runtime. Every networking operation is container-only."""
from __future__ import annotations
import base64
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from awg_config import READABLE, compile_uapi, parameters


def _deny_networks():
    # Operator-supplied private ranges (management, metadata) that tunnel clients must never reach.
    return [ipaddress.ip_network(item.strip(), strict=True)
            for item in os.environ.get('AWG_EGRESS_DENY', '').split(',') if item.strip()]


def egress_rules(desired, deny=()):
    """Container-namespace nftables table owned by one deployment; never touches other tables."""
    name = UserspaceRuntime.interface(desired.deployment_id)
    network = desired.profile.network
    pools = [('ip', ipaddress.ip_network(network.ipv4_pool, strict=True))]
    if network.ipv6_pool:
        pools.append(('ip6', ipaddress.ip_network(network.ipv6_pool, strict=True)))
    # Tunnel clients: no client-to-client, no denied service networks, only their own source
    # addresses outbound; nothing new may be opened towards them from outside.
    forward, nat = [f'iifname "{name}" oifname "{name}" drop'], []
    for family, pool in pools:
        for blocked in deny:
            if blocked.version == pool.version:
                forward.append(f'iifname "{name}" {family} daddr {blocked} drop')
        forward.append(f'iifname "{name}" {family} saddr {pool} accept')
        forward.append(f'oifname "{name}" {family} daddr {pool} ct state established,related accept')
        nat.append(f'{family} saddr {pool} oifname != "{name}" masquerade')
    forward += [f'iifname "{name}" drop', f'oifname "{name}" drop']
    body = '\n'.join([
        f'add table inet {name}', f'delete table inet {name}', f'table inet {name} {{',
        # The Agent's own sockets (management API, anything else local) are closed to the tunnel.
        '  chain input {', '    type filter hook input priority filter; policy accept;',
        f'    iifname "{name}" ct state established,related accept',
        f'    iifname "{name}" meta l4proto {{ icmp, ipv6-icmp }} accept',
        f'    iifname "{name}" drop', '  }',
        '  chain forward {', '    type filter hook forward priority filter; policy accept;',
        *('    ' + rule for rule in forward), '  }',
        '  chain postrouting {', '    type nat hook postrouting priority srcnat; policy accept;',
        *('    ' + rule for rule in nat), '  }', '}'])
    return body + '\n'


class UserspaceRuntime:
    def __init__(self):
        if os.environ.get('AWG_ISOLATED_RUNTIME') != '1' or not Path('/.dockerenv').exists():
            raise RuntimeError('runtime requires an explicitly isolated Docker container')
        if not Path('/dev/net/tun').exists():
            raise RuntimeError('TUN unavailable; requires isolated runner, never create a host device')
        self.deny = _deny_networks()  # invalid AWG_EGRESS_DENY fails at startup, not on every apply
        self.processes = {}
        self.epochs = {}
        self.previous = {}
        self.addresses = {}
        self.peer_epochs = {}

    @staticmethod
    def interface(identifier):
        return 'awg' + identifier.hex[:12]

    def _uapi(self, identifier, request='get=1\n\n'):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(f'/run/amneziawg/{self.interface(identifier)}.sock')
            connection.sendall(request.encode())
            data = bytearray()
            while not data.endswith(b'\n\n'):
                chunk = connection.recv(65536)
                if not chunk or len(data) + len(chunk) > 16_000_000:
                    raise RuntimeError('invalid UAPI response')
                data.extend(chunk)
        text = data.decode('ascii')
        if text.splitlines()[-2] != 'errno=0':
            raise RuntimeError('UAPI operation rejected')
        return text

    def _read(self, identifier):
        device, peers = {}, {}
        target = device
        for line in self._uapi(identifier).splitlines():
            if '=' not in line: continue
            key, value = line.split('=', 1)
            if key == 'public_key':
                public = base64.b64encode(bytes.fromhex(value)).decode()
                target = peers.setdefault(public, {'allowed_ip': []})
            elif key == 'allowed_ip': target['allowed_ip'].append(value)
            else: target[key] = value
        return device, peers

    @staticmethod
    def _ip(*args):
        subprocess.run(['ip', *args], check=True, capture_output=True, timeout=10)

    def _ensure(self, identifier):
        key = str(identifier)
        process = self.processes.get(key)
        if process is not None and process.poll() is None: return
        socket_path = Path(f'/run/amneziawg/{self.interface(identifier)}.sock')
        # A surviving unknown process is never silently adopted: counters and identity would be ambiguous.
        # A socket nobody listens on is debris of a dead process (e.g. container restart) and is removed.
        if socket_path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(socket_path))
                except ConnectionRefusedError:
                    socket_path.unlink()
                else:
                    raise RuntimeError('unowned runtime socket')
        process = subprocess.Popen(['amneziawg-go', '-f', self.interface(identifier)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes[key] = process
        self.epochs[key] = uuid4()
        self.peer_epochs[key] = {}
        for _ in range(100):
            if process.poll() is not None: raise RuntimeError('userspace runtime exited')
            if socket_path.exists(): return
            time.sleep(0.05)
        self.stop(identifier)
        raise RuntimeError('userspace runtime startup timed out')

    def apply(self, desired, private_key):
        self._ensure(desired.deployment_id)
        _, peers = self._read(desired.deployment_id)
        epoch_map = self.peer_epochs[str(desired.deployment_id)]
        expected_keys = {p.public_key for p in desired.peers} if desired.profile.enabled else set()
        for public in set(epoch_map) - expected_keys:
            del epoch_map[public]
        for public in expected_keys - set(peers):
            epoch_map[public] = uuid4()
        self._uapi(desired.deployment_id, compile_uapi(desired, private_key, set(peers)))
        name = self.interface(desired.deployment_id)
        network = desired.profile.network
        addresses = {f'{network.server_ipv4}/{ipaddress.ip_network(network.ipv4_pool).prefixlen}'}
        if network.server_ipv6:
            addresses.add(f'{network.server_ipv6}/{ipaddress.ip_network(network.ipv6_pool).prefixlen}')
        previous = self.addresses.get(str(desired.deployment_id), set())
        for address in previous - addresses: self._ip('address', 'del', address, 'dev', name)
        for address in addresses: self._ip('address', 'replace', address, 'dev', name)
        self.addresses[str(desired.deployment_id)] = addresses
        # Filtering is in place before the interface can forward anything.
        subprocess.run(['nft', '-f', '-'], input=egress_rules(desired, self.deny).encode(),
                       check=True, capture_output=True, timeout=10)
        self._ip('link', 'set', 'dev', name, 'mtu', str(network.mtu), 'up')

    def healthy(self, desired, public_key):
        try:
            process = self.processes.get(str(desired.deployment_id))
            if process is None or process.poll() is not None: return False
            device, peers = self._read(desired.deployment_id)
            derived = X25519PrivateKey.from_private_bytes(bytes.fromhex(device['private_key'])).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            if base64.b64encode(derived).decode() != public_key: return False
            if int(device.get('listen_port', 0)) != desired.profile.endpoint.port: return False
            expected = desired.peers if desired.profile.enabled else []
            if set(peers) != {p.public_key for p in expected}: return False
            for peer in expected:
                ips = {peer.ipv4_address + '/32'}
                if peer.ipv6_address: ips.add(peer.ipv6_address + '/128')
                if set(peers[peer.public_key]['allowed_ip']) != ips: return False
            for key, value in parameters(desired).items():
                if key not in READABLE: continue  # AWG 3.1 settings are write-only on UAPI get
                if device.get(key, '' if key.startswith('i') else '0') != value: return False
            output = subprocess.run(['ip', '-j', 'address', 'show', 'dev', self.interface(desired.deployment_id)], check=True, capture_output=True, timeout=5)
            interface = json.loads(output.stdout)[0]
            actual_addresses = {item['local'] for item in interface['addr_info']}
            required = {desired.profile.network.server_ipv4}
            if desired.profile.network.server_ipv6: required.add(desired.profile.network.server_ipv6)
            return 'UP' in interface['flags'] and interface['mtu'] == desired.profile.network.mtu and required <= actual_addresses
        except Exception:
            return False

    def stop(self, identifier):
        key = str(identifier)
        process = self.processes.pop(key, None)
        if process:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.addresses.pop(key, None)
        self.previous.pop(key, None)
        # Absent table is fine: stop() is also used for never-started deployments.
        subprocess.run(['nft', 'delete', 'table', 'inet', self.interface(identifier)],
                       check=False, capture_output=True, timeout=10)

    def counters(self, desired):
        key = str(desired.deployment_id)
        _, peers = self._read(desired.deployment_id)
        counters = {public: (int(p.get('rx_bytes', 0)), int(p.get('tx_bytes', 0)), int(p.get('last_handshake_time_sec', 0)))
                    for public, p in peers.items()}
        previous = self.previous.get(key, {})
        for public, (rx, tx, _) in counters.items():
            if public not in self.peer_epochs[key] or (public in previous and (rx < previous[public][0] or tx < previous[public][1])):
                self.peer_epochs[key][public] = uuid4()
        self.previous[key] = counters
        return self.epochs[key], counters, self.peer_epochs[key].copy()
