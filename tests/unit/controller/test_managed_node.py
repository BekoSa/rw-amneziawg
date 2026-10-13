from uuid import uuid4
import httpx
import pytest
from awg_contracts import NodeRegistration
from awg_controller.agent_client import AgentClient, AgentError
from awg_contracts.managed_node import (NodeCapabilities, NodeInstallEnvelope, NodeActualState,
    NodeBindingActual, NodeValidationResult, NodeApplyRequest, node_digest)


@pytest.fixture(autouse=True)
async def clean_managed_database():
    yield
    import os
    if os.environ.get('TEST_DATABASE_URL'):
        from awg_controller.db import Database
        async with Database(os.environ['TEST_DATABASE_URL']).connect() as conn:
            exists = await (await conn.execute("SELECT to_regclass('awg_nodes') AS relation")).fetchone()
            if exists['relation']:
                await conn.execute('TRUNCATE awg_nodes,awg_profiles,awg_users,tt_profiles CASCADE')


@pytest.mark.asyncio
async def test_managed_negotiation_404_only_is_legacy():
    registration = NodeRegistration(node_id=uuid4(), name='legacy', management_url='https://agent.invalid')
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    assert await client.node_capabilities(registration) is None
    await client.close()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(AgentError):
        await client.node_capabilities(registration)
    await client.close()


def test_resource_binding_ids_and_backend_allocation_are_stable():
    from awg_controller.managed_node import binding_id, allocate_awg_backend
    deployment = uuid4()
    assert binding_id(deployment, 'awg', 'udp', '0.0.0.0') == binding_id(deployment, 'awg', 'udp', '0.0.0.0')
    assert binding_id(deployment, 'awg', 'udp', '::') != binding_id(deployment, 'awg', 'udp', '0.0.0.0')
    assert allocate_awg_backend({20000, 20001}) == 20002
    with pytest.raises(ValueError, match='BACKEND_PORT_EXHAUSTED'):
        allocate_awg_backend(set(range(20000, 30000)))


@pytest.mark.asyncio
async def test_unknown_node_api_is_not_silently_treated_as_legacy():
    registration = NodeRegistration(node_id=uuid4(), name='unknown', management_url='https://agent.invalid')
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'api_version':'node.remnawave-extension.io/v9', 'protocol_version':'9.0'})))
    with pytest.raises(AgentError) as caught:
        await client.node_capabilities(registration)
    assert caught.value.reason == 'UNSUPPORTED_NODE_API'
    await client.close()


class ManagedAgent:
    """Remote boundary double; controller state and resource revisions use real SQL."""
    def __init__(self, node):
        from tests.unit.controller.test_service import Agent
        from tests.unit.controller.test_trusttunnel import Agent as TTAgent
        self.awg = Agent(node)
        self.tt = TTAgent(node)
        self.node = node
        self.resources = None
        self.failure = False
        self.busy443 = False
        self.calls = []
        self.installed = NodeCapabilities(node_id=node,
            envelope=NodeInstallEnvelope(node_id=node,envelope_id=uuid4(),backend_ipv4='10.244.0.2',
                bind_addresses=['0.0.0.0'],reserved_ports=[8443,2525]),
            installed_protocols=[{'adapter_id':'amneziawg-go-v3','versions':['2']},
                                 {'adapter_id':'trusttunnel','versions':['1.0']}])

    def __getattr__(self, name):
        return getattr(self.tt if name.startswith('trusttunnel_') else self.awg, name)

    async def capabilities(self, registration):
        from awg_contracts import AgentCapabilities,ProtocolCapability
        return AgentCapabilities(node_id=self.node,agent_version='test',runtime='userspace',
            protocols=[ProtocolCapability(adapter_id='awg-v1',versions=['1']),
                       ProtocolCapability(adapter_id='trusttunnel',versions=['1.0'])])

    async def node_capabilities(self, registration):
        return self.installed

    async def node_validate(self, registration, desired):
        rejected = self.failure or self.busy443 and any(binding.public_port == 443 for binding in desired.bindings)
        return NodeValidationResult(valid=not rejected,revision=desired.revision,digest=node_digest(desired),
            issues=[{'code':'PORT_IN_USE','message':'Listener unavailable'}] if rejected else [])

    async def node_apply(self, registration, desired, expected):
        self.calls.append('resources')
        if self.failure:
            raise AgentError('Listener unavailable','PORT_IN_USE')
        assert expected == (self.resources.revision if self.resources else None)
        self.resources = desired
        return await self.node_state(registration)

    async def apply(self, registration, desired, expected):
        self.calls.append('protocol')
        assert self.resources is not None
        return await self.awg.apply(registration,desired,expected)

    async def trusttunnel_apply(self, registration, desired, expected):
        self.calls.append('tt-protocol')
        if desired.profile.enabled:
            assert any(binding.deployment_id==desired.deployment_id and binding.backend_role=='acme-http'
                       for binding in self.resources.bindings)
        return await self.tt.trusttunnel_apply(registration,desired,expected)

    async def node_state(self, registration):
        if self.resources is None:
            return NodeActualState(node_id=self.node)
        return NodeActualState(node_id=self.node,desired_revision=self.resources.revision,
            accepted_revision=self.resources.revision,applied_revision=self.resources.revision,
            applied_digest=node_digest(self.resources),state='APPLYING',healthy=False,
            bindings=[NodeBindingActual(binding=binding,state='ACTIVE',backend_ready=
                (binding.deployment_id in self.awg.desired and binding.backend_role=='awg') or
                (binding.deployment_id in self.tt.desired and not self.tt.issuing and binding.backend_role!='awg'))
                for binding in self.resources.bindings])


async def setup_managed(bind_addresses=None):
    import os
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from awg_contracts import AWGProfile,content_digest
    from awg_controller.db import Database
    from awg_controller.service import Controller
    from remnawave_client import User
    from tests.unit.controller.test_service import Upstream
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('Docker PostgreSQL required')
    node,user_id = uuid4(),uuid4()
    user = User(user_uuid=user_id,username='managed',status='ACTIVE',
        expire_at=datetime.now(timezone.utc)+timedelta(days=1),subscription_id=str(user_id))
    db = Database(os.environ['TEST_DATABASE_URL'])
    await db.migrate()
    agent = ManagedAgent(node)
    if bind_addresses is not None:
        agent.installed = agent.installed.model_copy(update={'envelope':agent.installed.envelope.model_copy(
            update={'bind_addresses':bind_addresses})})
    settings = SimpleNamespace(encryption_key=b'k'*32,quota_policy='combined',ready_max_age=90,
        quarantine_seconds=300,admin_token='admin',gateway_token='gateway')
    controller = Controller(db,Upstream(user),agent,settings)
    await controller.register_node(NodeRegistration(node_id=node,name='managed',management_url='https://agent.invalid'))
    profile = AWGProfile(profile_id=uuid4(),name='managed',endpoint={'host':'vpn.example.org','port':51820},
        node_ids=[node],network={'ipv4_pool':'10.73.0.0/24','server_ipv4':'10.73.0.1'},
        protocol={'adapter_id':'awg-v1','version':'1'},access={'user_ids':[user_id]})
    await controller.save_profile(profile)
    assert (await controller.validate_profile(profile.profile_id))['valid']
    await controller.apply_profile(profile.profile_id,content_digest(profile))
    return controller,agent,profile,user


@pytest.mark.asyncio
async def test_bootstrap_resources_precede_protocol_apply_and_ready_binding_is_published():
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    assert agent.calls == ['resources','protocol']
    assert len((await controller.material(user.subscription_id)).peers) == 1
    binding = agent.resources.bindings[0]
    assert binding.public_port == 51820
    assert binding.backend_port == 20000
    await controller.reconcile()
    assert agent.calls == ['resources','protocol']


@pytest.mark.asyncio
async def test_failed_resource_apply_withholds_new_endpoint_and_keeps_committed_protocol():
    from awg_contracts import content_digest
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    previous = dict(agent.awg.desired)
    changed = profile.model_copy(update={'endpoint':profile.endpoint.model_copy(update={'port':51821})})
    await controller.save_profile(changed)
    assert (await controller.validate_profile(changed.profile_id))['valid']
    await controller.apply_profile(changed.profile_id,content_digest(changed))
    agent.failure = True
    await controller.reconcile()
    assert agent.awg.desired == previous
    assert (await controller.material(user.subscription_id)).peers == []


@pytest.mark.asyncio
async def test_resource_protocol_toggle_removes_ingress_and_subscription_without_editing_profile():
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    state = await controller.resources.state(agent.node)
    desired = state['desired'].model_copy(update={'revision':state['desired'].revision+1,
        'enabled_protocols':['trusttunnel'],'bindings':[]})
    result = await controller.resources.apply(agent.node,NodeApplyRequest(desired=desired,
        expected_applied_revision=state['actual'].applied_revision))
    assert result['desired'].bindings == []
    assert (await controller.material(user.subscription_id)).peers == []
    await controller.reconcile()
    snapshot = next(iter(agent.awg.desired.values()))
    assert not snapshot.profile.enabled
    assert snapshot.peers == []


@pytest.mark.asyncio
async def test_resource_api_rejects_arbitrary_bindings_and_requires_admin():
    from awg_controller.app import create_app
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(controller)),base_url='http://controller') as client:
        path = f'/api/v1/nodes/{agent.node}/resources'
        assert (await client.get(path)).status_code == 401
        headers = {'Authorization':'Bearer admin'}
        state = (await client.get(path,headers=headers)).json()
        assert state['managed']
        desired = state['desired']
        desired['bindings'][0]['public_port'] = 51999
        assert (await client.post(path+'/validate',headers=headers,json=desired)).status_code == 409


@pytest.mark.asyncio
async def test_active_profile_udp_conflict_never_steals_existing_deployment():
    from awg_contracts import TrustTunnelProfile,content_digest
    from awg_controller.service import one
    from psycopg.types.json import Jsonb
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    tt = TrustTunnelProfile(profile_id=uuid4(),name='conflict',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':51820},tls={'domain':'vpn.example.org','email':'admin@example.org'})
    async with controller.db.connect() as conn:
        await conn.execute('INSERT INTO tt_profiles(id,draft,active) VALUES(%s,%s,%s)',
            (tt.profile_id,Jsonb(tt.model_dump(mode='json')),Jsonb(tt.model_dump(mode='json'))))
        await controller.trusttunnel._deployment(conn,tt.profile_id,agent.node)
    with pytest.raises(ValueError):
        await controller.resources.state(agent.node)
    assert agent.resources.bindings[0].public_port == profile.endpoint.port


@pytest.mark.asyncio
async def test_auto_uses_http80_when_stock443_is_busy_and_keeps_data_port():
    from awg_contracts import TrustTunnelProfile
    from psycopg.types.json import Jsonb
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    tt = TrustTunnelProfile(profile_id=uuid4(),name='auto',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':2443},tls={'domain':'vpn.example.org','email':'admin@example.org'})
    async with controller.db.connect() as conn:
        await conn.execute('INSERT INTO tt_profiles(id,draft,active) VALUES(%s,%s,%s)',
            (tt.profile_id,Jsonb(tt.model_dump(mode='json')),Jsonb(tt.model_dump(mode='json'))))
        await controller.trusttunnel._deployment(conn,tt.profile_id,agent.node)
    agent.busy443 = True
    state = await controller.resources.state(agent.node)
    plan = state['desired']
    assert not any(binding.public_port == 443 for binding in plan.bindings)
    assert any(binding.backend_role == 'acme-http' and binding.public_port == 80 for binding in plan.bindings)
    assert {binding.transport for binding in plan.bindings if binding.public_port == 2443} == {'tcp','udp'}


@pytest.mark.asyncio
async def test_reenable_preview_rebuilds_authoritative_bindings():
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    initial = await controller.resources.state(agent.node)
    preview = await controller.resources.state(agent.node,protocols=[])
    await controller.resources.apply(agent.node,NodeApplyRequest(desired=preview['desired'],
        expected_applied_revision=initial['actual'].applied_revision))
    preview = await controller.resources.state(agent.node,protocols=['amneziawg'])
    assert preview['desired'].bindings[0].public_port == profile.endpoint.port
    assert (await controller.resources.validate(agent.node,preview['desired'])).valid


@pytest.mark.asyncio
async def test_changed_install_envelope_is_rejected():
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    agent.installed = agent.installed.model_copy(update={'envelope':agent.installed.envelope.model_copy(
        update={'backend_ipv4':'10.244.0.3'})})
    with pytest.raises(AgentError) as caught:
        await controller.resources.state(agent.node)
    assert caught.value.reason == 'ENVELOPE_MISMATCH'


@pytest.mark.asyncio
async def test_two_validated_drafts_cannot_apply_same_udp_port():
    from awg_contracts import content_digest
    from awg_controller.service import one
    controller,agent,profile,user = await setup_managed()
    # First profile is active; temporarily reset it to simulate two drafts
    # validated before either activation.
    async with controller.db.connect() as conn:
        await conn.execute('UPDATE awg_profiles SET active=NULL WHERE id=%s',(profile.profile_id,))
    second = profile.model_copy(update={'profile_id':uuid4(),'name':'second',
        'network':profile.network.model_copy(update={'ipv4_pool':'10.74.0.0/24','server_ipv4':'10.74.0.1'})})
    await controller.save_profile(second)
    assert (await controller.validate_profile(second.profile_id))['valid']
    await controller.apply_profile(profile.profile_id,content_digest(profile))
    with pytest.raises(ValueError):
        await controller.apply_profile(second.profile_id,content_digest(second))
    async with controller.db.connect() as conn:
        assert (await one(conn,'SELECT active FROM awg_profiles WHERE id=%s',(second.profile_id,)))['active'] is None


@pytest.mark.asyncio
async def test_failed_remote_apply_keeps_durable_resource_intent_for_retry():
    from awg_controller.service import one
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    preview = await controller.resources.state(agent.node,protocols=[])
    agent.failure = True
    with pytest.raises(AgentError):
        await controller.resources.apply(agent.node,NodeApplyRequest(desired=preview['desired'],
            expected_applied_revision=preview['actual'].applied_revision))
    async with controller.db.connect() as conn:
        row = await one(conn,'SELECT desired,last_error_code FROM node_resources WHERE node_id=%s',(agent.node,))
        assert row['desired']['revision'] == preview['desired'].revision
        assert row['last_error_code'] == 'PORT_IN_USE'
        revision = await one(conn,'SELECT digest FROM node_resource_revisions WHERE node_id=%s AND revision=%s',
            (agent.node,preview['desired'].revision))
        assert revision['digest'] == node_digest(preview['desired'])


@pytest.mark.asyncio
async def test_unrelated_tt_certificate_bootstrap_does_not_withhold_ready_awg():
    from awg_contracts import TrustTunnelProfile,content_digest
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    tt = TrustTunnelProfile(profile_id=uuid4(),name='bootstrap',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':2443},access={'user_ids':[user.user_uuid]},
        tls={'domain':'vpn.example.org','email':'admin@example.org','challenge':'http-01'})
    await controller.trusttunnel.save_profile(tt)
    assert (await controller.trusttunnel.validate_profile(tt.profile_id))['valid']
    await controller.trusttunnel.apply_profile(tt.profile_id,content_digest(tt))
    agent.tt.issuing = True
    await controller.reconcile()
    assert agent.calls[-1] == 'tt-protocol'
    assert len((await controller.material(user.subscription_id)).peers) == 1
    assert (await controller.trusttunnel._material_for_user(user)).entries == []
    agent.tt.issuing = False
    await controller.reconcile()
    assert len((await controller.trusttunnel._material_for_user(user)).entries) == 1


@pytest.mark.asyncio
async def test_migration_adopts_agent_watermark_and_preserves_existing_backend():
    from awg_contracts.managed_node import NodeDesiredConfig,IngressBinding,envelope_digest
    from awg_controller.managed_node import binding_id
    from awg_controller.service import one
    controller,agent,profile,user = await setup_managed()
    async with controller.db.connect() as conn:
        deployment = await one(conn,'SELECT id FROM awg_deployments WHERE profile_id=%s',(profile.profile_id,))
        await conn.execute('DELETE FROM node_awg_backends WHERE node_id=%s',(agent.node,))
    agent.resources = NodeDesiredConfig(node_id=agent.node,revision=7,
        envelope_digest=envelope_digest(agent.installed.envelope),enabled_protocols=['amneziawg'],
        bindings=[IngressBinding(binding_id=binding_id(deployment['id'],'awg','udp','0.0.0.0'),
            deployment_id=deployment['id'],backend_role='awg',transport='udp',public_port=51820,backend_port=20008)])
    preview = await controller.resources.state(agent.node)
    assert preview['desired'].revision == 8
    assert preview['desired'].bindings[0].backend_port == 20008


@pytest.mark.asyncio
async def test_failed_unrelated_tt_resource_change_preserves_confirmed_awg_binding():
    from awg_contracts import TrustTunnelProfile,content_digest
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    tt = TrustTunnelProfile(profile_id=uuid4(),name='blocked',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':2443},
        tls={'domain':'vpn.example.org','email':'admin@example.org','challenge':'http-01'})
    await controller.trusttunnel.save_profile(tt)
    assert (await controller.trusttunnel.validate_profile(tt.profile_id))['valid']
    await controller.trusttunnel.apply_profile(tt.profile_id,content_digest(tt))
    agent.failure = True
    await controller.reconcile()
    assert len((await controller.material(user.subscription_id)).peers) == 1
    from awg_controller.service import one
    async with controller.db.connect() as conn:
        await conn.execute('UPDATE node_resource_revisions SET digest=%s WHERE node_id=%s AND revision=%s',
            ('0'*64,agent.node,agent.resources.revision))
    assert (await controller.material(user.subscription_id)).peers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_reason", ["PORT_IN_USE", "AGENT_TIMEOUT", "INGRESS_AUTH_FAILED"])
async def test_disabling_protocol_revokes_runtime_despite_resource_apply_failure(failure_reason):
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    preview = await controller.resources.state(agent.node,protocols=[])
    agent.failure = True
    with pytest.raises(AgentError):
        await controller.resources.apply(agent.node,NodeApplyRequest(desired=preview['desired'],
            expected_applied_revision=preview['actual'].applied_revision))
    async def failed_ensure(conn, registration):
        raise AgentError("Ingress unavailable", failure_reason)
    controller.resources.ensure = failed_ensure
    await controller.reconcile()
    snapshot = next(iter(agent.awg.desired.values()))
    assert not snapshot.profile.enabled
    assert snapshot.peers == []
    assert (await controller.material(user.subscription_id)).peers == []


@pytest.mark.asyncio
async def test_disabling_trusttunnel_revokes_runtime_despite_ingress_timeout():
    from awg_contracts import TrustTunnelProfile,content_digest
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    tt = TrustTunnelProfile(profile_id=uuid4(),name='revoke',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':2443},access={'user_ids':[user.user_uuid]},
        tls={'domain':'vpn.example.org','email':'admin@example.org','challenge':'http-01'})
    await controller.trusttunnel.save_profile(tt)
    assert (await controller.trusttunnel.validate_profile(tt.profile_id))['valid']
    await controller.trusttunnel.apply_profile(tt.profile_id,content_digest(tt))
    await controller.reconcile()
    assert len((await controller.trusttunnel._material_for_user(user)).entries) == 1
    preview = await controller.resources.state(agent.node,protocols=['amneziawg'])
    agent.failure = True
    with pytest.raises(AgentError):
        await controller.resources.apply(agent.node,NodeApplyRequest(desired=preview['desired'],
            expected_applied_revision=preview['actual'].applied_revision))
    async def failed_ensure(conn, registration):
        raise AgentError('Ingress unavailable','AGENT_TIMEOUT')
    controller.resources.ensure = failed_ensure
    await controller.reconcile()
    snapshot = next(iter(agent.tt.desired.values()))
    assert not snapshot.profile.enabled
    assert snapshot.clients == []
    assert (await controller.trusttunnel._material_for_user(user)).entries == []


@pytest.mark.asyncio
async def test_enabled_lifecycle_fallback_requires_verified_committed_binding():
    from awg_controller.service import one
    from awg_contracts import DesiredDeployment
    controller,agent,profile,user = await setup_managed()
    await controller.reconcile()
    async with controller.db.connect() as conn:
        deployment = await one(conn,'SELECT desired FROM awg_deployments WHERE profile_id=%s',(profile.profile_id,))
        desired = DesiredDeployment.model_validate(deployment['desired'])
        assert await controller.resources.existing_path(conn,desired,'amneziawg')
        await conn.execute('UPDATE node_resource_revisions SET digest=%s WHERE node_id=%s AND revision=%s',
            ('0'*64,agent.node,agent.resources.revision))
        assert not await controller.resources.existing_path(conn,desired,'amneziawg')
        assert await controller.resources.existing_path(conn,desired.model_copy(update={
            'profile':desired.profile.model_copy(update={'enabled':False})}),'amneziawg')


@pytest.mark.asyncio
async def test_private_install_bind_address_is_preserved_for_awg_and_tt_plans():
    from awg_contracts import TrustTunnelProfile,content_digest
    controller,agent,profile,user = await setup_managed(bind_addresses=['192.168.40.7'])
    await controller.reconcile()
    assert {binding.public_address for binding in agent.resources.bindings} == {'192.168.40.7'}
    assert len((await controller.material(user.subscription_id)).peers) == 1
    tt = TrustTunnelProfile(profile_id=uuid4(),name='private',node_ids=[agent.node],
        endpoint={'host':'vpn.example.org','port':2443},
        tls={'domain':'vpn.example.org','email':'admin@example.org','challenge':'http-01'})
    await controller.trusttunnel.save_profile(tt)
    assert (await controller.trusttunnel.validate_profile(tt.profile_id))['valid']
    await controller.trusttunnel.apply_profile(tt.profile_id,content_digest(tt))
    preview = await controller.resources.state(agent.node)
    assert {binding.backend_role for binding in preview['desired'].bindings} == {'awg','trusttunnel','acme-http'}
    assert {binding.public_address for binding in preview['desired'].bindings} == {'192.168.40.7'}
    assert (await controller.resources.validate(agent.node,preview['desired'])).valid


@pytest.mark.asyncio
async def test_real_agent_client_accepts_unversioned_node_validation_result():
    import json
    from awg_contracts.managed_node import NodeDesiredConfig,envelope_digest
    node = uuid4()
    registration = NodeRegistration(node_id=node,name='validate',management_url='https://agent.invalid')
    envelope = NodeInstallEnvelope(node_id=node,envelope_id=uuid4(),backend_ipv4='10.244.0.2')
    desired = NodeDesiredConfig(node_id=node,revision=1,envelope_digest=envelope_digest(envelope),
        enabled_protocols=[],bindings=[])
    result = NodeValidationResult(valid=True,revision=1,digest=node_digest(desired),issues=[])
    def respond(request):
        assert request.method == 'POST'
        assert request.url.path == '/v1/node/validate'
        assert json.loads(request.content) == desired.model_dump(mode='json')
        return httpx.Response(200,json=result.model_dump(mode='json'))
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.node_validate(registration,desired) == result
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint',['capabilities','state'])
@pytest.mark.parametrize('version_field',['api_version','protocol_version'])
async def test_real_agent_client_rejects_unknown_version_on_versioned_node_models(endpoint,version_field):
    registration = NodeRegistration(node_id=uuid4(),name='unknown',management_url='https://agent.invalid')
    payload = {'node_id':str(registration.node_id),'api_version':'node.remnawave-extension.io/v1',
               'protocol_version':'1.0'}
    payload[version_field] = 'unknown'
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload)))
    try:
        with pytest.raises(AgentError) as caught:
            await getattr(client,'node_'+endpoint)(registration)
        assert caught.value.reason == 'UNSUPPORTED_NODE_API'
    finally:
        await client.close()
