"""TT controller behavior; PostgreSQL remains an isolated container dependency."""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from awg_contracts import (AgentCapabilities, ProtocolCapability, NodeRegistration,
    TrustTunnelProfile, TrustTunnelDesired, TrustTunnelActual, TrustTunnelValidationResult,
    TrustTunnelCredential, TrustTunnelExportResponse, trusttunnel_digest)
from awg_controller.app import create_app
from awg_controller.agent_client import AgentClient, AgentError
from awg_controller.db import Database
from awg_controller.service import Controller, rows, one
from remnawave_client import User, UpstreamError


def profile(node, squad):
    return TrustTunnelProfile(profile_id=uuid4(), name='TT test', node_ids=[node],
        endpoint={'host':'vpn.example.org', 'port':8443}, access={'squad_ids':[squad]},
        tls={'domain':'vpn.example.org', 'email':'operator@example.org'})


class Upstream:
    def __init__(self, user):
        self.current = user
        self.fail = False
    async def users(self):
        if self.fail:
            raise UpstreamError()
        return {self.current.user_uuid:self.current} if self.current else {}
    async def user(self, identity):
        if self.current and str(identity) in {str(self.current.user_uuid), str(self.current.upstream_id)}:
            return self.current
        return None
    async def by_short_uuid(self, token):
        return self.current if self.current and token == 'stock-token' else None


class Agent:
    """External management boundary only; durable Controller/SQL are real."""
    def __init__(self, node):
        self.node = node
        self.desired = {}
        self.supported = True
        self.issuing = False
        self.expired = False
        self.offline = False
    async def capabilities(self, registration):
        if self.offline:
            raise AgentError()
        protocols = [ProtocolCapability(adapter_id='trusttunnel', versions=['1.0'])] if self.supported else []
        return AgentCapabilities(node_id=self.node, agent_version='test', runtime='userspace', protocols=protocols)
    async def trusttunnel_validate(self, registration, desired):
        return TrustTunnelValidationResult(valid=True, revision=desired.revision, digest=trusttunnel_digest(desired))
    async def trusttunnel_state(self, registration, identity):
        if self.offline:
            raise AgentError()
        if identity not in self.desired:
            return TrustTunnelActual(deployment_id=identity)
        d = self.desired[identity]
        now = datetime.now(timezone.utc)
        pending = self.issuing or self.expired
        return TrustTunnelActual(deployment_id=identity, desired_revision=d.revision,
            validated_revision=d.revision, applied_revision=None if pending else d.revision,
            applied_digest=None if pending else trusttunnel_digest(d), state='APPLYING' if pending else 'READY',
            healthy=not pending, certificate_state='ISSUING' if self.issuing else 'EXPIRED' if self.expired else 'VALID',
            certificate_generation=1, certificate_environment='production',
            not_after=now+timedelta(days=-1 if self.expired else 30), fingerprint='f'*64,
            served_fingerprint='f'*64)
    async def trusttunnel_apply(self, registration, desired, expected):
        self.desired[desired.deployment_id] = desired
        return await self.trusttunnel_state(registration, desired.deployment_id)
    async def trusttunnel_export(self, registration, deployment_id, peer_id, format):
        assert peer_id in [c.peer_id for c in self.desired[deployment_id].clients]
        return TrustTunnelExportResponse(peer_id=peer_id, format=format,
            content='tt://?native-fixture' if format == 'deeplink' else 'native-toml-fixture')


@pytest.mark.asyncio
async def test_tt_api_auth_and_arbitrary_uuid_denied():
    c = SimpleNamespace(settings=SimpleNamespace(admin_token='admin', gateway_token='gateway'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url='http://controller') as client:
        assert (await client.get('/api/v1/trusttunnel/profiles')).status_code == 401
        assert (await client.get('/api/v1/trusttunnel/profiles', headers={'Authorization':'Bearer gateway'})).status_code == 401
        assert (await client.post('/v1/internal/subscriptions/trusttunnel/material',
            headers={'Authorization':'Bearer admin'}, json={'subscription_token':'stock-token'})).status_code == 401
        response = await client.post('/v1/internal/subscriptions/trusttunnel/material',
            headers={'Authorization':'Bearer gateway'}, json={'subscription_token':'stock-token','user_uuid':str(uuid4())})
        assert response.status_code == 422
        assert 'stock-token' not in response.text
        assert (await client.post(f'/api/v1/trusttunnel/peers/{uuid4()}/export',
            json={'format':'deeplink'})).status_code == 401


@pytest.mark.asyncio
async def test_tt_agent_wire_uses_real_password_and_typed_missing_state():
    node, peer, user = uuid4(), uuid4(), uuid4()
    desired = TrustTunnelDesired(deployment_id=uuid4(), node_id=node, revision=1,
        profile=profile(node, uuid4()), clients=[TrustTunnelCredential(peer_id=peer, user_uuid=user,
            username='opaque-user', password='s'*43)])
    def transport(request):
        if request.method == 'GET':
            return httpx.Response(404)
        body = json.loads(request.content)
        assert body['clients'][0]['password'] == 's'*43
        return httpx.Response(200, json={'valid':True, 'revision':1,'digest':trusttunnel_digest(desired)})
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    registration = NodeRegistration(node_id=node, name='test', management_url='https://agent.invalid')
    try:
        assert (await client.trusttunnel_validate(registration, desired)).valid
        actual = await client.trusttunnel_state(registration, desired.deployment_id)
        assert isinstance(actual, TrustTunnelActual) and actual.applied_revision is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tt_controller_client_matches_actual_agent_routes(tmp_path):
    from awg_agent.api import create_app as create_agent_app
    from tests.unit.agent.test_agent import service as create_awg_service
    from tests.unit.agent.test_trusttunnel import service as create_tt_service, desired
    awg = create_awg_service(tmp_path / 'awg')
    tt = create_tt_service(tmp_path / 'tt')
    first = desired()
    registration = NodeRegistration(node_id=first.node_id, name='test', management_url='https://agent.invalid')
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_agent_app(awg, trusttunnel=tt)))
    try:
        assert (await client.trusttunnel_state(registration, first.deployment_id)).applied_revision is None
        assert (await client.trusttunnel_validate(registration, first)).valid
        assert (await client.trusttunnel_apply(registration, first, None)).state == 'READY'
        actual = await client.trusttunnel_state(registration, first.deployment_id)
        assert actual.applied_digest == trusttunnel_digest(first)
        assert tt.runtime.current.clients[0].password.get_secret_value() == 'secret'*8
        exported = await client.trusttunnel_export(registration, first.deployment_id, first.clients[0].peer_id, 'deeplink')
        assert exported.content.get_secret_value() == 'native secret'
    finally:
        await client.close()
        tt.close()
        awg.close()


@pytest.fixture
async def setup_tt():
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('Docker PostgreSQL required')
    node, user_id, squad = uuid4(), uuid4(), uuid4()
    user = User(user_uuid=user_id, username='alice', status='ACTIVE',
        expire_at=datetime.now(timezone.utc)+timedelta(days=1), squad_ids=[squad],
        traffic_used_bytes=0, traffic_limit_bytes=1000, subscription_id='stock-token')
    settings = SimpleNamespace(encryption_key=b'k'*32, quota_policy='combined',
        ready_max_age=90, quarantine_seconds=300, admin_token='admin', gateway_token='gateway')
    db = Database(os.environ['TEST_DATABASE_URL'])
    await db.migrate()
    upstream, agent = Upstream(user), Agent(node)
    controller = Controller(db, upstream, agent, settings)
    await controller.register_node(NodeRegistration(node_id=node, name='test', management_url='https://agent.invalid'))
    p = profile(node, squad)
    try:
        yield controller, upstream, agent, p
    finally:
        async with db.connect() as conn:
            await conn.execute('DELETE FROM tt_tombstones WHERE peer_id IN (SELECT p.id FROM tt_peers p JOIN tt_deployments d ON d.id=p.deployment_id WHERE d.profile_id=%s)', (p.profile_id,))
            await conn.execute('DELETE FROM tt_peers WHERE deployment_id IN (SELECT id FROM tt_deployments WHERE profile_id=%s)', (p.profile_id,))
            await conn.execute('DELETE FROM tt_revisions WHERE deployment_id IN (SELECT id FROM tt_deployments WHERE profile_id=%s)', (p.profile_id,))
            await conn.execute('DELETE FROM tt_deployments WHERE profile_id=%s', (p.profile_id,))
            await conn.execute('DELETE FROM tt_profiles WHERE id=%s', (p.profile_id,))
            await conn.execute('DELETE FROM tt_credentials WHERE user_id=%s', (user_id,))
            await conn.execute('DELETE FROM awg_users WHERE id=%s', (user_id,))
            await conn.execute('DELETE FROM awg_nodes WHERE id=%s', (node,))


async def activate(c, p):
    await c.trusttunnel.save_profile(p)
    result = await c.trusttunnel.validate_profile(p.profile_id)
    assert result['valid']
    await c.trusttunnel.apply_profile(p.profile_id, result['draft_digest'])
    await c.reconcile()


@pytest.mark.asyncio
async def test_tt_stable_credentials_encrypted_desired_and_rotation(setup_tt):
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    first = await c.trusttunnel.material('stock-token')
    assert len(first.entries) == 1
    password = first.entries[0].password.get_secret_value()
    assert len(password) >= 43
    revisions = [d.revision for d in agent.desired.values()]
    await c.reconcile()
    assert [d.revision for d in agent.desired.values()] == revisions
    assert (await c.trusttunnel.material('stock-token')).entries[0].password.get_secret_value() == password
    async with c.db.connect() as conn:
        records = await rows(conn, 'SELECT row_to_json(t) AS data FROM tt_credentials t WHERE user_id=%s', (upstream.current.user_uuid,))
        records += await rows(conn, 'SELECT row_to_json(t) AS data FROM tt_revisions t WHERE deployment_id IN (SELECT id FROM tt_deployments WHERE profile_id=%s)', (p.profile_id,))
        assert password not in json.dumps(records, default=str)
    native = await c.trusttunnel.material('stock-token', native_format='all')
    assert native.entries[0].native_deeplink.get_secret_value() == 'tt://?native-fixture'
    assert native.entries[0].native_toml.get_secret_value() == 'native-toml-fixture'
    await c.trusttunnel.rotate_credentials(upstream.current.user_uuid)
    assert not (await c.trusttunnel.material('stock-token')).entries, 'rotation excludes still-applied old credentials'
    await c.reconcile()
    rotated = (await c.trusttunnel.material('stock-token')).entries[0]
    assert rotated.password.get_secret_value() != password and rotated.credential_generation == 2
    assert rotated.username == first.entries[0].username and rotated.peer_id == first.entries[0].peer_id


@pytest.mark.asyncio
async def test_tt_save_does_not_apply_and_older_agent_rejected(setup_tt):
    c, upstream, agent, p = setup_tt
    await c.trusttunnel.save_profile(p)
    await c.reconcile()
    assert not agent.desired
    with pytest.raises(ValueError):
        await c.trusttunnel.apply_profile(p.profile_id, 'f'*64)
    agent.supported = False
    with pytest.raises((ValueError, AgentError)):
        await c.trusttunnel.validate_profile(p.profile_id)
    assert not agent.desired


@pytest.mark.asyncio
async def test_tt_lifecycle_incomplete_snapshot_and_nonblocking_issuance(setup_tt):
    c, upstream, agent, p = setup_tt
    agent.issuing = True
    await activate(c, p)
    assert not (await c.trusttunnel.material('stock-token')).entries
    agent.issuing = False
    await c.reconcile()
    assert len((await c.trusttunnel.material('stock-token')).entries) == 1
    first = agent.desired.copy()
    upstream.fail = True
    with pytest.raises(UpstreamError):
        await c.reconcile()
    assert agent.desired == first, 'incomplete upstream must not infer deletion'
    upstream.fail = False
    upstream.current.squad_ids = []
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())
    upstream.current.squad_ids = p.access.squad_ids
    await c.reconcile()
    assert len((await c.trusttunnel.material('stock-token')).entries) == 1
    upstream.current.expire_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())
    upstream.current.expire_at = datetime.now(timezone.utc)+timedelta(days=1)
    await c.reconcile()
    upstream.current.status = 'DISABLED'
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())
    upstream.current.status = 'ACTIVE'
    await c.reconcile()
    upstream.current = None
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())
    async with c.db.connect() as conn:
        tombstones = await rows(conn, 'SELECT * FROM tt_tombstones t JOIN tt_peers p ON p.id=t.peer_id WHERE p.deployment_id IN (SELECT id FROM tt_deployments WHERE profile_id=%s)', (p.profile_id,))
        assert tombstones and all(t['acknowledged_at'] for t in tombstones)


@pytest.mark.asyncio
async def test_tt_certificate_expiry_node_disable_and_detachment(setup_tt):
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    agent.expired = True
    await c.reconcile()
    assert not (await c.trusttunnel.material('stock-token')).entries
    agent.expired = False
    await c.register_node(NodeRegistration(node_id=agent.node, name='test', management_url='https://agent.invalid', enabled=False))
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())
    await c.register_node(NodeRegistration(node_id=agent.node, name='test', management_url='https://agent.invalid'))
    await c.reconcile()
    p.node_ids = []
    p.enabled = False
    await c.trusttunnel.save_profile(p)
    result = await c.trusttunnel.validate_profile(p.profile_id)
    assert result['valid'], 'detaching must validate revocation against the old node'
    await c.trusttunnel.apply_profile(p.profile_id, result['draft_digest'])
    await c.reconcile()
    desired = next(iter(agent.desired.values()))
    assert not desired.profile.enabled and desired.profile.node_ids == [agent.node] and not desired.clients


@pytest.mark.asyncio
async def test_tt_cached_outage_expiry_and_offline_deletion_ack(setup_tt):
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    # Cache knowledge remains authoritative during outage. Expiry is a known fact,
    # never an inference from missing users on a partial API response.
    async with c.db.connect() as conn:
        cache = await one(conn, 'SELECT upstream FROM awg_users WHERE id=%s', (upstream.current.user_uuid,))
        cache['upstream']['expire_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        from psycopg.types.json import Jsonb
        await conn.execute('UPDATE awg_users SET upstream=%s WHERE id=%s',
            (Jsonb(cache['upstream']), upstream.current.user_uuid))
    upstream.fail = True
    await c.trusttunnel.reconcile_cached()
    assert all(not d.clients for d in agent.desired.values())
    upstream.fail = False
    await c.reconcile()
    assert any(d.clients for d in agent.desired.values())
    agent.offline = True
    upstream.current = None
    await c.reconcile()
    async with c.db.connect() as conn:
        pending = await rows(conn, '''SELECT t.* FROM tt_tombstones t JOIN tt_peers p ON p.id=t.peer_id
            WHERE p.deployment_id IN (SELECT id FROM tt_deployments WHERE profile_id=%s)''', (p.profile_id,))
        assert pending and all(t['acknowledged_at'] is None for t in pending)
    agent.offline = False
    await c.reconcile()
    assert all(not d.clients for d in agent.desired.values())


@pytest.mark.asyncio
async def test_tt_material_wire_no_admin_secrets_and_ownership_aead(setup_tt):
    from cryptography.exceptions import InvalidTag
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url='http://controller') as client:
        response = await client.post('/v1/internal/subscriptions/trusttunnel/material?native_format=all',
            headers={'Authorization':'Bearer gateway'}, json={'subscription_token':'stock-token'})
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        password = response.json()['entries'][0]['password']
        assert len(password) >= 43 and password != '**********'
        for path in ('profiles','status','peers'):
            metadata = await client.get('/api/v1/trusttunnel/'+path, headers={'Authorization':'Bearer admin'})
            assert metadata.status_code == 200 and password not in metadata.text
        assert (await client.post('/v1/internal/subscriptions/trusttunnel/material',
            headers={'Authorization':'Bearer gateway'}, json={'subscription_token':str(upstream.current.user_uuid)})).status_code == 404
    async with c.db.connect() as conn:
        record = await one(conn, 'SELECT * FROM tt_credentials WHERE user_id=%s', (upstream.current.user_uuid,))
        deployment = await one(conn, 'SELECT * FROM tt_deployments WHERE profile_id=%s', (p.profile_id,))
        with pytest.raises(InvalidTag):
            c.trusttunnel.vault.decrypt(f'trusttunnel:{uuid4()}:1', record['encrypted_password'])
        with pytest.raises(InvalidTag):
            c.trusttunnel.vault.decrypt(f'trusttunnel:desired:{deployment["id"]}:999', deployment['encrypted_desired'])


@pytest.mark.asyncio
async def test_tt_initial_applying_is_polled_without_duplicate_apply(setup_tt):
    c, upstream, agent, p = setup_tt
    agent.issuing = True
    await activate(c, p)
    # A duplicate apply while issuance is already accepted is an observable failure.
    async def reject_duplicate(*args):
        raise AssertionError('must poll accepted initial issuance')
    agent.trusttunnel_apply = reject_duplicate
    await c.reconcile()
    state = (await c.trusttunnel.status(p.profile_id))['items'][0]
    assert state['last_error_code'] is None and state['actual']['state'] == 'APPLYING'


@pytest.mark.asyncio
async def test_tt_admin_explicit_export_rejects_disabled_or_stale_peer(setup_tt):
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    peer = (await c.trusttunnel.material('stock-token')).entries[0].peer_id
    path = f'/api/v1/trusttunnel/peers/{peer}/export'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url='http://controller') as client:
        assert (await client.post(path, json={'format':'deeplink'})).status_code == 401
        assert (await client.post(path, headers={'Authorization':'Bearer gateway'}, json={'format':'deeplink'})).status_code == 401
        response = await client.post(path, headers={'Authorization':'Bearer admin'}, json={'format':'deeplink'})
        assert response.status_code == 200
        assert response.json()['content'] == 'tt://?native-fixture'
        upstream.current.status = 'DISABLED'
        response = await client.post(path, headers={'Authorization':'Bearer admin'}, json={'format':'toml'})
        assert response.status_code == 409 and 'native-toml-fixture' not in response.text
        upstream.current.status = 'ACTIVE'
        await c.trusttunnel.rotate_credentials(upstream.current.user_uuid)
        response = await client.post(path, headers={'Authorization':'Bearer admin'}, json={'format':'deeplink'})
        assert response.status_code == 409 and 'tt://' not in response.text


@pytest.mark.asyncio
async def test_tt_stale_cached_snapshot_cannot_resurrect_authoritative_revoke(setup_tt):
    from psycopg.types.json import Jsonb
    c, upstream, agent, p = setup_tt
    await activate(c, p)
    stale_active = upstream.current.model_copy(deep=True)
    peer_id = (await c.trusttunnel.material('stock-token')).entries[0].peer_id
    upstream.current.status = 'DISABLED'
    # Authoritative cache update happens before the older outage reconcile is
    # allowed to proceed. It must never apply stale credentials again.
    async with c.db.connect() as conn:
        await conn.execute('UPDATE awg_users SET upstream=%s WHERE id=%s',
            (Jsonb(upstream.current.model_dump(mode='json')), upstream.current.user_uuid))
    upstream.current = stale_active
    assert not (await c.trusttunnel.material('stock-token')).entries, 'fresh cache revoke dominates stale stock auth response'
    with pytest.raises(ValueError):
        await c.trusttunnel.export_peer(peer_id, 'deeplink')
    await c.trusttunnel.reconcile({stale_active.user_uuid:stale_active})
    desired = next(iter(agent.desired.values()))
    assert not desired.clients, 'cache revoke must dominate an earlier ACTIVE snapshot'
