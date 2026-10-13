"""No-TUN lab harness using production ResourceService, UDS client and TT runtime.

Control binds management bridge only; staged CA never enables publication READY.
"""
import json
import os
import socket
import threading
from pathlib import Path
from uuid import UUID
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from awg_agent.certificates import CertificateManager
from awg_agent.ingress_client import IngressClient
from awg_agent.managed_node import NodeResourceService
from awg_agent.trusttunnel import TrustTunnelService
from awg_agent.trusttunnel_runtime import TrustTunnelRuntime
from awg_contracts import NodeInstallEnvelope, NodeDesiredConfig, NodeApplyRequest, TrustTunnelApplyRequest, TrustTunnelDesired

os.umask(0o077)
envelope = NodeInstallEnvelope.model_validate_json(Path('/control/envelope.json').read_text())
lock = threading.RLock()
service = resources = None
udp_echo = None


def initialize():
    global service, resources
    directory = Path('/state') / os.environ.get('MANAGED_LAB_RUN', 'manual')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    api_ca = Path('/opt/tt-lab/pebble-ca.pem')
    root = httpx.get('https://pebble:15000/roots/0', verify=str(api_ca), trust_env=False, timeout=5).raise_for_status().text
    ca = directory / 'test-ca.pem'
    ca.write_text(api_ca.read_text() + '\n' + root)
    os.environ['TT_TEST_ACME_CA'] = str(ca)
    service = TrustTunnelService(directory / 'journal', envelope.node_id,
        TrustTunnelRuntime(directory / 'runtime', managed=True), CertificateManager(directory / 'acme'))
    ingress = IngressClient('/control/ingress.sock', '/control/ingress.secret', envelope)
    resources = NodeResourceService(directory / 'resources', envelope, ingress, trusttunnel=service)
    service.lock = resources.lock
    service.resources = resources
    service.certificates.publish_lock = resources.lock
    service.certificates.available_methods = resources.acme_methods
    resources.recover()
    service.recover()


def action(path, body):
    if path == '/health':
        try:
            if service is None:
                initialize()
            resources.ingress.state()
        except (httpx.HTTPError, OSError, RuntimeError):
            return {'ready': False}
        return {'ready': True}
    if path == '/envelope':
        return envelope.model_dump(mode='json')
    if path == '/node/validate':
        return resources.validate(NodeDesiredConfig.model_validate(body)).model_dump(mode='json')
    if path == '/node/apply':
        return resources.apply(NodeApplyRequest.model_validate(body)).model_dump(mode='json')
    if path == '/node/state':
        return resources.state().model_dump(mode='json')
    if path == '/auth-denial':
        response = resources.ingress.http.post('/v1/ingress/commit', json={})
        return {'status': response.status_code}
    if path == '/udp-echo':
        # Raw relay transport probe, explicitly not an AWG handshake substitute.
        global udp_echo
        if udp_echo is None:
            udp_echo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_echo.bind(('10.242.21.3', 20000))
            def echo():
                while True:
                    data, source = udp_echo.recvfrom(65535)
                    udp_echo.sendto(data, source)
            threading.Thread(target=echo, daemon=True).start()
        return {'listening': True}
    if path == '/apply':
        return service.apply(TrustTunnelApplyRequest.model_validate(body)).model_dump(mode='json')
    if path == '/tick':
        service.run_once()
        return {'tick': True}
    if path == '/state':
        return service.state(UUID(body['id'])).model_dump(mode='json')
    if path == '/export':
        desired = TrustTunnelDesired.model_validate(service._load(UUID(body['id']))['effective'])
        peer = next(peer for peer in desired.clients if str(peer.peer_id) == body['peer_id'])
        return {format: service.runtime.export(desired, peer, format) for format in ('toml', 'deeplink')}
    if path == '/ca':
        return {'ca': httpx.get('https://pebble:15000/roots/0', verify='/opt/tt-lab/pebble-ca.pem', trust_env=False, timeout=5).raise_for_status().text}
    if path == '/recover':
        service.close()
        resources.close()
        initialize()
        return {'recovered': True}
    raise ValueError('unknown action')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            with lock:
                response = action(self.path, body)
            self.send_response(200)
        except Exception as error:
            response = {'error': type(error).__name__}
            if type(error) is ValueError:
                response['detail'] = str(error)[:200]
            self.send_response(500)
        data = json.dumps(response).encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('10.242.22.3', 9000), Handler).serve_forever()
