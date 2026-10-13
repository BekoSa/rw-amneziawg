from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from awg_contracts import (ApplyRequest, DesiredDeployment, ProtocolCapability, TrustTunnelApplyRequest,
                          TrustTunnelDesired, TrustTunnelExportRequest, wire_payload)


def create_app(service, trusttunnel=None, node_resources=None):
    app = FastAPI(title='AWG Agent', version='1.0', docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={'detail': 'Invalid management payload'})

    def invoke(operation, *args):
        try: return operation(*args)
        except KeyError: raise HTTPException(404, 'Unknown deployment') from None
        except ValueError as exc: raise HTTPException(409, str(exc)) from None
        except Exception: raise HTTPException(503, 'Agent operation failed') from None

    def match(identifier, desired):
        if identifier != desired.deployment_id: raise HTTPException(409, 'Deployment identity mismatch')

    @app.get('/v1/capabilities')
    def capabilities():
        result = service.capabilities()
        if trusttunnel is not None:
            result.protocols.append(ProtocolCapability(adapter_id='trusttunnel', versions=['1.0'],
                features=['http2', 'http3', 'acme-http01', 'acme-tls-alpn01']))
        return result

    @app.post('/v1/deployments/{identifier}/validate')
    def validate(identifier: UUID, desired: DesiredDeployment):
        match(identifier, desired)
        return invoke(service.validate, desired)

    @app.put('/v1/deployments/{identifier}/apply')
    def apply(identifier: UUID, request: ApplyRequest):
        match(identifier, request.desired)
        return invoke(service.apply, request)

    @app.get('/v1/deployments/{identifier}/state')
    def state(identifier: UUID): return invoke(service.state, identifier)

    @app.get('/v1/deployments/{identifier}/traffic')
    def traffic(identifier: UUID): return invoke(service.traffic, identifier)

    @app.get('/health/live')
    def live(): return {'alive': True}

    @app.get('/health/ready')
    def ready():
        ids = [row[0] for row in service.db.execute('SELECT id FROM deployments')]
        states = [service.state(UUID(identifier)) for identifier in ids]
        ok = all(state.state == 'READY' for state in states)
        if trusttunnel is not None:
            ok = ok and all(trusttunnel.state(identifier).state == 'READY' for identifier in trusttunnel.identifiers())
        return JSONResponse(status_code=200 if ok else 503, content={'ready': ok})

    if trusttunnel is not None:
        @app.post('/v1/trusttunnel/deployments/{identifier}/validate')
        def tt_validate(identifier: UUID, desired: TrustTunnelDesired):
            match(identifier, desired)
            return invoke(trusttunnel.validate, desired)

        @app.put('/v1/trusttunnel/deployments/{identifier}/apply')
        def tt_apply(identifier: UUID, request: TrustTunnelApplyRequest):
            match(identifier, request.desired)
            return invoke(trusttunnel.apply, request)

        @app.get('/v1/trusttunnel/deployments/{identifier}/state')
        def tt_state(identifier: UUID):
            return invoke(trusttunnel.state, identifier)

        @app.post('/v1/trusttunnel/deployments/{identifier}/export')
        def tt_export(identifier: UUID, request: TrustTunnelExportRequest):
            # Management transport mTLS authorization precedes this explicit secret reveal.
            return wire_payload(invoke(trusttunnel.export, identifier, request.peer_id, request.format))

    if node_resources is not None:
        from awg_contracts import NodeDesiredConfig, NodeApplyRequest

        @app.get('/v1/node/capabilities')
        def node_capabilities(): return invoke(node_resources.capabilities)

        @app.post('/v1/node/validate')
        def node_validate(desired: NodeDesiredConfig): return invoke(node_resources.validate, desired)

        @app.put('/v1/node/apply')
        def node_apply(request: NodeApplyRequest): return invoke(node_resources.apply, request)

        @app.get('/v1/node/state')
        def node_state(): return invoke(node_resources.state)

    return app
