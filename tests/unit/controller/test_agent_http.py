"""Actual Controller HTTP client against actual Agent ASGI routes and service."""
import base64
from uuid import uuid4
import httpx
import pytest
from awg_contracts import AWGProfile, DesiredDeployment, NodeRegistration
from awg_controller.agent_client import AgentClient
from awg_agent.api import create_app
from awg_agent.service import AgentService


class Runtime:
    def __init__(self):
        self.current = {}
        self.epoch = uuid4()
    def apply(self, desired, private_key):
        self.current[desired.deployment_id] = desired
    def healthy(self, desired, public_key):
        return self.current.get(desired.deployment_id) == desired
    def counters(self, desired):
        return self.epoch, {}, {}
    def stop(self, deployment_id):
        self.current.pop(deployment_id,None)


@pytest.mark.asyncio
async def test_controller_http_methods_match_real_agent_routes(tmp_path):
    node = uuid4()
    agent = AgentService(tmp_path,node,Runtime())
    # Only replace network transport: all production serialization/routes/services execute.
    client = AgentClient.__new__(AgentClient)
    client.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(agent)))
    registration = NodeRegistration(node_id=node,name='test',management_url='https://agent.invalid')
    profile = AWGProfile(profile_id=uuid4(),name='test',endpoint={'host':'test.invalid','port':51820},
        network={'ipv4_pool':'10.51.0.0/24','server_ipv4':'10.51.0.1'},
        protocol={'adapter_id':'amneziawg-go-v3','version':'2','parameters':{
            'Jc':4,'Jmin':40,'Jmax':70,'S1':12,'S2':13,'S3':14,'S4':15,
            'H1':'100-110','H2':'200-210','H3':'300-310','H4':'400-410'}},node_ids=[node])
    desired = DesiredDeployment(deployment_id=uuid4(),node_id=node,revision=1,profile=profile)
    assert (await client.capabilities(registration)).node_id == node
    assert (await client.state(registration,desired.deployment_id)).applied_revision is None
    assert (await client.validate(registration,desired)).valid
    assert (await client.apply(registration,desired,None)).state == 'READY'
    assert (await client.state(registration,desired.deployment_id)).applied_revision == 1
    assert (await client.traffic(registration,desired.deployment_id)).deployment_id == desired.deployment_id
    await client.close()
    agent.close()
