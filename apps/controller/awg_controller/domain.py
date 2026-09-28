import base64
from cryptography.exceptions import InvalidTag
import ipaddress
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption


class KeyVault:
    """AES-GCM keyring: blobs are `v1:<key_id>:<b64(nonce||ciphertext)>`, AAD binds key id and owner."""
    def __init__(self, key: bytes, key_id: str = 'default', previous_keys: dict[str, bytes] | None = None):
        keys = {**(previous_keys or {}), key_id: key}
        for identifier, value in keys.items():
            if len(value) != 32:
                raise ValueError('encryption keys must decode to 32 bytes')
            if not identifier.isascii() or not identifier.replace('-', '').replace('_', '').isalnum():
                raise ValueError('invalid encryption key id')
        self.key_id = key_id
        self.keys = {identifier: AESGCM(value) for identifier, value in keys.items()}

    def encrypt(self, owner: str, private: str) -> str:
        nonce = os.urandom(12)
        encrypted = self.keys[self.key_id].encrypt(nonce, private.encode(), f'{self.key_id}:{owner}'.encode())
        return f'v1:{self.key_id}:' + base64.b64encode(nonce+encrypted).decode()

    def generate(self, owner: str):
        key = X25519PrivateKey.generate()
        private = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
        public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        return self.encrypt(owner, private), public

    def needs_rotation(self, encrypted: str) -> bool:
        return not encrypted.startswith(f'v1:{self.key_id}:')

    def decrypt(self, owner: str, encrypted: str):
        if not encrypted.startswith('v1:'):
            # Pre-keyring blobs carry no key id and owner-only AAD: try every configured key.
            data = base64.b64decode(encrypted, validate=True)
            for aead in self.keys.values():
                try:
                    return aead.decrypt(data[:12], data[12:], owner.encode()).decode()
                except InvalidTag:
                    continue
            raise ValueError('no configured key decrypts legacy blob')
        _, key_id, payload = encrypted.split(':', 2)
        if key_id not in self.keys:
            raise ValueError('unknown encryption key id')
        data = base64.b64decode(payload, validate=True)
        return self.keys[key_id].decrypt(data[:12], data[12:], f'{key_id}:{owner}'.encode()).decode()


def eligible(user, squad_ids, user_ids, awg_usage, policy):
    if user.status != 'ACTIVE' or user.expire_at <= datetime.now(timezone.utc):
        return False
    if user.user_uuid not in user_ids and not set(user.squad_ids).intersection(squad_ids):
        return False
    usage = awg_usage + (user.traffic_used_bytes if policy == 'combined' else 0)
    return policy == 'observe_only' or not user.traffic_limit_bytes or usage < user.traffic_limit_bytes


def counter_delta(previous: int, current: int, new_epoch: bool):
    if min(previous, current) < 0:
        raise ValueError('negative counter')
    return current if new_epoch or current < previous else current-previous


def free_address(pool: str, occupied: set[str], server: str):
    network = ipaddress.ip_network(pool, strict=True)
    reserved = ipaddress.ip_interface(server).ip
    # Work proportional to occupied addresses, even for a /64.
    for address in network.hosts():
        if address != reserved and str(address) not in occupied:
            return str(address)
    raise ValueError('IP pool exhausted')
