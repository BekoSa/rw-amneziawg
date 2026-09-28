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


def _self_signed(tmp_path, name):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False).sign(key, hashes.SHA256()))
    (tmp_path / f'{name}.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / f'{name}.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()))
    return tmp_path / f'{name}.crt', tmp_path / f'{name}.key'


@pytest.mark.asyncio
async def test_failure_reasons_on_real_connections(tmp_path):
    import asyncio, socket, ssl
    from awg_controller.agent_client import failure_reason
    async def reason(url, verify=True, timeout=5):
        async with httpx.AsyncClient(verify=verify, timeout=timeout) as client:
            try:
                await client.get(url)
            except httpx.HTTPError as error:
                return failure_reason(error)
    with socket.socket() as probe:  # a port nobody listens on
        probe.bind(('127.0.0.1', 0))
        closed = probe.getsockname()[1]
    assert await reason(f'https://127.0.0.1:{closed}/') == 'AGENT_REFUSED'

    cert, key = _self_signed(tmp_path, 'foreign.agents.awg.internal')
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(cert, key)
    server = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    other_ca, _ = _self_signed(tmp_path, 'other-ca')
    client_ctx = ssl.create_default_context(cafile=str(other_ca))
    assert await reason(f'https://127.0.0.1:{port}/', verify=client_ctx) == 'AGENT_TLS_UNTRUSTED'
    server.close()

    silent = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)  # accepts, never completes TLS
    port = silent.sockets[0].getsockname()[1]
    assert await reason(f'https://127.0.0.1:{port}/', timeout=0.5) == 'AGENT_TIMEOUT'
    silent.close()


@pytest.mark.asyncio
async def test_tui_shows_the_reason_not_just_503():
    from awg_tui.api import APIError, ControllerAPI
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={'code': 'DEPENDENCY_UNAVAILABLE', 'reason': 'AGENT_REFUSED'}))
    api = ControllerAPI('http://controller', 'admin', transport=transport)
    with pytest.raises(APIError, match='порт управления закрыт'):
        await api.register_node({})
    await api.close()
