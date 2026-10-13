"""TrustTunnel desired/actual lifecycle; independent from the AWG journal."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from awg_contracts import (TrustTunnelActual, TrustTunnelDesired, TrustTunnelExportResponse,
                          TrustTunnelValidationResult, ValidationIssue, trusttunnel_digest, wire_payload)


class TrustTunnelService:
    def __init__(self, directory, node_id, runtime, certificates):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.owner = open(self.directory / 'trusttunnel.lock', 'a+')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError('another agent owns TrustTunnel state') from None
        self.lock = threading.RLock()
        self.node_id, self.runtime, self.certificates = node_id, runtime, certificates
        self.certificates.publish_lock = self.lock
        self.db = sqlite3.connect(self.directory / 'trusttunnel.sqlite3', check_same_thread=False)
        (self.directory / 'trusttunnel.sqlite3').chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        # Accepted watermark survives fallback, process crash and filtered effective state.
        self.db.execute('CREATE TABLE IF NOT EXISTS accepted(id TEXT PRIMARY KEY,revision INTEGER,digest TEXT)')
        self.db.commit()
        for path in self.directory.glob('trusttunnel.sqlite3*'):
            path.chmod(0o600)
        self.stopping = threading.Event()
        self.worker = None

    def _load(self, identifier):
        row = self.db.execute('SELECT payload FROM deployments WHERE id=?', (str(identifier),)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, identifier, record):
        self.db.execute('INSERT INTO deployments VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                        (str(identifier), json.dumps(record)))

    def identifiers(self):
        with self.lock:
            return [UUID(row[0]) for row in self.db.execute('SELECT id FROM deployments')]

    def validate(self, desired):
        issues = []
        with self.lock:
            try:
                if desired.node_id != self.node_id:
                    raise ValueError('wrong node identity')
                if any(identifier != desired.deployment_id for identifier in self.identifiers()):
                    raise ValueError('only one TrustTunnel deployment is supported per node')
                resources = getattr(self, "resources", None)
                self.runtime.validate(desired)
                if resources is None: self.certificates.validate(desired.profile.tls)
            except ValueError as exc:
                issues.append(ValidationIssue(code='INVALID_CONFIGURATION', message=str(exc)))
            return TrustTunnelValidationResult(valid=not issues, revision=desired.revision,
                digest=trusttunnel_digest(desired), issues=issues)

    def apply(self, request):
        desired = request.desired
        with self.lock:
            resources = getattr(self, "resources", None)
            if resources is not None:
                resources.require_protocol(desired, "trusttunnel")
                if desired.profile.enabled: self.certificates.validate(desired.profile.tls)
            validation = self.validate(desired)
            if not validation.valid:
                raise ValueError(validation.issues[0].message)
            accepted = self.db.execute('SELECT revision,digest FROM accepted WHERE id=?',
                                       (str(desired.deployment_id),)).fetchone()
            record = self._load(desired.deployment_id)
            if accepted:
                if desired.revision < accepted[0]:
                    raise ValueError('stale revision')
                if desired.revision == accepted[0] and validation.digest != accepted[1]:
                    raise ValueError('revision digest conflict')
                if desired.revision == accepted[0] and record and record['phase'] == 'ready':
                    actual = self.state(desired.deployment_id)
                    if actual.healthy and actual.applied_revision == desired.revision:
                        return actual
            if not accepted or desired.revision > accepted[0]:
                applied = (record or {}).get('actual', {}).get('applied_revision')
                if request.expected_applied_revision != applied:
                    raise ValueError('applied revision compare-and-swap conflict')
            record = record or {'lkg': None}
            record.update(desired=wire_payload(desired), phase='applying')
            record['actual'] = self._actual(desired, record, state='APPLYING').model_dump(mode='json')
            with self.db:
                self.db.execute('INSERT INTO accepted VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'revision=excluded.revision,digest=excluded.digest',
                    (str(desired.deployment_id), desired.revision, validation.digest))
                self._save(desired.deployment_id, record)
            return self._execute(desired, record)

    def _actual(self, desired, record, *, state, applied=None, healthy=False, error=None, effective=None):
        metadata = self.certificates.metadata(desired.profile.tls)
        keys = ('certificate_state', 'certificate_generation', 'certificate_environment', 'not_after',
                'fingerprint', 'next_attempt_at', 'certificate_error_code')
        values = {key: metadata[key] for key in keys if metadata.get(key) is not None}
        bundle = self.certificates.current(desired.profile.tls)
        served = self.runtime.served_fingerprint(effective or desired, bundle) if bundle and healthy else None
        now = datetime.now(timezone.utc)
        valid = bool(bundle and metadata.get('not_after') and metadata['not_after'] > now
                     and metadata.get('certificate_environment') == 'production'
                     and served == metadata.get('fingerprint'))
        if state == 'READY' and not valid:
            state = 'DEGRADED'
        return TrustTunnelActual(deployment_id=desired.deployment_id, desired_revision=desired.revision,
            validated_revision=desired.revision, applied_revision=applied,
            last_known_good_revision=(record.get('lkg') or {}).get('revision'),
            applied_digest=trusttunnel_digest(desired) if applied is not None else None,
            state=state, healthy=healthy, runtime_epoch=self.runtime.epoch if healthy else None,
            observed_at=now, error=error, served_fingerprint=served, **values)

    def _execute(self, desired, record):
        bundle = self.certificates.current(desired.profile.tls)
        if not desired.profile.enabled:
            self.runtime.stop()
            record.update(phase='disabled', effective=None)
            actual = self._actual(desired, record, state='DEGRADED', applied=desired.revision)
        elif bundle is None:
            # Initial issuance runs in supervisor; accepting a revoke remains bounded.
            self.runtime.stop()
            self.runtime.ensure_ingress()
            record.update(phase='applying', effective=None)
            actual = self._actual(desired, record, state='APPLYING')
        else:
            try:
                self.runtime.apply(desired, bundle)
                if not self.runtime.healthy(desired, bundle):
                    raise RuntimeError('runtime verification failed')
                if hasattr(self.certificates, 'mark_served'):
                    self.certificates.mark_served(desired.profile.tls, bundle)
                record.update(lkg=wire_payload(desired), effective=wire_payload(desired), phase='ready')
                actual = self._actual(desired, record, state='READY', applied=desired.revision, healthy=True)
            except Exception:
                actual = self._rollback(desired, record, bundle)
        record['actual'] = actual.model_dump(mode='json')
        with self.db:
            self._save(desired.deployment_id, record)
        return actual

    @staticmethod
    def _identity(client):
        return (client.peer_id, client.user_uuid, client.username, client.credential_generation,
                client.password.get_secret_value())

    def _rollback(self, desired, record, bundle):
        record['phase'] = 'rolling_back'
        with self.db:
            self._save(desired.deployment_id, record)
        healthy, fallback = False, None
        try:
            if hasattr(self.certificates, 'restore_served'):
                bundle = self.certificates.restore_served(desired.profile.tls) or bundle
            if record.get('lkg') and bundle:
                fallback = TrustTunnelDesired.model_validate(record['lkg'])
                authorized = {self._identity(client) for client in desired.clients}
                fallback.clients = [client for client in fallback.clients if self._identity(client) in authorized]
                # Latest disable is authoritative and never opens old ingress credentials.
                if not desired.profile.enabled:
                    self.runtime.stop()
                    fallback = None
                else:
                    self.runtime.apply(fallback, bundle)
                    healthy = self.runtime.healthy(fallback, bundle)
                    if not healthy:
                        raise RuntimeError('safe rollback failed')
            else:
                self.runtime.stop()
        except Exception:
            self.runtime.stop()
            fallback, healthy = None, False
        record.update(phase='degraded', effective=wire_payload(fallback) if fallback else None)
        return self._actual(desired, record, state='DEGRADED', healthy=healthy, effective=fallback,
            error=ValidationIssue(code='APPLY_FAILED', message='TrustTunnel apply failed; safe fallback enforced'))

    def recover(self):
        with self.lock:
            for identifier in self.identifiers():
                record = self._load(identifier)
                desired = TrustTunnelDesired.model_validate(record['desired'])
                if record['phase'] in ('rolling_back', 'degraded'):
                    actual = self._rollback(desired, record, self.certificates.current(desired.profile.tls))
                    record['actual'] = actual.model_dump(mode='json')
                    with self.db:
                        self._save(identifier, record)
                else:
                    self._execute(desired, record)

    def state(self, identifier):
        with self.lock:
            record = self._load(identifier)
            if record is None:
                raise KeyError('unknown deployment')
            desired = TrustTunnelDesired.model_validate(record['desired'])
            previous = TrustTunnelActual.model_validate(record['actual'])
            effective = TrustTunnelDesired.model_validate(record['effective']) if record.get('effective') else None
            bundle = self.certificates.current(desired.profile.tls)
            healthy = bool(effective and bundle and self.runtime.healthy(effective, bundle))
            state = 'READY' if healthy and record['phase'] == 'ready' else ('APPLYING' if record['phase'] == 'applying' else 'DEGRADED')
            return self._actual(desired, record, state=state, applied=previous.applied_revision,
                                healthy=healthy, error=previous.error, effective=effective)

    def export(self, identifier, peer_id, format):
        with self.lock:
            if format not in ('deeplink', 'toml'):
                raise ValueError('unsupported export format')
            if self.state(identifier).state != 'READY':
                raise ValueError('deployment is not production READY')
            desired = TrustTunnelDesired.model_validate(self._load(identifier)['effective'])
            peer = next((client for client in desired.clients if client.peer_id == peer_id), None)
            if peer is None:
                raise KeyError('unknown peer')
            return TrustTunnelExportResponse(peer_id=peer_id, format=format,
                content=self.runtime.export(desired, peer, format))

    def run_once(self):
        # No Controller request is needed for issuance, renewal, child monitoring or drift repair.
        with self.lock:
            records = [self._load(identifier) for identifier in self.identifiers()]
        for snapshot in records:
            desired = TrustTunnelDesired.model_validate(snapshot['desired'])
            if not desired.profile.enabled:
                continue
            self.runtime.ensure_ingress()
            self.certificates.ensure(desired.profile.tls)
            with self.lock:
                record = self._load(desired.deployment_id)
                latest = TrustTunnelDesired.model_validate(record['desired'])
                if record['phase'] in ('degraded', 'rolling_back'):
                    actual = self._rollback(latest, record, self.certificates.current(latest.profile.tls))
                    record['actual'] = actual.model_dump(mode='json')
                    with self.db:
                        self._save(latest.deployment_id, record)
                else:
                    self._execute(latest, record)

    def start(self):
        if self.worker is not None:
            return
        def supervise():
            while not self.stopping.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Never log subprocess/config/ACME exception bodies.
                    pass
                self.stopping.wait(5)
        self.worker = threading.Thread(target=supervise, name='trusttunnel-supervisor', daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()
        if hasattr(self.certificates, 'close'):
            self.certificates.close()
        if self.worker is not None:
            self.worker.join(timeout=15)
        with self.lock:
            self.runtime.close()
            self.db.close()
            self.owner.close()
