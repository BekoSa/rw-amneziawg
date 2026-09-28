import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from awg_contracts import (AWGProfile, NodeRegistration, AgentCapabilities, ProtocolCapability,
    ActualDeployment, ValidationResult, TrafficSnapshot, PeerTraffic, content_digest)
from awg_controller.db import Database
from awg_controller.service import Controller, one, rows
from remnawave_client import User, UpstreamError


class Upstream:
    def __init__(self, user):
        self.current = user
        self.fail = False
    async def users(self):
        if self.fail:
            raise UpstreamError()
        return {self.current.user_uuid:self.current} if self.current else {}
    async def user(self, user_id):
        return self.current
    async def by_short_uuid(self, value):
        return self.current if self.current and value == self.current.subscription_id else None


class Agent:
    def __init__(self, node):
        self.node = node
        self.desired = {}
        self.offline = False
        self.epoch = uuid4()
        self.sequence = 0
    async def capabilities(self, registration):
        if self.offline:
            raise RuntimeError('offline')
        return AgentCapabilities(node_id=self.node,agent_version='test',runtime='userspace',protocols=[ProtocolCapability(adapter_id='awg-v1',versions=['1'])])
    async def validate(self, registration, desired):
        return ValidationResult(valid=True,revision=desired.revision,digest=content_digest(desired))
    async def state(self, registration, id):
        if id not in self.desired:
            return ActualDeployment(deployment_id=id)
        desired = self.desired[id]
        return ActualDeployment(deployment_id=id,desired_revision=desired.revision,validated_revision=desired.revision,
            applied_revision=desired.revision,last_known_good_revision=desired.revision,applied_digest=content_digest(desired),
            state='READY',healthy=True,server_public_key='a'*44)
    async def apply(self, registration, desired, expected):
        self.desired[desired.deployment_id] = desired
        return await self.state(registration,desired.deployment_id)
    async def traffic(self,registration,id):
        self.sequence += 1
        return TrafficSnapshot(node_id=self.node,deployment_id=id,runtime_epoch=self.epoch,sequence=self.sequence,peers=[])


@pytest.mark.asyncio
async def test_lifecycle_offline_tombstone_restore_and_fail_conservative():
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('Docker PostgreSQL required')
    node, profile_id, user_id, squad = [uuid4() for _ in range(4)]
    user = User(user_uuid=user_id,username='alice',status='ACTIVE',expire_at=datetime.now(timezone.utc)+timedelta(days=1),
        squad_ids=[squad],traffic_used_bytes=0,traffic_limit_bytes=1000,subscription_id='token')
    settings = SimpleNamespace(encryption_key=b'k'*32,quota_policy='combined',ready_max_age=90,quarantine_seconds=300)
    db = Database(os.environ['TEST_DATABASE_URL'])
    await db.migrate()
    upstream, agents = Upstream(user), Agent(node)
    controller = Controller(db,upstream,agents,settings)
    profile = AWGProfile(profile_id=profile_id,name='test',endpoint={'host':'test.invalid','port':51820},
        network={'ipv4_pool':'10.72.0.0/24','server_ipv4':'10.72.0.1'},protocol={'adapter_id':'awg-v1','version':'1'},
        access={'squad_ids':[squad]},node_ids=[node])
    await controller.register_node(NodeRegistration(node_id=node,name='test',management_url='https://agent.invalid'))
    await controller.save_profile(profile)
    with pytest.raises(ValueError):
        await controller.apply_profile(profile_id, content_digest(profile))
    assert (await controller.validate_profile(profile_id))['valid']
    await controller.apply_profile(profile_id, content_digest(profile))
    await controller.reconcile()
    material = await controller.material('token')
    assert len(material.peers) == 1
    address = material.peers[0].ipv4_address
    first = agents.desired.copy()
    await controller.reconcile()
    assert agents.desired == first
    upstream.fail = True
    with pytest.raises(UpstreamError):
        await controller.reconcile()
    assert agents.desired == first
    upstream.fail = False
    user.status = 'DISABLED'
    await controller.reconcile()
    assert not (await controller.material('token')).peers
    user.status = 'ACTIVE'
    await controller.reconcile()
    restored = (await controller.material('token')).peers
    assert len(restored) == 1
    assert restored[0].ipv4_address == address, 're-enabled peer reclaims its quarantined address'
    old_key = next(iter(agents.desired.values())).peers[0].public_key
    await controller.rotate_user_key(user_id)
    await controller.reconcile()
    rotated = next(iter(agents.desired.values()))
    assert rotated.peers[0].public_key != old_key and rotated.peers[0].key_generation == 2
    assert old_key in rotated.revoked_public_keys
    assert rotated.peers[0].ipv4_address == address, 'rotation keeps the address'
    assert (await controller.material('token')).peers[0].client_private_key.get_secret_value() != restored[0].client_private_key.get_secret_value()
    agents.offline = True
    upstream.current = None
    await controller.reconcile()
    async with db.connect() as conn:
        tombstones = await rows(conn,'SELECT t.* FROM awg_tombstones t JOIN awg_peers p ON p.id=t.peer_id WHERE p.user_id=%s',(user_id,))
        assert len(tombstones) == 1 and tombstones[0]['acknowledged_at'] is None
    agents.offline = False
    await controller.reconcile()
    assert all(not d.peers for d in agents.desired.values())
    async with db.connect() as conn:
        tombstone = await one(conn,'SELECT t.* FROM awg_tombstones t JOIN awg_peers p ON p.id=t.peer_id WHERE p.user_id=%s',(user_id,))
        assert tombstone['acknowledged_at'] is not None
        assert (await one(conn,'SELECT count(*) AS n FROM awg_ip_allocations WHERE profile_id=%s AND release_after>now()',(profile_id,)))['n'] > 0


@pytest.mark.asyncio
async def test_auth_separation_and_material_rejects_uuid_injection():
    import httpx
    from awg_controller.app import create_app
    controller = SimpleNamespace(settings=SimpleNamespace(admin_token='admin-test',gateway_token='gateway-test'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(controller)),base_url='http://controller') as client:
        assert (await client.get('/api/v1/nodes')).status_code == 401
        assert (await client.get('/api/v1/nodes',headers={'Authorization':'Bearer gateway-test'})).status_code == 401
        response = await client.post('/v1/internal/subscriptions/material',headers={'Authorization':'Bearer admin-test'},json={'subscription_token':'token'})
        assert response.status_code == 401
        response = await client.post('/v1/internal/subscriptions/material',headers={'Authorization':'Bearer gateway-test'},json={'subscription_token':'token','user_uuid':str(uuid4())})
        assert response.status_code == 422
        assert 'token' not in response.text



@pytest.mark.asyncio
async def test_admin_routes_only_from_extension_network(monkeypatch):
    import httpx
    from awg_controller.app import create_app
    monkeypatch.setenv('AWG_ADMIN_ALLOWED_CIDRS', '10.99.0.0/24')
    controller = SimpleNamespace(settings=SimpleNamespace(admin_token='admin-test', gateway_token='gateway-test', webhook_secret='s'))
    app = create_app(controller)
    outside = httpx.ASGITransport(app=app, client=('172.30.0.5', 1234))
    async with httpx.AsyncClient(transport=outside, base_url='http://controller') as client:
        assert (await client.get('/api/v1/nodes', headers={'Authorization': 'Bearer admin-test'})).status_code == 404
        assert (await client.post('/v1/internal/subscriptions/material', json={})).status_code == 404
        assert (await client.post('/webhooks/remnawave', content=b'{}')).status_code == 401, 'webhooks stay reachable'
    inside = httpx.ASGITransport(app=app, client=('10.99.0.7', 1234))
    async with httpx.AsyncClient(transport=inside, base_url='http://controller') as client:
        assert (await client.get('/api/v1/nodes')).status_code == 401, 'token still required inside'
