"""The actual client sends authenticated bytes and sanitizes hostile responses."""
import hashlib
import hmac
import json
from uuid import UUID
import httpx
import pytest
from awg_contracts import NodeInstallEnvelope
from awg_agent.ingress_client import IngressClient, IngressError


def test_control_request_mac_binds_challenge_body_nonce_and_path(tmp_path):
    secret=tmp_path/'secret';secret.write_text('a'*64+'\n')
    envelope=NodeInstallEnvelope(envelope_id=UUID(int=1),node_id=UUID(int=2),backend_ipv4='10.245.0.2')
    client=IngressClient(tmp_path/'control.sock',secret,envelope)
    seen=[]
    def handle(request):
        if request.url.path.endswith('/session'):
            return httpx.Response(200,json={'session_id':str(UUID(int=9)),'challenge':'b'*64,'next_nonce':1})
        nonce=request.headers['x-ingress-nonce'];seen.append(nonce)
        text='\n'.join([str(UUID(int=9)),'b'*64,request.method,request.url.path,nonce,
                        hashlib.sha256(request.content).hexdigest()]).encode()
        assert request.headers['x-ingress-mac']==hmac.new(b'a'*64,text,hashlib.sha256).hexdigest()
        return httpx.Response(503,json={'code':'private credential here','message':'secret'})
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(handle),base_url='http://ingress')
    for _ in range(2):
        with pytest.raises(IngressError,match='^INGRESS_UNAVAILABLE$'): client.state()
    assert seen==['1','2']
    client.close()


def test_expired_session_reauthenticates_once(tmp_path):
    secret=tmp_path/'secret';secret.write_text('a'*64)
    envelope=NodeInstallEnvelope(envelope_id=UUID(int=1),node_id=UUID(int=2),backend_ipv4='10.245.0.2')
    client=IngressClient(tmp_path/'control.sock',secret,envelope);sessions=[]
    def handle(request):
        if request.url.path.endswith('/session'):
            identifier=str(UUID(int=10+len(sessions)));sessions.append(identifier)
            return httpx.Response(200,json={'session_id':identifier,'challenge':'c'*64,'next_nonce':1})
        return httpx.Response(401,json={'code':'SESSION_EXPIRED','message':'expired'})
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(handle),base_url='http://ingress')
    with pytest.raises(IngressError): client.state()
    assert len(sessions)==2
    client.close()


def test_abort_accepts_empty_204_without_losing_authenticated_session(tmp_path):
    from awg_contracts import IngressCommitRequest
    secret=tmp_path/'secret';secret.write_text('a'*64)
    envelope=NodeInstallEnvelope(envelope_id=UUID(int=1),node_id=UUID(int=2),backend_ipv4='10.245.0.2')
    client=IngressClient(tmp_path/'control.sock',secret,envelope)
    sessions=[]
    def handle(request):
        if request.url.path.endswith('/session'):
            sessions.append(request.url.path)
            return httpx.Response(200,json={'session_id':str(UUID(int=9)),'challenge':'b'*64,'next_nonce':1})
        assert request.url.path=='/v1/ingress/abort'
        return httpx.Response(204)
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(handle),base_url='http://ingress')
    request=IngressCommitRequest(ticket_id=UUID(int=3),revision=1,digest='a'*64)
    client.abort(request);client.abort(request)
    assert len(sessions)==1 and client.nonce==2
    client.close()
