from __future__ import annotations

import base64
import fcntl
import ipaddress
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
from awg_contracts import (ActualDeployment, AgentCapabilities, ApplyRequest, DeploymentState, DesiredDeployment,
                           PeerTraffic, ProtocolCapability, TrafficSnapshot, ValidationIssue,
                           ValidationResult, content_digest)
from awg_config import ADAPTER_ID, FEATURES, VERSIONS, compile_uapi


class AgentService:
    def __init__(self, directory: Path, node_id: UUID, runtime):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.lock_file = open(self.directory / 'agent.lock', 'a+')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError('another agent owns this state directory') from None
        self.lock = threading.RLock()
        self.node_id, self.runtime = node_id, runtime
        self.db = sqlite3.connect(self.directory / 'journal.sqlite3', check_same_thread=False)
        os.chmod(self.directory / 'journal.sqlite3', 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS deployments (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock_file.close()

    def capabilities(self):
        # Reported to the Controller/TUI so operators see which AmneziaWG core a node runs.
        version = '0.1.0 (' + os.environ.get('AWG_RUNTIME_VERSION', 'amneziawg runtime unknown') + ')'
        return AgentCapabilities(node_id=self.node_id, agent_version=version, runtime='userspace',
                                 protocols=[ProtocolCapability(adapter_id=ADAPTER_ID, versions=VERSIONS, features=FEATURES)],
                                 supports_ipv6=True, supports_atomic_apply=False)

    def _load(self, identifier):
        row = self.db.execute('SELECT payload FROM deployments WHERE id=?', (str(identifier),)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, identifier, record):
        with self.db:
            self.db.execute('INSERT INTO deployments VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                            (str(identifier), json.dumps(record)))

    def _key(self, identifier):
        path = self.directory / f'{UUID(str(identifier)).hex}.key'
        if not path.exists():
            key = X25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(key)
                output.flush()
                os.fsync(output.fileno())
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            if os.fstat(source.fileno()).st_mode & 0o077:
                raise RuntimeError('insecure local server key permissions')
            key = source.read()
        if len(key) != 32:
            raise RuntimeError('invalid durable server key')
        public = X25519PrivateKey.from_private_bytes(key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return key, base64.b64encode(public).decode()

    def validate(self, desired):
        issues = []
        try:
            if desired.node_id != self.node_id: raise ValueError('wrong node identity')
            if len(desired.peers) > self.capabilities().max_peers: raise ValueError('peer limit exceeded')
            compile_uapi(desired, bytes(32), set())
            self._check_node_conflicts(desired)
        except ValueError as error:
            issues.append(ValidationIssue(code='INVALID_CONFIGURATION', message=str(error)))
        return ValidationResult(valid=not issues, revision=desired.revision, digest=content_digest(desired), issues=issues)

    def _check_node_conflicts(self, desired):
        """Deployments share one network namespace: pools and UDP ports must be disjoint."""
        network = desired.profile.network
        pools = [ipaddress.ip_network(network.ipv4_pool)] + ([ipaddress.ip_network(network.ipv6_pool)] if network.ipv6_pool else [])
        for _, payload in self.db.execute('SELECT id,payload FROM deployments WHERE id<>?', (str(desired.deployment_id),)):
            record = json.loads(payload)
            # Both what is running (possibly an LKG after rollback) and what is intended must stay disjoint.
            for key in ('effective', 'desired'):
                if not record.get(key):
                    continue
                other = DesiredDeployment.model_validate(record[key])
                if not other.profile.enabled:
                    continue
                if other.profile.endpoint.port == desired.profile.endpoint.port:
                    raise ValueError('listen port already used by another deployment on this node')
                theirs = [ipaddress.ip_network(other.profile.network.ipv4_pool)]
                if other.profile.network.ipv6_pool:
                    theirs.append(ipaddress.ip_network(other.profile.network.ipv6_pool))
                if any(a.version == b.version and a.overlaps(b) for a in pools for b in theirs):
                    raise ValueError('address pool overlaps another deployment on this node')

    def _actual(self, desired, record, *, state, healthy=False, applied=None, error=None):
        _, public = self._key(desired.deployment_id)
        return ActualDeployment(deployment_id=desired.deployment_id, desired_revision=desired.revision,
            validated_revision=desired.revision, applied_revision=applied,
            last_known_good_revision=(record.get('lkg') or {}).get('revision'),
            applied_digest=content_digest(desired) if applied else None, state=state, healthy=healthy,
            interface_name='awg' + desired.deployment_id.hex[:12], server_public_key=public,
            listen_port=desired.profile.endpoint.port, error=error)

    def apply(self, request: ApplyRequest):
        desired = request.desired
        with self.lock:
            validation = self.validate(desired)
            if not validation.valid: raise ValueError(validation.issues[0].message)
            record = self._load(desired.deployment_id)
            if record:
                previous = DesiredDeployment.model_validate(record['desired'])
                if desired.revision < previous.revision: raise ValueError('stale revision')
                if desired.revision == previous.revision:
                    if validation.digest != content_digest(previous): raise ValueError('revision digest conflict')
                    if record['phase'] == 'ready':
                        current = self.state(desired.deployment_id)
                        if current.state == DeploymentState.READY:
                            return current
                        # Identical retry repairs runtime drift (process death, interface loss).
                    # A failed or interrupted identical revision is retryable; the original CAS has already succeeded.
                elif request.expected_applied_revision != record['actual'].get('applied_revision'):
                    raise ValueError('applied revision compare-and-swap conflict')
            elif request.expected_applied_revision is not None:
                raise ValueError('applied revision compare-and-swap conflict')
            if record is None:
                record = {'sequence': 0, 'lkg': None}
            # Persist the authoritative authorization set before any mutation; recovery MUST honor it.
            record.update(desired=desired.model_dump(mode='json'), phase='applying')
            record['actual'] = self._actual(desired, record, state='APPLYING').model_dump(mode='json')
            self._save(desired.deployment_id, record)
            return self._execute(desired, record)

    def _execute(self, desired, record):
        private, public = self._key(desired.deployment_id)
        try:
            self.runtime.apply(desired, private)
            if not self.runtime.healthy(desired, public): raise RuntimeError('runtime verification failed')
            record.update(lkg=desired.model_dump(mode='json'), phase='ready', effective=desired.model_dump(mode='json'))
            actual = self._actual(desired, record, state='READY', healthy=True, applied=desired.revision)
        except Exception:
            # Do not expose runtime exception/config text: it may contain key material.
            actual = self._rollback(desired, record, private, public)
        record['actual'] = actual.model_dump(mode='json')
        self._save(desired.deployment_id, record)
        return actual

    def _rollback(self, desired, record, private, public):
        record['phase'] = 'rolling_back'
        self._save(desired.deployment_id, record)
        healthy = False
        try:
            if record.get('lkg'):
                fallback = DesiredDeployment.model_validate(record['lkg'])
                authorized = {(p.public_key, p.key_generation, p.ipv4_address, p.ipv6_address) for p in desired.peers}
                fallback.peers = [p for p in fallback.peers if (p.public_key, p.key_generation, p.ipv4_address, p.ipv6_address) in authorized
                                  and p.public_key not in desired.revoked_public_keys] if desired.profile.enabled else []
                self.runtime.apply(fallback, private)
                healthy = self.runtime.healthy(fallback, public)
                if not healthy: raise RuntimeError('rollback verification failed')
                record['effective'] = fallback.model_dump(mode='json')
            else:
                self.runtime.stop(desired.deployment_id)
                record.pop('effective', None)
        except Exception:
            # A failed removal must never leave an unverified partially authorized interface running.
            self.runtime.stop(desired.deployment_id)
            record.pop('effective', None)
        record['phase'] = 'degraded'
        return self._actual(desired, record, state='DEGRADED', healthy=healthy,
                            error=ValidationIssue(code='APPLY_FAILED', message='Runtime apply failed; reconciled to safe fallback'))

    def recover(self):
        with self.lock:
            records = list(self.db.execute('SELECT id,payload FROM deployments'))
            for identifier, payload in records:
                record = json.loads(payload)
                desired = DesiredDeployment.model_validate(record['desired'])
                if record['phase'] in ('applying', 'ready'):
                    self._execute(desired, record)
                else:
                    private, public = self._key(identifier)
                    record['actual'] = self._rollback(desired, record, private, public).model_dump(mode='json')
                    self._save(identifier, record)

    def state(self, identifier):
        with self.lock:
            record = self._load(identifier)
            if not record: raise KeyError('unknown deployment')
            actual = ActualDeployment.model_validate(record['actual'])
            effective = record.get('effective')
            healthy = bool(effective and self.runtime.healthy(DesiredDeployment.model_validate(effective), actual.server_public_key))
            data = actual.model_dump()
            data.update(healthy=healthy, observed_at=datetime.now(timezone.utc))
            if actual.state == 'READY' and not healthy: data['state'] = 'DEGRADED'
            return ActualDeployment.model_validate(data)

    def traffic(self, identifier):
        with self.lock:
            record = self._load(identifier)
            if not record or not record.get('effective'): raise KeyError('no effective deployment')
            desired = DesiredDeployment.model_validate(record['effective'])
            epoch, counters, peer_epochs = self.runtime.counters(desired)
            record['sequence'] += 1
            self._save(identifier, record)
            peers = []
            for peer in desired.peers:
                if peer.public_key not in counters: continue
                rx, tx, handshake = counters[peer.public_key]
                peers.append(PeerTraffic(peer_id=peer.peer_id, public_key=peer.public_key, key_generation=peer.key_generation, counter_epoch=peer_epochs[peer.public_key],
                    rx_bytes=str(rx), tx_bytes=str(tx), latest_handshake=datetime.fromtimestamp(handshake, timezone.utc) if handshake else None))
            return TrafficSnapshot(node_id=self.node_id, deployment_id=identifier, runtime_epoch=epoch, sequence=record['sequence'], peers=peers)
