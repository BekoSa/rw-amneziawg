"""Resource coordinator safety independent of privileged network adapters."""
from uuid import UUID, uuid4
import pytest
import importlib
from datetime import datetime, timezone


def modules():
    c = importlib.import_module('awg_contracts')
    assert importlib.util.find_spec('awg_agent.managed_node'), 'resource coordinator missing'
    return c, importlib.import_module('awg_agent.managed_node')


def plan(c, revision=1):
    envelope = c.NodeInstallEnvelope(envelope_id=UUID(int=1), node_id=UUID(int=2), backend_ipv4='10.245.0.2')
    desired = c.NodeDesiredConfig(node_id=envelope.node_id, revision=revision,
        envelope_digest=c.envelope_digest(envelope), enabled_protocols=['amneziawg'], bindings=[
        c.IngressBinding(binding_id=UUID(int=3), deployment_id=UUID(int=4), transport='udp',
            public_port=51820, backend_role='awg', backend_port=20001)])
    return envelope, desired


class Ingress:
    def __init__(self, c, node_id):
        self.c, self.node_id, self.current, self.prepared = c, node_id, None, None
        self.fail = False
        self.commits = 0
    def validate(self, desired):
        return self.c.NodeValidationResult(valid=True,revision=desired.revision,digest=self.c.node_digest(desired),issues=[])
    def prepare(self, request):
        if self.fail: raise RuntimeError('private infrastructure failure')
        self.prepared = request.desired
        return self.c.IngressPrepared(ticket_id=uuid4(), revision=request.desired.revision,
            digest=self.c.node_digest(request.desired))
    def commit(self, request):
        self.current = self.prepared
        self.commits += 1
        return self.state()
    def abort(self, request): self.prepared = None
    def state(self):
        d = self.current
        return self.c.NodeActualState(node_id=self.node_id, state='APPLYING' if d else 'DRAFT',
            accepted_revision=d.revision if d else None, desired_revision=d.revision if d else None,
            applied_revision=d.revision if d else None, applied_digest=self.c.node_digest(d) if d else None,
            bindings=[self.c.NodeBindingActual(binding=b,state='ACTIVE',backend_ready=True) for b in d.bindings] if d else [],
            observed_at=datetime.now(timezone.utc), runtime_epoch=uuid4())


def test_resource_commit_waits_for_protocol_readiness(tmp_path):
    c,m = modules(); envelope, desired = plan(c); ingress=Ingress(c,envelope.node_id)
    service=m.NodeResourceService(tmp_path,envelope,ingress)
    result=service.apply(c.NodeApplyRequest(desired=desired))
    assert result.state=='APPLYING' and not result.healthy
    assert not result.bindings[0].backend_ready
    assert service.backend_port(UUID(int=4),'awg')==20001
    assert ingress.commits==1
    service.apply(c.NodeApplyRequest(desired=desired))
    assert ingress.commits==1
    service.close()


def test_accepted_watermark_survives_ingress_failure_and_restart(tmp_path):
    c,m=modules(); envelope,desired=plan(c,2); ingress=Ingress(c,envelope.node_id); ingress.fail=True
    service=m.NodeResourceService(tmp_path,envelope,ingress)
    result=service.apply(c.NodeApplyRequest(desired=desired))
    assert result.state=='ERROR' and result.error.code=='INGRESS_UNAVAILABLE'
    assert 'private' not in result.error.message
    service.close(); service=m.NodeResourceService(tmp_path,envelope,ingress)
    with pytest.raises(ValueError,match='STALE_REVISION'): service.apply(c.NodeApplyRequest(desired=plan(c,1)[1]))
    changed=desired.model_copy(deep=True); changed.bindings[0].public_port=51821
    with pytest.raises(ValueError,match='REVISION_CONFLICT'): service.apply(c.NodeApplyRequest(desired=changed))
    ingress.fail=False
    assert service.apply(c.NodeApplyRequest(desired=desired)).state=='APPLYING'
    service.close()


def test_envelope_mismatch_never_prepares_socket(tmp_path):
    c,m=modules(); envelope,desired=plan(c); ingress=Ingress(c,envelope.node_id)
    service=m.NodeResourceService(tmp_path,envelope,ingress)
    desired.envelope_digest='a'*64
    assert not service.validate(desired).valid
    with pytest.raises(ValueError): service.apply(c.NodeApplyRequest(desired=desired))
    assert ingress.prepared is None
    service.close()


def test_managed_awg_adapter_preserves_public_desired(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired, Runtime
    envelope,config=plan(c); d=desired(); config.bindings[0].deployment_id=d.deployment_id
    ingress=Ingress(c,envelope.node_id); resources=m.NodeResourceService(tmp_path/'resources',envelope,ingress)
    driver=Runtime(); agent=AgentService(tmp_path/'awg',envelope.node_id,m.ManagedAWGRuntime(driver,resources))
    resources.bind_services(agent)
    resources.apply(c.NodeApplyRequest(desired=config))
    original=c.content_digest(d)
    assert agent.apply(c.ApplyRequest(desired=d)).state=='READY'
    assert driver.current[str(d.deployment_id)].profile.endpoint.port==20001
    assert d.profile.endpoint.port==51820 and c.content_digest(d)==original
    assert resources.state().state=='READY'
    agent.close(); resources.close()


def test_legacy_agent_does_not_advertise_managed_routes(tmp_path):
    from awg_agent.api import create_app
    from tests.unit.agent.test_agent import service
    agent=service(tmp_path)
    assert '/v1/node/apply' not in {r.path for r in create_app(agent).routes}
    agent.close()


def test_managed_trusttunnel_frontends_are_private_fixed_roles():
    from awg_agent.trusttunnel_runtime import ingress_config
    config=ingress_config(8444,8080,8444,managed=True)
    assert 'bind 0.0.0.0:8444' in config and 'bind 0.0.0.0:8080' in config
    assert 'bind 0.0.0.0:443' not in config
    assert 'server endpoint 127.0.0.1:9443' in config


def test_port_change_is_not_ready_until_matching_protocol_applies(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired, Runtime
    envelope,config=plan(c); d=desired(); config.bindings[0].deployment_id=d.deployment_id
    ingress=Ingress(c,envelope.node_id); r=m.NodeResourceService(tmp_path/'resources',envelope,ingress)
    agent=AgentService(tmp_path/'awg',envelope.node_id,m.ManagedAWGRuntime(Runtime(),r));r.bind_services(agent)
    r.apply(c.NodeApplyRequest(desired=config));agent.apply(c.ApplyRequest(desired=d))
    new=config.model_copy(deep=True);new.revision=2;new.bindings[0].public_port=51821
    assert r.apply(c.NodeApplyRequest(desired=new,expected_applied_revision=1)).state=='APPLYING'
    assert not r.state().bindings[0].backend_ready
    d.revision=2;d.profile.endpoint.port=51821
    agent.apply(c.ApplyRequest(desired=d,expected_applied_revision=1))
    assert r.state().state=='READY'
    agent.close();r.close()


def test_validate_does_not_change_accepted_watermark(tmp_path):
    c,m=modules(); envelope,d=plan(c); ingress=Ingress(c,envelope.node_id)
    r=m.NodeResourceService(tmp_path,envelope,ingress)
    assert r.validate(d).valid
    assert ingress.prepared is None and not r.path.exists()
    changed=d.model_copy(deep=True);changed.bindings[0].public_port=51821
    assert r.validate(changed).valid
    assert r.apply(c.NodeApplyRequest(desired=changed)).accepted_revision==1
    r.close()


def test_disabled_managed_awg_removes_runtime_after_binding_release(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired, Runtime
    envelope,config=plan(c);d=desired();config.bindings[0].deployment_id=d.deployment_id
    ingress=Ingress(c,envelope.node_id);r=m.NodeResourceService(tmp_path/'resources',envelope,ingress)
    driver=Runtime();agent=AgentService(tmp_path/'awg',envelope.node_id,m.ManagedAWGRuntime(driver,r));r.bind_services(agent)
    r.apply(c.NodeApplyRequest(desired=config));agent.apply(c.ApplyRequest(desired=d))
    empty=config.model_copy(deep=True);empty.revision=2;empty.bindings=[];empty.enabled_protocols=[]
    r.apply(c.NodeApplyRequest(desired=empty,expected_applied_revision=1))
    d.revision=2;d.profile.enabled=False;d.peers=[]
    assert agent.apply(c.ApplyRequest(desired=d,expected_applied_revision=1)).state=='READY'
    assert not driver.current
    agent.close();r.close()


def test_bootstrap_legacy_journal_preserves_keys_and_endpoint(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired,Runtime
    d=desired();agent=AgentService(tmp_path/'awg',d.node_id,Runtime())
    key=agent.apply(c.ApplyRequest(desired=d)).server_public_key
    envelope,_=plan(c);ingress=Ingress(c,envelope.node_id);r=m.NodeResourceService(tmp_path/'resources',envelope,ingress)
    agent.runtime=m.ManagedAWGRuntime(Runtime(),r);r.bind_services(agent)
    actual=r.bootstrap_resources()
    assert actual.bindings[0].binding.public_port==51820
    assert actual.bindings[0].binding.backend_port==20000
    agent.recover()
    assert agent.state(d.deployment_id).server_public_key==key
    assert r.state().state=='READY'
    agent.close();r.close()


def test_recovery_adopts_proven_ingress_commit_after_agent_crash(tmp_path):
    c,m=modules();envelope,d=plan(c);ingress=Ingress(c,envelope.node_id)
    r=m.NodeResourceService(tmp_path,envelope,ingress)
    r.record.update(accepted_revision=1,accepted_digest=c.node_digest(d),desired=d.model_dump(mode='json'),phase='applying')
    r._save()
    ticket=ingress.prepare(c.IngressPrepareRequest(desired=d));ingress.commit(c.IngressCommitRequest(**ticket.model_dump()))
    def refuse_prepare(request): raise AssertionError('committed ingress must not be prepared again')
    ingress.prepare=refuse_prepare
    r.close();r=m.NodeResourceService(tmp_path,envelope,ingress)
    assert r.recover().applied_revision==1
    assert r.state().error is None
    r.close()


def test_final_awg_traffic_available_after_public_binding_release(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired, Runtime
    envelope,config=plan(c);d=desired();config.bindings[0].deployment_id=d.deployment_id
    ingress=Ingress(c,envelope.node_id);r=m.NodeResourceService(tmp_path/'resources',envelope,ingress)
    agent=AgentService(tmp_path/'awg',envelope.node_id,m.ManagedAWGRuntime(Runtime(),r));r.bind_services(agent)
    r.apply(c.NodeApplyRequest(desired=config));agent.apply(c.ApplyRequest(desired=d))
    empty=config.model_copy(deep=True);empty.revision=2;empty.bindings=[];empty.enabled_protocols=[]
    r.apply(c.NodeApplyRequest(desired=empty,expected_applied_revision=1))
    assert agent.traffic(d.deployment_id).peers
    with pytest.raises(Exception, match='BACKEND_NOT_READY'):
        agent.runtime.apply(d,'private-key')
    d.revision=2;d.profile.enabled=False;d.peers=[]
    assert agent.apply(c.ApplyRequest(desired=d,expected_applied_revision=1)).state=='READY'
    agent.close();r.close()


@pytest.mark.parametrize('http_port,alpn_port,expected', [('80','0',{'trusttunnel','acme-http'}),('0','443',{'trusttunnel','acme-tls-alpn'})])
def test_bootstrap_preserves_disabled_legacy_acme_ingress(tmp_path,monkeypatch,http_port,alpn_port,expected):
    from types import SimpleNamespace
    c,m=modules();envelope,_=plan(c);identifier=UUID(int=8)
    envelope.bind_addresses=['10.245.0.2']
    monkeypatch.setenv('TT_HTTP_PORT',http_port);monkeypatch.setenv('TT_TLS_ALPN_PORT',alpn_port)
    class LegacyTT:
        runtime=SimpleNamespace(ingress=None,ensure_ingress=lambda:None)
        def identifiers(self): return [identifier]
        def _load(self,key):
            return {'desired':{'profile':{'enabled':True,'endpoint':{'port':2443},'tls':{'challenge':'auto'}}}}
    ingress=Ingress(c,envelope.node_id);r=m.NodeResourceService(tmp_path,envelope,ingress,trusttunnel=LegacyTT())
    state=r.bootstrap_resources()
    assert state.error is None
    assert {item.binding.backend_role for item in state.bindings}==expected
    assert all(item.binding.public_address=='10.245.0.2' for item in state.bindings)
    r.close()


def test_bootstrap_preserves_private_awg_bind_address(tmp_path):
    c,m=modules()
    from awg_agent.service import AgentService
    from tests.unit.agent.test_agent import desired,Runtime
    d=desired();agent=AgentService(tmp_path/'awg',d.node_id,Runtime());agent.apply(c.ApplyRequest(desired=d))
    envelope,_=plan(c);envelope.bind_addresses=['10.245.0.2']
    ingress=Ingress(c,envelope.node_id);r=m.NodeResourceService(tmp_path/'resources',envelope,ingress,awg=agent)
    assert r.bootstrap_resources().bindings[0].binding.public_address=='10.245.0.2'
    agent.close();r.close()
