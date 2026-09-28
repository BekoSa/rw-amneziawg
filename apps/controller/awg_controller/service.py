"""Persist intent before delivery; retries replay complete desired snapshots."""
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from awg_contracts import (AWGProfile, DesiredDeployment, PeerSpec, NodeRegistration,
                          ActualDeployment, content_digest, SubscriptionMaterial, SubscriptionPeer)

from .db import LOCK_ID
from .domain import KeyVault, eligible, counter_delta
from .agent_client import AgentError
from remnawave_client import UpstreamError


async def one(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchone()


async def rows(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchall()


class Controller:
    def __init__(self, db, upstream, agents, settings):
        self.db, self.upstream, self.agents, self.settings = db, upstream, agents, settings
        self.vault = KeyVault(settings.encryption_key, getattr(settings, 'encryption_key_id', 'default'),
                              getattr(settings, 'previous_encryption_keys', None))
        self.wakeup = asyncio.Event()
        self.metrics = {'reconcile_runs':0,'reconcile_errors':0,'apply':0,'apply_errors':0}
        self.last_upstream_ok = None

    async def error(self, code, entity=None):
        async with self.db.connect() as conn:
            await conn.execute('INSERT INTO awg_errors(code,entity_id) VALUES(%s,%s)',(code,entity))
            await conn.execute('DELETE FROM awg_errors WHERE id < (SELECT COALESCE(max(id),0)-1000 FROM awg_errors)')

    async def loop(self):
        while True:
            self.wakeup.clear()
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as failure:
                self.metrics['reconcile_errors'] += 1
                try:
                    # An expired/revoked API token is the most common cause and needs an operator: name it.
                    await self.error('REMNAWAVE_TOKEN_REJECTED' if getattr(failure, 'unauthorized', False) else 'RECONCILE_FAILED')
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self.wakeup.wait(), self.settings.reconcile_seconds)
            except TimeoutError:
                pass

    async def save_profile(self, profile):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            existing = await one(conn,'SELECT active FROM awg_profiles WHERE id=%s',(profile.profile_id,))
            if existing and existing['active']:
                active = AWGProfile.model_validate(existing['active'])
                # Pool changes require a separate drain/migration; never silently readdress peers.
                if active.network != profile.network:
                    raise ValueError('Active network changes require a drain/migration')
            await conn.execute('''INSERT INTO awg_profiles(id,draft) VALUES(%s,%s)
                ON CONFLICT(id) DO UPDATE SET draft=EXCLUDED.draft,validated_digest=NULL,updated_at=now()''',
                (profile.profile_id,Jsonb(profile.model_dump(mode='json'))))

    async def rotate_user_key(self, user_uuid):
        """New client keypair, same identity/IPs. The old key joins the deny overlay of every
        deployment so neither the Agent's rollback nor an offline node can resurrect it."""
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            user = await one(conn,'SELECT public_key FROM awg_users WHERE id=%s AND NOT deleted',(user_uuid,))
            if not user:
                raise ValueError('Unknown user')
            for row in await rows(conn,'SELECT DISTINCT deployment_id FROM awg_peers WHERE user_id=%s',(user_uuid,)):
                await conn.execute('INSERT INTO awg_revoked_keys(deployment_id,public_key) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                    (row['deployment_id'],user['public_key']))
            encrypted,public = self.vault.generate(str(user_uuid))
            await conn.execute('UPDATE awg_users SET encrypted_key=%s,public_key=%s,key_generation=key_generation+1 WHERE id=%s',
                (encrypted,public,user_uuid))
        self.wakeup.set()

    async def register_node(self, registration):
        capabilities = await self.agents.capabilities(registration)
        if capabilities.runtime == 'mock':
            raise ValueError('Mock Agent cannot be registered as a managed node')
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            await conn.execute('''INSERT INTO awg_nodes(id,registration,capabilities,online,last_seen_at)
                VALUES(%s,%s,%s,true,now()) ON CONFLICT(id) DO UPDATE SET registration=EXCLUDED.registration,
                capabilities=EXCLUDED.capabilities,online=true,last_seen_at=now()''',
                (registration.node_id,Jsonb(registration.model_dump(mode='json')),Jsonb(capabilities.model_dump(mode='json'))))
        self.wakeup.set()

    async def _deployment(self, conn, profile_id, node_id):
        await conn.execute('INSERT INTO awg_deployments(id,profile_id,node_id) VALUES(%s,%s,%s) ON CONFLICT(profile_id,node_id) DO NOTHING',
            (uuid4(),profile_id,node_id))
        return await one(conn,'SELECT * FROM awg_deployments WHERE profile_id=%s AND node_id=%s',(profile_id,node_id))

    async def validate_profile(self, profile_id):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            record = await one(conn,'SELECT draft FROM awg_profiles WHERE id=%s',(profile_id,))
            if not record:
                raise ValueError('Unknown profile')
            profile = AWGProfile.model_validate(record['draft'])
            results = []
            if not profile.node_ids:
                return {'valid':False,'results':[]}
            for node_id in profile.node_ids:
                node = await one(conn,'SELECT registration FROM awg_nodes WHERE id=%s',(node_id,))
                if not node:
                    raise ValueError('Unknown node')
                deployment = await self._deployment(conn,profile_id,node_id)
                peers = []
                if deployment['desired']:
                    peers = DesiredDeployment.model_validate(deployment['desired']).peers
                desired = DesiredDeployment(deployment_id=deployment['id'],node_id=node_id,
                    revision=deployment['next_revision'],profile=profile,peers=peers)
                result = await self.agents.validate(NodeRegistration.model_validate(node['registration']),desired)
                if result.digest != content_digest(desired) or result.revision != desired.revision:
                    raise AgentError('Validation digest mismatch')
                results.append(result)
            valid = all(r.valid for r in results)
            await conn.execute('UPDATE awg_profiles SET validated_digest=%s WHERE id=%s',
                (content_digest(profile) if valid else None,profile_id))
            return {'valid':valid,'draft_digest':content_digest(profile),'results':[r.model_dump(mode='json') for r in results]}

    async def apply_profile(self, profile_id, expected_digest):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            record = await one(conn,'SELECT * FROM awg_profiles WHERE id=%s',(profile_id,))
            if not record or record['validated_digest'] != content_digest(record['draft']) or expected_digest != record['validated_digest']:
                raise ValueError('Profile must pass validation after the last edit')
            await conn.execute('UPDATE awg_profiles SET active=draft WHERE id=%s',(profile_id,))
        self.wakeup.set()

    async def reconcile(self):
        self.metrics['reconcile_runs'] += 1
        async with self.db.connect(autocommit=True) as conn:
            acquired = await one(conn,'SELECT pg_try_advisory_lock(%s) AS acquired',(LOCK_ID,))
            if not acquired['acquired']:
                return
            try:
                users = await self.upstream.users()
                known = await rows(conn,'SELECT id,upstream FROM awg_users WHERE NOT deleted')
                deleted = []
                numeric = {str(u.upstream_id or '').isdigit() for u in users.values()}
                for row in known:
                    if row['id'] not in users:
                        upstream_id = row['upstream'].get('upstream_id') or str(row['id'])
                        if numeric and numeric != {upstream_id.isdigit()}:
                            # Stored identity scheme differs from the live upstream (2.x rows on 3.x):
                            # a 404 there would not mean "deleted". Fail closed.
                            raise UpstreamError('Remnawave identity scheme mismatch')
                        user = await self.upstream.user(upstream_id)
                        if user is None:
                            deleted.append(row['id'])
                        elif user.user_uuid != row['id']:
                            # Identity scheme drift (e.g. 2.x rows on 3.x): fail closed, never delete.
                            raise UpstreamError('Remnawave identity mismatch')
                        else:
                            users[user.user_uuid] = user
                # No write until ALL authoritative fetches succeeded.
                self.last_upstream_ok = datetime.now(timezone.utc)
                async with conn.transaction():
                    await self._persist_users(conn, users, deleted)
                    for profile_row in await rows(conn,'SELECT active FROM awg_profiles WHERE active IS NOT NULL'):
                        profile = AWGProfile.model_validate(profile_row['active'])
                        nodes = await rows(conn,'SELECT * FROM awg_nodes')
                        for node in nodes:
                            existing = await one(conn,'SELECT id FROM awg_deployments WHERE profile_id=%s AND node_id=%s',(profile.profile_id,node['id']))
                            if node['id'] in profile.node_ids or existing:
                                await self._desired(conn,profile,node,users)
                for deployment in await rows(conn,'SELECT d.*,n.registration FROM awg_deployments d JOIN awg_nodes n ON n.id=d.node_id WHERE desired IS NOT NULL'):
                    await self._deliver(conn,deployment)
            finally:
                await conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))

    async def _persist_users(self, conn, users, deleted):
        for user in users.values():
            old = await one(conn,'SELECT reset_at,encrypted_key FROM awg_users WHERE id=%s',(user.user_uuid,))
            if old:
                if self.vault.needs_rotation(old['encrypted_key']):
                    owner = str(user.user_uuid)
                    try:
                        rotated = self.vault.encrypt(owner,self.vault.decrypt(owner,old['encrypted_key']))
                    except Exception:
                        # One undecryptable row must not block revocations for everyone else.
                        await conn.execute("INSERT INTO awg_errors(code,entity_id) VALUES('KEY_DECRYPT_FAILED',%s)",(user.user_uuid,))
                    else:
                        await conn.execute('UPDATE awg_users SET encrypted_key=%s WHERE id=%s',(rotated,user.user_uuid))
                reset = user.last_traffic_reset_at is not None and (old['reset_at'] is None or user.last_traffic_reset_at > old['reset_at'])
                await conn.execute('UPDATE awg_users SET upstream=%s,deleted=false,usage=CASE WHEN %s THEN 0 ELSE usage END,reset_at=%s WHERE id=%s',
                    (Jsonb(user.model_dump(mode='json')),reset,user.last_traffic_reset_at,user.user_uuid))
            else:
                encrypted,public = self.vault.generate(str(user.user_uuid))
                await conn.execute('INSERT INTO awg_users(id,upstream,encrypted_key,public_key,reset_at) VALUES(%s,%s,%s,%s,%s)',
                    (user.user_uuid,Jsonb(user.model_dump(mode='json')),encrypted,public,user.last_traffic_reset_at))
        for user_id in deleted:
            await conn.execute('UPDATE awg_users SET deleted=true WHERE id=%s',(user_id,))

    async def _desired(self, conn, profile, node, users):
        deployment = await self._deployment(conn,profile.profile_id,node['id'])
        registration = NodeRegistration.model_validate(node['registration'])
        accepted = set()
        for user in users.values():
            stored = await one(conn,'SELECT * FROM awg_users WHERE id=%s',(user.user_uuid,))
            if profile.enabled and registration.enabled and node['id'] in profile.node_ids and eligible(user,profile.access.squad_ids,profile.access.user_ids,int(stored['usage']),self.settings.quota_policy):
                accepted.add(user.user_uuid)
                peer = await one(conn,'SELECT * FROM awg_peers WHERE user_id=%s AND deployment_id=%s',(user.user_uuid,deployment['id']))
                if peer is None:
                    peer_id = uuid4()
                    address = await self.db.allocate(conn,profile.profile_id,profile.network.ipv4_pool,profile.network.server_ipv4,peer_id)
                    ipv6 = None
                    if profile.network.ipv6_pool:
                        ipv6 = await self.db.allocate(conn,profile.profile_id,profile.network.ipv6_pool,profile.network.server_ipv6,peer_id)
                    await conn.execute('INSERT INTO awg_peers(id,user_id,deployment_id,ipv4,ipv6) VALUES(%s,%s,%s,%s,%s)',
                        (peer_id,user.user_uuid,deployment['id'],address,ipv6))
                else:
                    if not peer['present']:
                        # The same peer (same key) may take back what IT released, so clients keep a
                        # working config after disable/enable - but only if every address family is
                        # still free. Otherwise release and allocate a fresh set.
                        families = 2 if profile.network.ipv6_pool else 1
                        mine = await rows(conn,'''SELECT address FROM awg_ip_allocations WHERE profile_id=%s AND peer_id IS NULL
                            AND released_by=%s AND address IN (%s::inet,%s::inet) FOR UPDATE''',
                            (profile.profile_id,peer['id'],peer['ipv4'],peer['ipv6'] or peer['ipv4']))
                        held = await rows(conn,'SELECT 1 FROM awg_ip_allocations WHERE peer_id=%s',(peer['id'],))
                        if len(held) == families:
                            pass
                        elif not held and len(mine) == families:
                            await conn.execute('''UPDATE awg_ip_allocations SET peer_id=%s,release_after=NULL,released_by=NULL
                                WHERE profile_id=%s AND released_by=%s AND peer_id IS NULL''',(peer['id'],profile.profile_id,peer['id']))
                        else:
                            await conn.execute('UPDATE awg_ip_allocations SET peer_id=NULL,release_after=now(),released_by=NULL WHERE peer_id=%s',(peer['id'],))
                            address = await self.db.allocate(conn,profile.profile_id,profile.network.ipv4_pool,profile.network.server_ipv4,peer['id'])
                            ipv6 = await self.db.allocate(conn,profile.profile_id,profile.network.ipv6_pool,profile.network.server_ipv6,peer['id']) if profile.network.ipv6_pool else None
                            await conn.execute('UPDATE awg_peers SET ipv4=%s,ipv6=%s WHERE id=%s',(address,ipv6,peer['id']))
                    await conn.execute('UPDATE awg_peers SET present=true WHERE id=%s',(peer['id'],))
                    await conn.execute('DELETE FROM awg_tombstones WHERE peer_id=%s',(peer['id'],))
        peers = await rows(conn,'SELECT p.*,u.public_key,u.key_generation FROM awg_peers p JOIN awg_users u ON u.id=p.user_id WHERE deployment_id=%s ORDER BY p.id',(deployment['id'],))
        present = []
        for peer in peers:
            if peer['user_id'] in accepted:
                present.append(PeerSpec(peer_id=peer['id'],user_uuid=peer['user_id'],public_key=peer['public_key'],
                    ipv4_address=peer['ipv4'],ipv6_address=peer['ipv6'],key_generation=peer['key_generation']))
            else:
                await conn.execute('UPDATE awg_peers SET present=false WHERE id=%s',(peer['id'],))
                await conn.execute('INSERT INTO awg_tombstones(peer_id,public_key) VALUES(%s,%s) ON CONFLICT DO NOTHING',(peer['id'],peer['public_key']))
        tombstones = await rows(conn,'SELECT t.public_key FROM awg_tombstones t JOIN awg_peers p ON p.id=t.peer_id WHERE p.deployment_id=%s ORDER BY t.public_key',(deployment['id'],))
        rotated = await rows(conn,'SELECT public_key FROM awg_revoked_keys WHERE deployment_id=%s',(deployment['id'],))
        desired = DesiredDeployment(deployment_id=deployment['id'],node_id=node['id'],revision=deployment['next_revision'],
            profile=profile,peers=present,revoked_public_keys=sorted({t['public_key'] for t in tombstones+rotated}))
        old = deployment['desired']
        if old:
            comparable = desired.model_dump(mode='json')
            comparable['revision'] = old['revision']
            if comparable == old:
                return
        value = desired.model_dump(mode='json')
        await conn.execute('UPDATE awg_deployments SET desired=%s,next_revision=next_revision+1 WHERE id=%s',(Jsonb(value),deployment['id']))
        await conn.execute('INSERT INTO awg_revisions(deployment_id,revision,digest,desired) VALUES(%s,%s,%s,%s)',
            (deployment['id'],desired.revision,content_digest(desired),Jsonb(value)))

    async def _deliver(self, conn, deployment):
        registration = NodeRegistration.model_validate(deployment['registration'])
        desired = DesiredDeployment.model_validate(deployment['desired'])
        try:
            capabilities = await self.agents.capabilities(registration)
            actual = await self.agents.state(registration,deployment['id'])
            # Capture old counters before changing peers/runtime where possible.
            if actual.applied_revision is not None:
                await self._collect(conn, await self.agents.traffic(registration,deployment['id']))
            if actual.applied_revision != desired.revision or actual.applied_digest != content_digest(desired) or not actual.healthy:
                result = await self.agents.validate(registration,desired)
                if not result.valid or result.digest != content_digest(desired):
                    raise AgentError('Desired revision rejected')
                self.metrics['apply'] += 1
                actual = await self.agents.apply(registration,desired,actual.applied_revision)
            if actual.state.value != 'READY' or not actual.healthy or actual.applied_revision != desired.revision or actual.applied_digest != content_digest(desired):
                raise AgentError('Revision not ready')
            async with conn.transaction():
                await conn.execute('UPDATE awg_nodes SET online=true,last_seen_at=now(),capabilities=%s WHERE id=%s',(Jsonb(capabilities.model_dump(mode='json')),registration.node_id))
                await conn.execute('UPDATE awg_deployments SET actual=%s,last_seen_at=now() WHERE id=%s',(Jsonb(actual.model_dump(mode='json')),deployment['id']))
                removed = await rows(conn,'SELECT p.id FROM awg_peers p JOIN awg_tombstones t ON t.peer_id=p.id WHERE p.deployment_id=%s AND NOT p.present AND t.acknowledged_at IS NULL',(deployment['id'],))
                for peer in removed:
                    await conn.execute('UPDATE awg_tombstones SET acknowledged_at=now() WHERE peer_id=%s',(peer['id'],))
                    await conn.execute("UPDATE awg_ip_allocations SET peer_id=NULL,released_by=%s,release_after=now()+(%s * interval '1 second') WHERE peer_id=%s",(peer['id'],self.settings.quarantine_seconds,peer['id']))
            await self._collect(conn, await self.agents.traffic(registration,deployment['id']))
        except Exception:
            self.metrics['apply_errors'] += 1
            await conn.execute('UPDATE awg_nodes SET online=false WHERE id=%s',(registration.node_id,))
            await self.error('AGENT_RECONCILE_FAILED',registration.node_id)

    async def _collect(self, conn, snapshot):
        async with conn.transaction():
            epoch = await one(conn,'SELECT * FROM awg_counter_epochs WHERE deployment_id=%s AND epoch=%s',(snapshot.deployment_id,snapshot.runtime_epoch))
            if epoch and (epoch['retired'] or snapshot.sequence <= epoch['sequence']):
                return
            if not epoch:
                latest = await one(conn,'SELECT max(observed_at) AS seen FROM awg_counter_epochs WHERE deployment_id=%s',(snapshot.deployment_id,))
                if latest['seen'] is not None and snapshot.observed_at <= latest['seen']:
                    return
                await conn.execute('UPDATE awg_counter_epochs SET retired=true WHERE deployment_id=%s',(snapshot.deployment_id,))
                await conn.execute('INSERT INTO awg_counter_epochs(deployment_id,epoch,sequence,observed_at) VALUES(%s,%s,%s,%s)',(snapshot.deployment_id,snapshot.runtime_epoch,snapshot.sequence,snapshot.observed_at))
            else:
                await conn.execute('UPDATE awg_counter_epochs SET sequence=%s,observed_at=%s WHERE deployment_id=%s AND epoch=%s',(snapshot.sequence,snapshot.observed_at,snapshot.deployment_id,snapshot.runtime_epoch))
            seen = set()
            for traffic in snapshot.peers:
                if traffic.peer_id in seen:
                    raise ValueError('Duplicate traffic peer')
                seen.add(traffic.peer_id)
                peer = await one(conn,'SELECT p.*,u.public_key,u.key_generation,u.reset_at FROM awg_peers p JOIN awg_users u ON u.id=p.user_id WHERE p.id=%s AND p.deployment_id=%s',(traffic.peer_id,snapshot.deployment_id))
                if not peer or peer['public_key'] != traffic.public_key or peer['key_generation'] != traffic.key_generation:
                    continue
                counter_epoch = traffic.counter_epoch or UUID(int=0)
                previous = await one(conn,'SELECT * FROM awg_peer_counters WHERE peer_id=%s AND epoch=%s AND generation=%s AND counter_epoch=%s',(traffic.peer_id,snapshot.runtime_epoch,traffic.key_generation,counter_epoch))
                rx,tx = int(traffic.rx_bytes),int(traffic.tx_bytes)
                drx = counter_delta(int(previous['rx']) if previous else 0,rx,not previous)
                dtx = counter_delta(int(previous['tx']) if previous else 0,tx,not previous)
                await conn.execute('''INSERT INTO awg_peer_counters(peer_id,epoch,generation,rx,tx,counter_epoch) VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(peer_id,epoch,generation,counter_epoch) DO UPDATE SET rx=EXCLUDED.rx,tx=EXCLUDED.tx''',
                    (traffic.peer_id,snapshot.runtime_epoch,traffic.key_generation,rx,tx,counter_epoch))
                await conn.execute('UPDATE awg_peers SET rx_total=rx_total+%s,tx_total=tx_total+%s,latest_handshake=%s WHERE id=%s',(drx,dtx,traffic.latest_handshake,traffic.peer_id))
                if peer['reset_at'] is None or snapshot.observed_at > peer['reset_at']:
                    await conn.execute('UPDATE awg_users SET usage=usage+%s WHERE id=%s',(drx+dtx,peer['user_id']))

    async def material(self, token):
        user = await self.upstream.by_short_uuid(token)
        if user is None:
            return None
        output = []
        async with self.db.connect() as conn:
            stored = await one(conn,'SELECT * FROM awg_users WHERE id=%s AND NOT deleted',(user.user_uuid,))
            if stored:
                for row in await rows(conn,'''SELECT p.*,d.desired,d.actual,d.last_seen_at,n.registration,n.online
                    FROM awg_peers p JOIN awg_deployments d ON d.id=p.deployment_id JOIN awg_nodes n ON n.id=d.node_id
                    WHERE p.user_id=%s AND p.present''',(user.user_uuid,)):
                    if not row['desired'] or not row['actual'] or not row['last_seen_at'] or not row['online']:
                        continue
                    desired = DesiredDeployment.model_validate(row['desired'])
                    if not any(p.peer_id == row['id'] and p.public_key == stored['public_key'] and p.key_generation == stored['key_generation'] for p in desired.peers):
                        continue
                    actual = ActualDeployment.model_validate(row['actual'])
                    profile = desired.profile
                    if (not NodeRegistration.model_validate(row['registration']).enabled or not profile.enabled or
                        not eligible(user,profile.access.squad_ids,profile.access.user_ids,int(stored['usage']),self.settings.quota_policy) or
                        datetime.now(timezone.utc)-row['last_seen_at'] > timedelta(seconds=self.settings.ready_max_age) or
                        actual.state.value != 'READY' or not actual.healthy or actual.applied_revision != desired.revision or
                        actual.applied_digest != content_digest(desired) or not actual.server_public_key):
                        continue
                    output.append(SubscriptionPeer(profile_id=profile.profile_id,node_id=desired.node_id,name=profile.name,
                        endpoint=profile.endpoint,server_public_key=actual.server_public_key,
                        client_private_key=self.vault.decrypt(str(user.user_uuid),stored['encrypted_key']),
                        ipv4_address=row['ipv4'],ipv6_address=row['ipv6'],dns_servers=profile.dns_servers,
                        allowed_ips=profile.network.client_allowed_ips,mtu=profile.network.mtu,protocol=profile.protocol))
        return SubscriptionMaterial(user_uuid=user.user_uuid,peers=output)
