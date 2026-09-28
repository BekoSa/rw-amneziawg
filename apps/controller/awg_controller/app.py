import asyncio
import hmac
import ipaddress
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from datetime import datetime, timezone, timedelta
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from awg_contracts import AWGProfile, NodeRegistration, SubscriptionRequest
from remnawave_client import RemnawaveClient, UpstreamError, verify_webhook
from .config import Settings
from .db import Database
from .agent_client import AgentClient, AgentError
from .service import Controller, rows


class ApplyProfileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_app(controller=None):
    @asynccontextmanager
    async def lifespan(app):
        owned = controller is None
        service = controller
        if owned:
            settings = Settings.from_env()
            service = Controller(Database(settings.database_url),RemnawaveClient(settings.remnawave_url,settings.remnawave_token),AgentClient(settings),settings)
        app.state.controller = service
        await service.db.migrate()
        task = asyncio.create_task(service.loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if owned:
                await service.upstream.close()
                await service.agents.close()

    app = FastAPI(title='AWG Controller',version='1.0',lifespan=lifespan)
    if controller:
        app.state.controller = controller

    # Production: the Controller also sits on the panel's Docker network (webhooks, Remnawave API).
    # Admin/internal routes answer only peers from the extension's own network (TUI, Gateway).
    allowed = [ipaddress.ip_network(item.strip(), strict=False)
               for item in os.environ.get('AWG_ADMIN_ALLOWED_CIDRS', '').split(',') if item.strip()]

    @app.middleware('http')
    async def restrict_sources(request, call_next):
        if allowed and request.url.path.startswith(('/api/', '/v1/internal/', '/metrics')):
            try:
                peer = ipaddress.ip_address(request.client.host)
            except (AttributeError, ValueError):
                peer = None
            if peer is None or not any(peer in network for network in allowed):
                return JSONResponse(status_code=404, content={'code': 'NOT_FOUND'})
        return await call_next(request)

    @app.middleware('http')
    async def correlation(request, call_next):
        request.state.correlation_id = str(uuid4())
        response = await call_next(request)
        response.headers['X-Correlation-ID'] = request.state.correlation_id
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422,content={'code':'INVALID_REQUEST','correlation_id':request.state.correlation_id})

    @app.exception_handler(ValueError)
    async def domain_error(request, exc):
        # Do not echo exception/input; fields can contain secrets.
        return JSONResponse(status_code=409,content={'code':'OPERATION_REJECTED','correlation_id':request.state.correlation_id})

    @app.exception_handler(UpstreamError)
    @app.exception_handler(AgentError)
    async def dependency_error(request, exc):
        if isinstance(exc, AgentError):
            reason = exc.reason
        else:
            reason = 'REMNAWAVE_TOKEN_REJECTED' if getattr(exc, 'unauthorized', False) else 'REMNAWAVE_UNAVAILABLE'
        return JSONResponse(status_code=503,content={'code':'DEPENDENCY_UNAVAILABLE','reason':reason,
            'correlation_id':request.state.correlation_id})

    def service(request: Request):
        return request.app.state.controller

    async def admin(s=Depends(service), authorization: str=Header(default='')):
        if not hmac.compare_digest(authorization,'Bearer '+s.settings.admin_token):
            raise HTTPException(401,'Unauthorized')
        return s

    async def gateway(s=Depends(service), authorization: str=Header(default='')):
        if not hmac.compare_digest(authorization,'Bearer '+s.settings.gateway_token):
            raise HTTPException(401,'Unauthorized')
        return s

    @app.get('/health/live')
    async def live():
        return {'alive':True}

    @app.get('/health/ready')
    async def ready(s=Depends(service)):
        try:
            async with s.db.connect() as conn:
                await conn.execute('SELECT 1')
        except Exception:
            raise HTTPException(503,'Database unavailable') from None
        return {'ready':True,'upstream_last_success':s.last_upstream_ok}

    @app.get('/api/v1/nodes')
    async def nodes(s=Depends(admin)):
        async with s.db.connect() as conn:
            data = await rows(conn,'SELECT * FROM awg_nodes ORDER BY id')
            output = []
            for row in data:
                deployments = await rows(conn,'SELECT actual FROM awg_deployments WHERE node_id=%s AND actual IS NOT NULL',(row['id'],))
                online = row['online'] and row['last_seen_at'] is not None and datetime.now(timezone.utc)-row['last_seen_at'] < timedelta(seconds=s.settings.ready_max_age)
                output.append({'registration':row['registration'],'capabilities':row['capabilities'],'online':online,'last_seen_at':row['last_seen_at'],
                    'deployments':[d['actual'] for d in deployments]})
            return {'items':output}

    @app.post('/api/v1/nodes')
    async def register(value:NodeRegistration,s=Depends(admin)):
        await s.register_node(value)
        return {'registered':True}

    @app.get('/api/v1/profiles')
    async def profiles(s=Depends(admin)):
        async with s.db.connect() as conn:
            return {'items':await rows(conn,'SELECT * FROM awg_profiles ORDER BY id')}

    @app.put('/api/v1/profiles/{profile_id}')
    async def save(profile_id:UUID,value:AWGProfile,s=Depends(admin)):
        if value.profile_id != profile_id:
            raise ValueError('profile mismatch')
        await s.save_profile(value)
        return {'saved':True}

    @app.post('/api/v1/profiles/{profile_id}/validate')
    async def validate(profile_id:UUID,s=Depends(admin)):
        return await s.validate_profile(profile_id)

    @app.post('/api/v1/profiles/{profile_id}/apply',status_code=202)
    async def apply(profile_id:UUID,value:ApplyProfileRequest,s=Depends(admin)):
        await s.apply_profile(profile_id,value.expected_digest)
        return {'accepted':True}

    @app.post('/api/v1/reconcile',status_code=202)
    async def reconcile(s=Depends(admin)):
        s.wakeup.set()
        return {'accepted':True}

    @app.get('/api/v1/peers')
    async def peers(s=Depends(admin)):
        async with s.db.connect() as conn:
            # Users deleted in Remnawave disappear once every node confirmed removal; until then they
            # stay visible as pending removal (e.g. an offline node still holds the peer).
            return {'items':await rows(conn,"""SELECT p.*,d.node_id,d.profile_id,u.upstream->>'username' AS username,
                u.upstream->>'status' AS user_status,u.deleted AS user_deleted,
                (t.peer_id IS NOT NULL AND t.acknowledged_at IS NULL) AS removal_pending
                FROM awg_peers p JOIN awg_deployments d ON d.id=p.deployment_id
                JOIN awg_users u ON u.id=p.user_id LEFT JOIN awg_tombstones t ON t.peer_id=p.id
                WHERE NOT u.deleted OR (t.peer_id IS NOT NULL AND t.acknowledged_at IS NULL)
                ORDER BY u.upstream->>'username',p.id""")}

    @app.post('/api/v1/users/{user_uuid}/rotate-key',status_code=202)
    async def rotate_key(user_uuid:UUID,s=Depends(admin)):
        await s.rotate_user_key(user_uuid)
        return {'accepted':True}

    @app.get('/api/v1/remnawave/token')
    async def token(s=Depends(admin)):
        expires = getattr(s.upstream, 'token_expires_at', None)
        async with s.db.connect() as conn:
            last = await rows(conn,"SELECT code,created_at FROM awg_errors WHERE code IN ('REMNAWAVE_TOKEN_REJECTED','RECONCILE_FAILED') ORDER BY id DESC LIMIT 1")
        rejected = bool(last) and last[0]['code'] == 'REMNAWAVE_TOKEN_REJECTED' and (
            s.last_upstream_ok is None or last[0]['created_at'] > s.last_upstream_ok)
        return {'expires_at': expires, 'days_left': (expires - datetime.now(timezone.utc)).days if expires else None,
                'rejected': rejected, 'last_success': s.last_upstream_ok}

    @app.get('/api/v1/remnawave/squads')
    async def squads(s=Depends(admin)):
        return {'items':await s.upstream.squads()}

    @app.get('/api/v1/revisions')
    async def revisions(s=Depends(admin)):
        async with s.db.connect() as conn:
            return {'items':await rows(conn,'SELECT deployment_id,revision,digest,created_at FROM awg_revisions ORDER BY created_at DESC LIMIT 1000')}

    @app.get('/api/v1/errors')
    async def errors(s=Depends(admin)):
        async with s.db.connect() as conn:
            return {'items':await rows(conn,'SELECT * FROM awg_errors ORDER BY id DESC LIMIT 100')}

    @app.post('/v1/internal/subscriptions/material')
    async def material(value:SubscriptionRequest,s=Depends(gateway)):
        material = await s.material(value.subscription_token.get_secret_value())
        if material is None:
            raise HTTPException(404,'Unknown subscription')
        payload = material.model_dump(mode='json')
        for target, peer in zip(payload['peers'],material.peers):
            target['client_private_key'] = peer.client_private_key.get_secret_value()
        return JSONResponse(payload,headers={'Cache-Control':'no-store'})

    @app.post('/webhooks/remnawave',status_code=202)
    async def webhook(request:Request,s=Depends(service),x_remnawave_signature:str=Header(default='')):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>1024*1024:
                raise HTTPException(413,'Payload too large')
        if not verify_webhook(bytes(body),x_remnawave_signature,s.settings.webhook_secret):
            raise HTTPException(401,'Invalid signature')
        # Unsigned timestamp header cannot authorize mutation. Duplicate signed payloads
        # only set a coalescing Event; canonical state is fetched from API.
        s.wakeup.set()
        return {'accepted':True}

    @app.get('/metrics',response_class=PlainTextResponse)
    async def metrics(s=Depends(admin)):
        async with s.db.connect() as conn:
            counts = (await rows(conn,'SELECT count(*) AS total,count(*) FILTER(WHERE online) AS ready FROM awg_nodes'))[0]
            peers = (await rows(conn,'SELECT count(*) AS total,COALESCE(sum(rx_total),0) AS rx,COALESCE(sum(tx_total),0) AS tx FROM awg_peers'))[0]
        values = {'awg_nodes_total':counts['total'],'awg_nodes_ready':counts['ready'],'awg_peers_total':peers['total'],
            'awg_peer_rx_bytes':peers['rx'],'awg_peer_tx_bytes':peers['tx'],'awg_reconcile_runs_total':s.metrics['reconcile_runs'],
            'awg_reconcile_errors_total':s.metrics['reconcile_errors'],'awg_apply_total':s.metrics['apply'],'awg_apply_errors_total':s.metrics['apply_errors']}
        return ''.join(f'{key} {value}\n' for key,value in values.items())

    return app


app = create_app()
