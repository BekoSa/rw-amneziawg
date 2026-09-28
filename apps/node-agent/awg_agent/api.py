from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from awg_contracts import ApplyRequest, DesiredDeployment


def create_app(service):
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
    def capabilities(): return service.capabilities()

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
        return JSONResponse(status_code=200 if ok else 503, content={'ready': ok})

    return app
