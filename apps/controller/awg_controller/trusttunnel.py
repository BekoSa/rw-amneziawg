"""Extension-owned TrustTunnel intent, encrypted credentials and publication gate.

Identity comes from the existing authoritative Remnawave cache. This service
never fetches a second users snapshot or connects to a stock database.
"""
import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb
from awg_contracts import (NodeRegistration, TrustTunnelProfile, TrustTunnelCredential,
    TrustTunnelDesired, TrustTunnelActual, TrustTunnelMaterial, TrustTunnelSubscriptionEntry,
    TrustTunnelExportResponse, trusttunnel_digest, wire_payload, content_digest)
from remnawave_client import User
from .agent_client import AgentError
from .domain import KeyVault, eligible

TT_LOCK_ID = 472319814


async def one(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchone()


async def rows(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchall()


class TrustTunnelController:
    def __init__(self, db, upstream, agents, settings):
        self.db, self.upstream, self.agents, self.settings = db, upstream, agents, settings
        self.vault = KeyVault(settings.encryption_key, getattr(settings, 'encryption_key_id', 'default'),
                             getattr(settings, 'previous_encryption_keys', None))
        self.wakeup = None

    def _wake(self):
        if self.wakeup is not None:
            self.wakeup.set()

    @staticmethod
    def _credential_context(user_id, generation):
        return f'trusttunnel:{user_id}:{generation}'

    @staticmethod
    def _desired_context(deployment_id, revision):
        return f'trusttunnel:desired:{deployment_id}:{revision}'

    def _decrypt_desired(self, row):
        return TrustTunnelDesired.model_validate_json(self.vault.decrypt(
            self._desired_context(row['id'], row['next_revision']-1), row['encrypted_desired']))

    async def _capabilities(self, registration):
        capabilities = await self.agents.capabilities(registration)
        if capabilities.node_id != registration.node_id or capabilities.runtime == 'mock':
            raise AgentError('Unsupported TrustTunnel agent', 'TRUSTTUNNEL_UNSUPPORTED')
        if not any(p.adapter_id == 'trusttunnel' and '1.0' in p.versions for p in capabilities.protocols):
            raise AgentError('Unsupported TrustTunnel agent', 'TRUSTTUNNEL_UNSUPPORTED')
        return capabilities

    async def save_profile(self, profile):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (TT_LOCK_ID,))
            await conn.execute('''INSERT INTO tt_profiles(id,draft) VALUES(%s,%s)
                ON CONFLICT(id) DO UPDATE SET draft=EXCLUDED.draft,validated_digest=NULL,updated_at=now()''',
                (profile.profile_id, Jsonb(profile.model_dump(mode='json'))))

    async def _deployment(self, conn, profile_id, node_id):
        existing = await one(conn, 'SELECT * FROM tt_deployments WHERE node_id=%s', (node_id,))
        if existing and existing['profile_id'] != profile_id:
            raise ValueError('Only one TrustTunnel deployment per node is supported')
        await conn.execute('''INSERT INTO tt_deployments(id,profile_id,node_id) VALUES(%s,%s,%s)
            ON CONFLICT(profile_id,node_id) DO NOTHING''', (uuid4(), profile_id, node_id))
        return await one(conn, 'SELECT * FROM tt_deployments WHERE profile_id=%s AND node_id=%s', (profile_id, node_id))

    @staticmethod
    def _local_profile(profile, node):
        registration = NodeRegistration.model_validate(node['registration'])
        enabled = profile.enabled and registration.enabled and node['id'] in profile.node_ids
        # Detachment is a target-local disabled snapshot: exact contract node identity,
        # no users/sessions on the former node, while another node receives its own set.
        return profile.model_copy(update={'node_ids':[node['id']], 'enabled':enabled})

    async def validate_profile(self, profile_id):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (TT_LOCK_ID,))
            record = await one(conn, 'SELECT draft FROM tt_profiles WHERE id=%s', (profile_id,))
            if not record:
                raise ValueError('Unknown profile')
            profile = TrustTunnelProfile.model_validate(record['draft'])
            node_ids = set(profile.node_ids)
            node_ids.update(r['node_id'] for r in await rows(conn,
                'SELECT node_id FROM tt_deployments WHERE profile_id=%s AND encrypted_desired IS NOT NULL', (profile_id,)))
            results = []
            for node_id in sorted(node_ids, key=str):
                node = await one(conn, 'SELECT * FROM awg_nodes WHERE id=%s', (node_id,))
                if not node:
                    raise ValueError('Unknown node')
                registration = NodeRegistration.model_validate(node['registration'])
                await self._capabilities(registration)
                deployment = await self._deployment(conn, profile_id, node_id)
                clients = self._decrypt_desired(deployment).clients if deployment['encrypted_desired'] else []
                local = self._local_profile(profile, node)
                desired = TrustTunnelDesired(deployment_id=deployment['id'], node_id=node_id,
                    revision=deployment['next_revision'], profile=local, clients=clients if local.enabled else [])
                result = await self.agents.trusttunnel_validate(registration, desired)
                if result.digest != trusttunnel_digest(desired) or result.revision != desired.revision:
                    raise AgentError('Validation identity mismatch')
                results.append(result)
            valid = bool(results) and all(r.valid for r in results)
            digest = content_digest(profile)
            await conn.execute('UPDATE tt_profiles SET validated_digest=%s WHERE id=%s',
                (digest if valid else None, profile_id))
            return {'valid':valid, 'draft_digest':digest, 'results':[r.model_dump(mode='json') for r in results]}

    async def apply_profile(self, profile_id, digest):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (TT_LOCK_ID,))
            record = await one(conn, 'SELECT * FROM tt_profiles WHERE id=%s', (profile_id,))
            if (not record or not record['validated_digest'] or digest != record['validated_digest'] or
                    digest != content_digest(record['draft'])):
                raise ValueError('Profile must pass validation after the last edit')
            await conn.execute('UPDATE tt_profiles SET active=draft WHERE id=%s', (profile_id,))
        self._wake()

    async def _credential(self, conn, user_id):
        record = await one(conn, 'SELECT * FROM tt_credentials WHERE user_id=%s', (user_id,))
        if not record:
            username = 'tt_' + secrets.token_hex(16)
            context = self._credential_context(user_id, 1)
            encrypted = self.vault.encrypt(context, secrets.token_urlsafe(32))
            await conn.execute('''INSERT INTO tt_credentials(user_id,username,encrypted_password)
                VALUES(%s,%s,%s)''', (user_id, username, encrypted))
            record = await one(conn, 'SELECT * FROM tt_credentials WHERE user_id=%s', (user_id,))
        if self.vault.needs_rotation(record['encrypted_password']):
            context = self._credential_context(user_id, record['credential_generation'])
            encrypted = self.vault.encrypt(context, self.vault.decrypt(context, record['encrypted_password']))
            await conn.execute('UPDATE tt_credentials SET encrypted_password=%s WHERE user_id=%s', (encrypted, user_id))
            record['encrypted_password'] = encrypted
        return record

    async def rotate_credentials(self, user_uuid):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (TT_LOCK_ID,))
            user = await one(conn, 'SELECT id FROM awg_users WHERE id=%s AND NOT deleted', (user_uuid,))
            record = await one(conn, 'SELECT * FROM tt_credentials WHERE user_id=%s', (user_uuid,))
            if not user or not record:
                raise ValueError('Unknown TrustTunnel user')
            generation = record['credential_generation']+1
            encrypted = self.vault.encrypt(self._credential_context(user_uuid, generation), secrets.token_urlsafe(32))
            await conn.execute('''UPDATE tt_credentials SET encrypted_password=%s,credential_generation=%s
                WHERE user_id=%s''', (encrypted, generation, user_uuid))
            # New generation makes old runtime material unpublishable immediately.
            await conn.execute('''UPDATE tt_deployments SET last_error_code=NULL WHERE id IN
                (SELECT deployment_id FROM tt_peers WHERE user_id=%s)''', (user_uuid,))
        self._wake()

    async def _desired(self, conn, profile, node, snapshot):
        deployment = await self._deployment(conn, profile.profile_id, node['id'])
        local = self._local_profile(profile, node)
        accepted = set()
        clients = []
        for user_id, user in sorted(snapshot.items(), key=lambda pair:str(pair[0])):
            stored = await one(conn, 'SELECT upstream,usage,deleted FROM awg_users WHERE id=%s', (user_id,))
            if stored:
                # Current authoritative cache facts dominate any older snapshot
                # captured before a disable, expiry or entitlement change.
                user = User.model_validate(stored['upstream'])
            if (not stored or stored['deleted'] or not local.enabled or
                    not eligible(user, local.access.squad_ids, local.access.user_ids,
                                 int(stored['usage']), self.settings.quota_policy)):
                continue
            credential = await self._credential(conn, user_id)
            peer = await one(conn, 'SELECT * FROM tt_peers WHERE user_id=%s AND deployment_id=%s', (user_id, deployment['id']))
            if not peer:
                peer = {'id':uuid4()}
                await conn.execute('''INSERT INTO tt_peers(id,user_id,deployment_id,credential_generation)
                    VALUES(%s,%s,%s,%s)''', (peer['id'], user_id, deployment['id'], credential['credential_generation']))
            else:
                await conn.execute('UPDATE tt_peers SET present=true,credential_generation=%s WHERE id=%s',
                                   (credential['credential_generation'], peer['id']))
                await conn.execute('DELETE FROM tt_tombstones WHERE peer_id=%s', (peer['id'],))
            password = self.vault.decrypt(self._credential_context(user_id, credential['credential_generation']),
                                          credential['encrypted_password'])
            clients.append(TrustTunnelCredential(peer_id=peer['id'], user_uuid=user_id,
                username=credential['username'], password=password, credential_generation=credential['credential_generation']))
            accepted.add(user_id)
        for peer in await rows(conn, 'SELECT * FROM tt_peers WHERE deployment_id=%s AND present', (deployment['id'],)):
            if peer['user_id'] not in accepted:
                await conn.execute('UPDATE tt_peers SET present=false WHERE id=%s', (peer['id'],))
                await conn.execute('''INSERT INTO tt_tombstones(peer_id,credential_generation) VALUES(%s,%s)
                    ON CONFLICT(peer_id) DO UPDATE SET credential_generation=EXCLUDED.credential_generation,
                    acknowledged_at=NULL,created_at=now()''', (peer['id'], peer['credential_generation']))
        current = self._decrypt_desired(deployment) if deployment['encrypted_desired'] else None
        candidate = TrustTunnelDesired(deployment_id=deployment['id'], node_id=node['id'],
            revision=current.revision if current else deployment['next_revision'], profile=local, clients=clients)
        if current and trusttunnel_digest(current) == trusttunnel_digest(candidate):
            return
        desired = candidate.model_copy(update={'revision':deployment['next_revision']})
        digest = trusttunnel_digest(desired)
        encrypted = self.vault.encrypt(self._desired_context(deployment['id'], desired.revision),
                                      json.dumps(wire_payload(desired), separators=(',', ':'), ensure_ascii=False))
        await conn.execute('''UPDATE tt_deployments SET encrypted_desired=%s,desired_digest=%s,
            next_revision=next_revision+1 WHERE id=%s''', (encrypted, digest, deployment['id']))
        await conn.execute('''INSERT INTO tt_revisions(deployment_id,revision,digest,encrypted_desired)
            VALUES(%s,%s,%s,%s)''', (deployment['id'], desired.revision, digest, encrypted))

    async def reconcile(self, snapshot):
        """Use the authoritative cache committed by the existing controller.

        Persist all snapshots before sending them. Initial certificate issuance is
        reported APPLYING; it is polled later, never awaited until certificate ready.
        The supplied snapshot is a reconcile trigger, never an override of newer
        cache facts. Cache lifecycle writes use the same TT serialization lock.
        """
        async with self.db.connect(autocommit=True) as conn:
            acquired = await one(conn, 'SELECT pg_try_advisory_lock(%s) AS acquired', (TT_LOCK_ID,))
            if not acquired['acquired']:
                return
            try:
                async with conn.transaction():
                    snapshot = {row['id']:User.model_validate(row['upstream']) for row in
                        await rows(conn, 'SELECT id,upstream FROM awg_users WHERE NOT deleted')}
                    nodes = await rows(conn, 'SELECT * FROM awg_nodes')
                    for record in await rows(conn, 'SELECT active FROM tt_profiles WHERE active IS NOT NULL'):
                        profile = TrustTunnelProfile.model_validate(record['active'])
                        for node in nodes:
                            existing = await one(conn, 'SELECT id FROM tt_deployments WHERE profile_id=%s AND node_id=%s',
                                                 (profile.profile_id, node['id']))
                            if node['id'] in profile.node_ids or existing:
                                await self._desired(conn, profile, node, snapshot)
                for deployment in await rows(conn, '''SELECT d.*,n.registration FROM tt_deployments d
                    JOIN awg_nodes n ON n.id=d.node_id WHERE d.encrypted_desired IS NOT NULL'''):
                    await self._deliver(conn, deployment)
            finally:
                await conn.execute('SELECT pg_advisory_unlock(%s)', (TT_LOCK_ID,))

    async def reconcile_cached(self):
        """Use only known cache facts during outages; absence is never inferred."""
        # Cache is read only after reconcile owns TT_LOCK_ID. Reading it here
        # would let an older ACTIVE snapshot race a newer authoritative revoke.
        await self.reconcile(None)

    async def _deliver(self, conn, deployment):
        registration = NodeRegistration.model_validate(deployment['registration'])
        try:
            desired = self._decrypt_desired(deployment)
            await self._capabilities(registration)
            actual = await self.agents.trusttunnel_state(registration, deployment['id'])
            if actual.deployment_id != desired.deployment_id:
                raise AgentError('Deployment identity mismatch')
            # accepted initial issuance must be polled, not reapplied each period.
            accepted = actual.desired_revision == desired.revision and actual.state.value == 'APPLYING'
            matching = actual.applied_revision == desired.revision and actual.applied_digest == deployment['desired_digest']
            if not accepted and not matching:
                result = await self.agents.trusttunnel_validate(registration, desired)
                if (not result.valid or result.revision != desired.revision or
                        result.digest != deployment['desired_digest']):
                    raise AgentError('TrustTunnel desired invalid', 'TRUSTTUNNEL_INVALID')
                actual = await self.agents.trusttunnel_apply(registration, desired, actual.applied_revision)
                if actual.deployment_id != desired.deployment_id:
                    raise AgentError('Deployment identity mismatch')
            await conn.execute('''UPDATE tt_deployments SET actual=%s,last_seen_at=now(),last_error_code=NULL
                WHERE id=%s''', (Jsonb(actual.model_dump(mode='json')), deployment['id']))
            if actual.applied_revision == desired.revision and actual.applied_digest == deployment['desired_digest']:
                await conn.execute('''UPDATE tt_tombstones SET acknowledged_at=now() WHERE acknowledged_at IS NULL
                    AND peer_id IN (SELECT id FROM tt_peers WHERE deployment_id=%s AND NOT present)''', (deployment['id'],))
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            code = getattr(failure, 'reason', 'TRUSTTUNNEL_RECONCILE_FAILED')
            # Secrets and transport exception strings never enter errors/metadata.
            await conn.execute('UPDATE tt_deployments SET last_error_code=%s WHERE id=%s', (code, deployment['id']))

    def _publishable(self, row, desired, actual, active, registration, now):
        if (not row['last_seen_at'] or row['last_error_code'] or not registration.enabled or
                not active.enabled or desired.node_id not in active.node_ids or desired.profile != active or
                now-row['last_seen_at'] > timedelta(seconds=self.settings.ready_max_age) or
                now-actual.observed_at > timedelta(seconds=self.settings.ready_max_age) or
                actual.observed_at > now+timedelta(seconds=5)):
            return False
        return (actual.state.value == 'READY' and actual.healthy and
            actual.desired_revision == desired.revision and actual.applied_revision == desired.revision and
            actual.applied_digest == trusttunnel_digest(desired) and
            actual.certificate_environment == 'production' and actual.certificate_generation >= 1 and
            actual.certificate_state in {'VALID','RENEWING','ERROR'} and actual.not_after is not None and
            actual.not_after > now and actual.fingerprint is not None and
            actual.fingerprint == actual.served_fingerprint)

    async def material(self, token, *, native_format=None):
        if native_format not in {None, 'deeplink', 'toml', 'all'}:
            raise ValueError('Unknown export format')
        user = await self.upstream.by_short_uuid(token)
        if user is None:
            return None
        return await self._material_for_user(user, native_format=native_format)

    async def export_peer(self, peer_id, format):
        """Explicit authenticated admin export, with authoritative lifecycle lookup."""
        if format not in {'deeplink','toml'}:
            raise ValueError('Unknown export format')
        async with self.db.connect() as conn:
            record = await one(conn, '''SELECT u.id,u.upstream FROM tt_peers p
                JOIN awg_users u ON u.id=p.user_id WHERE p.id=%s AND p.present AND NOT u.deleted''', (peer_id,))
        if not record:
            raise ValueError('Peer unavailable')
        user = await self.upstream.user(record['upstream'].get('upstream_id') or str(record['id']))
        if user is None or user.user_uuid != record['id']:
            raise ValueError('Peer unavailable')
        material = await self._material_for_user(user, native_format=format, peer_id=peer_id)
        if not material.entries:
            raise ValueError('Peer unavailable')
        entry = material.entries[0]
        content = entry.native_deeplink if format == 'deeplink' else entry.native_toml
        if content is None:
            raise AgentError('Export unavailable')
        return TrustTunnelExportResponse(peer_id=peer_id, format=format, content=content)

    async def _material_for_user(self, user, *, native_format=None, peer_id=None):
        output = []
        async with self.db.connect() as conn:
            # Serialize with rotation/Apply so stale secrets cannot be published
            # from a concurrently edited generation or pending revoke snapshot.
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (TT_LOCK_ID,))
            stored = await one(conn, 'SELECT * FROM awg_users WHERE id=%s AND NOT deleted', (user.user_uuid,))
            credential = await one(conn, 'SELECT * FROM tt_credentials WHERE user_id=%s', (user.user_uuid,))
            if not stored or not credential:
                return TrustTunnelMaterial(user_uuid=user.user_uuid)
            cached_user = User.model_validate(stored['upstream'])
            for row in await rows(conn, '''SELECT d.*,p.id AS peer_id,p.credential_generation AS peer_generation,
                n.registration,f.active FROM tt_peers p JOIN tt_deployments d ON d.id=p.deployment_id
                JOIN awg_nodes n ON n.id=d.node_id JOIN tt_profiles f ON f.id=d.profile_id
                WHERE p.user_id=%s AND p.present''', (user.user_uuid,)):
                if peer_id is not None and row['peer_id'] != peer_id:
                    continue
                if not row['encrypted_desired'] or not row['actual'] or not row['active']:
                    continue
                desired = self._decrypt_desired(row)
                actual = TrustTunnelActual.model_validate(row['actual'])
                active = TrustTunnelProfile.model_validate(row['active'])
                registration = NodeRegistration.model_validate(row['registration'])
                if (not self._publishable(row, desired, actual, active, registration, datetime.now(timezone.utc)) or
                        not eligible(user, active.access.squad_ids, active.access.user_ids,
                                     int(stored['usage']), self.settings.quota_policy) or
                        not eligible(cached_user, active.access.squad_ids, active.access.user_ids,
                                     int(stored['usage']), self.settings.quota_policy)):
                    continue
                peer = next((client for client in desired.clients if client.peer_id == row['peer_id'] and
                             client.user_uuid == user.user_uuid and
                             client.credential_generation == credential['credential_generation'] and
                             client.username == credential['username']), None)
                if peer is None or row['peer_generation'] != credential['credential_generation']:
                    continue
                password = self.vault.decrypt(self._credential_context(user.user_uuid, credential['credential_generation']),
                                              credential['encrypted_password'])
                if not secrets.compare_digest(peer.password.get_secret_value(), password):
                    continue
                native = {}
                formats = ['deeplink','toml'] if native_format == 'all' else [native_format] if native_format else []
                try:
                    exports = await asyncio.wait_for(asyncio.gather(*(self.agents.trusttunnel_export(
                        registration, desired.deployment_id, peer.peer_id, format) for format in formats)), timeout=10)
                except TimeoutError:
                    raise AgentError('Export timed out', 'AGENT_TIMEOUT') from None
                for format, exported in zip(formats, exports):
                    if exported.peer_id != peer.peer_id or exported.format != format:
                        raise AgentError('Export identity mismatch')
                    native['native_'+format] = exported.content
                # Bounded exports can cross certificate expiry; repeat freshness.
                if (not self._publishable(row, desired, actual, active, registration, datetime.now(timezone.utc)) or
                        not eligible(user, active.access.squad_ids, active.access.user_ids,
                                     int(stored['usage']), self.settings.quota_policy) or
                        not eligible(cached_user, active.access.squad_ids, active.access.user_ids,
                                     int(stored['usage']), self.settings.quota_policy)):
                    continue
                output.append(TrustTunnelSubscriptionEntry(profile_id=active.profile_id,
                    node_id=desired.node_id, peer_id=peer.peer_id, name=active.name, endpoint=active.endpoint,
                    tls_server_name=active.tls.domain, username=credential['username'], password=password,
                    credential_generation=credential['credential_generation'], **native))
        return TrustTunnelMaterial(user_uuid=user.user_uuid, entries=output)

    async def profiles(self):
        async with self.db.connect() as conn:
            return {'items':await rows(conn, 'SELECT * FROM tt_profiles ORDER BY id')}

    async def status(self, profile_id=None):
        async with self.db.connect() as conn:
            return {'items':await rows(conn, '''SELECT id,profile_id,node_id,actual,last_seen_at,last_error_code
                FROM tt_deployments WHERE (%s::uuid IS NULL OR profile_id=%s) ORDER BY id''', (profile_id,profile_id))}

    async def peers(self, profile_id=None):
        async with self.db.connect() as conn:
            return {'items':await rows(conn, '''SELECT p.*,d.node_id,d.profile_id,c.username AS credential_username,
                u.upstream->>'username' AS username,u.upstream->>'status' AS user_status,u.deleted AS user_deleted,
                (t.peer_id IS NOT NULL AND t.acknowledged_at IS NULL) AS removal_pending
                FROM tt_peers p JOIN tt_deployments d ON d.id=p.deployment_id
                JOIN awg_users u ON u.id=p.user_id JOIN tt_credentials c ON c.user_id=p.user_id
                LEFT JOIN tt_tombstones t ON t.peer_id=p.id
                WHERE (%s::uuid IS NULL OR d.profile_id=%s)
                AND (NOT u.deleted OR (t.peer_id IS NOT NULL AND t.acknowledged_at IS NULL)) ORDER BY p.id''',
                (profile_id,profile_id))}
