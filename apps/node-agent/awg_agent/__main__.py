import os
import ssl
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
        if tls is None or not authorized_controller(tls.getpeercert(), expected):
            transport.close()


def main():
    os.umask(0o077)
    UUID(os.environ['AWG_CONTROLLER_ID'])
    service = AgentService(Path(os.environ.get('AWG_STATE_DIR', '/var/lib/awg-agent')),
                           UUID(os.environ['AWG_NODE_ID']), UserspaceRuntime())
    service.recover()
    uvicorn.run(create_app(service), host=os.environ.get('AWG_MANAGEMENT_HOST', '0.0.0.0'),
                port=int(os.environ.get('AWG_MANAGEMENT_PORT', '8443')), http=ControllerTLSProtocol,
                ssl_certfile=os.environ['AWG_TLS_CERT'], ssl_keyfile=os.environ['AWG_TLS_KEY'],
                ssl_ca_certs=os.environ['AWG_TLS_CA'], ssl_cert_reqs=ssl.CERT_REQUIRED,
                access_log=False, proxy_headers=False, workers=1)


if __name__ == '__main__':
    main()
