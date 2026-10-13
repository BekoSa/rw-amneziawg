import logging
import os
import ssl
import time
import json
from pathlib import Path
from uuid import UUID
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from .api import create_app
from .runtime import UserspaceRuntime
from .service import AgentService


def authorized_controller(certificate, expected_uri):
    return ('URI', expected_uri) in certificate.get('subjectAltName', ())


class ControllerTLSProtocol(H11Protocol):
    def connection_made(self, transport):
        # Initialize first: uvicorn's connection_lost expects protocol state even for rejected peers.
        super().connection_made(transport)
        tls = transport.get_extra_info('ssl_object')
        expected = 'spiffe://awg/controller/' + str(UUID(os.environ['AWG_CONTROLLER_ID']))
        certificate = tls.getpeercert() if tls is not None else None
        if not certificate or not authorized_controller(certificate, expected):
            # Identities only (no keys): tells the operator which Controller this node was enrolled for.
            presented = [value for kind, value in (certificate or {}).get('subjectAltName', ()) if kind == 'URI']
            logging.getLogger('uvicorn.error').warning(
                'Rejected management connection: this node accepts %s, the client presented %s. '
                'Issue a new node key from this panel and re-run install-node.sh --reenroll.',
                expected, presented or 'no client certificate')
            transport.close()


def main():
    os.umask(0o077)
    UUID(os.environ['AWG_CONTROLLER_ID'])
    directory = Path(os.environ.get('AWG_STATE_DIR', '/var/lib/awg-agent'))
    node_id = UUID(os.environ['AWG_NODE_ID'])
    resources = None
    runtime = UserspaceRuntime()
    envelope_path = os.environ.get('NODE_INSTALL_ENVELOPE')
    if envelope_path:
        from awg_contracts import NodeInstallEnvelope
        from .ingress_client import IngressClient
        from .managed_node import NodeResourceService, ManagedAWGRuntime
        envelope = NodeInstallEnvelope.model_validate_json(Path(envelope_path).read_text())
        if envelope.node_id != node_id: raise ValueError('ENVELOPE_MISMATCH')
        ingress = IngressClient(os.environ['NODE_INGRESS_SOCKET'], os.environ['NODE_INGRESS_SECRET'], envelope)
        deadline = time.monotonic() + 30
        while True:
            try:
                ingress.state()
                break
            except Exception:
                if time.monotonic() >= deadline: raise RuntimeError('INGRESS_UNAVAILABLE') from None
                time.sleep(0.2)
        resources = NodeResourceService(directory / 'resources', envelope, ingress)
        runtime = ManagedAWGRuntime(runtime, resources)
    service = AgentService(directory, node_id, runtime)
    trusttunnel = None
    if resources is not None or os.environ.get('TT_ENABLED') == '1':
        from .trusttunnel import TrustTunnelService
        from .trusttunnel_runtime import TrustTunnelRuntime
        from .certificates import CertificateManager
        directory = service.directory / 'trusttunnel'
        trusttunnel = TrustTunnelService(directory, service.node_id,
            TrustTunnelRuntime(directory / 'runtime', managed=resources is not None), CertificateManager(directory / 'acme'))
    if resources is not None:
        resources.bind_services(service, trusttunnel)
        actual = resources.bootstrap_resources()
        if actual.state == 'ERROR': raise RuntimeError('INGRESS_UNAVAILABLE')
    service.recover()
    if trusttunnel is not None:
        trusttunnel.recover()
        trusttunnel.start()
    try:
        uvicorn.run(create_app(service, trusttunnel=trusttunnel, node_resources=resources), host=os.environ.get('AWG_MANAGEMENT_HOST', '0.0.0.0'),
                port=int(os.environ.get('AWG_MANAGEMENT_PORT', '8443')), http=ControllerTLSProtocol,
                ssl_certfile=os.environ['AWG_TLS_CERT'], ssl_keyfile=os.environ['AWG_TLS_KEY'],
                ssl_ca_certs=os.environ['AWG_TLS_CA'], ssl_cert_reqs=ssl.CERT_REQUIRED,
                access_log=False, proxy_headers=False, workers=1)
    finally:
        if trusttunnel is not None:
            trusttunnel.close()
        service.close()
        if resources is not None: resources.close()


if __name__ == '__main__':
    main()
