from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from starlette.responses import Response, StreamingResponse

from awg_contracts import SubscriptionMaterial
from awg_capabilities import FAMILIES, identify
from subscription_renderers import RawAWGRenderer, accepts, enrich, enrich_info, info_document

log = logging.getLogger('uvicorn.error')

HOP_HEADERS = {b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
               b'te', b'trailer', b'transfer-encoding', b'upgrade'}
CONDITIONAL_HEADERS = {'if-match', 'if-none-match', 'if-modified-since', 'if-unmodified-since', 'if-range', 'range'}
BODY_VALIDATORS = {b'etag', b'last-modified', b'content-md5', b'digest', b'content-digest',
                   b'repr-digest', b'content-length', b'content-encoding', b'accept-ranges', b'age',
                   b'expires', b'cache-control', b'signature', b'signature-input'}


@dataclass(frozen=True)
class GatewaySettings:
    stock_url: str = 'http://remnawave:3000'
    controller_url: str = 'http://controller:8080'
    controller_token: str = field(default='', repr=False)
    metrics_token: str = field(default='', repr=False)
    subscription_prefix: str = '/api/sub/'
    max_enrichment_bytes: int = 2 * 1024 * 1024
    max_material_bytes: int = 1024 * 1024
    stock_timeout: float = 15.0
    enrichment_timeout: float = 2.0
    material_max_age: float = 30.0
    # Production: the Gateway is reachable only from a TLS reverse proxy or the stock subscription page,
    # so their X-Forwarded-* headers are authoritative. Never enable on a directly exposed Gateway.
    trust_forwarded: bool = False
    # Stock subscription page pointed at the Gateway (REMNAWAVE_PANEL_URL) also reads a few API routes.
    subpage_passthrough: bool = False

    def __post_init__(self):
        for value in (self.stock_url, self.controller_url):
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('gateway upstream URLs must be HTTP(S) origins without credentials')
        if not self.subscription_prefix.startswith('/') or not self.subscription_prefix.endswith('/'):
            raise ValueError('subscription_prefix must start and end with /')
        if min(self.max_enrichment_bytes, self.max_material_bytes, self.stock_timeout, self.enrichment_timeout, self.material_max_age) <= 0:
            raise ValueError('limits must be positive')

    @classmethod
    def from_env(cls):
        token = os.environ.get('AWG_GATEWAY_TOKEN', '')
        token_file = os.environ.get('AWG_GATEWAY_TOKEN_FILE')
        if token_file:
            with open(token_file, encoding='utf-8') as secret:
                token = secret.read().strip()
        return cls(stock_url=os.environ.get('AWG_GATEWAY_STOCK_URL', 'http://remnawave:3000'),
                   controller_url=os.environ.get('AWG_GATEWAY_CONTROLLER_URL', 'http://controller:8080'),
                   controller_token=token,
                   metrics_token=os.environ.get('AWG_GATEWAY_METRICS_TOKEN', ''),
                   subscription_prefix=os.environ.get('AWG_GATEWAY_SUBSCRIPTION_PREFIX', '/api/sub/'),
                   trust_forwarded=os.environ.get('AWG_GATEWAY_TRUST_FORWARDED') == '1',
                   subpage_passthrough=os.environ.get('AWG_GATEWAY_SUBPAGE_PASSTHROUGH') == '1')


FORWARDED = {b'x-forwarded-for', b'x-forwarded-proto'}
# Stock 3.4.x answers a blocked device (HWID limit reached / HWID missing) with HTTP 200 plus these
# headers and placeholder entries. Such a response must never be turned into working AWG access.
HWID_BLOCKED = ('x-hwid-limit', 'x-hwid-max-devices-reached', 'x-hwid-not-supported')


def stock_denied(response: httpx.Response) -> bool:
    return response.status_code != 200 or any(name in response.headers for name in HWID_BLOCKED)


def sanitize_headers(raw: list[tuple[bytes, bytes]], *, request=False, trust_forwarded=False) -> list[tuple[bytes, bytes]]:
    blocked = set(HOP_HEADERS)
    for name, value in raw:
        if name.lower() == b'connection':
            blocked.update(token.strip().lower() for token in value.split(b','))
    if request:
        blocked |= {b'host', b'content-length', b'x-user-uuid', b'x-remnawave-user-uuid', b'forwarded', b'x-forwarded-host',
                    b'x-real-ip'}
        if not trust_forwarded:
            blocked |= FORWARDED
    headers = [(name, value) for name, value in raw if name.lower() not in blocked
               and not (request and name.lower().startswith((b'x-awg-', b'x-internal-')))]
    if request and trust_forwarded and not any(name.lower() == b'x-forwarded-proto' for name, _ in headers):
        headers.append((b'x-forwarded-proto', b'https'))  # stock only serves requests marked as TLS-terminated
    return headers


def conf_filename(name: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-.')[:48]
    return (slug or 'amneziawg') + '.conf'


def plain_response(status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> Response:
    response = Response(body, status_code=status)
    response.raw_headers = headers
    if not any(name.lower() == b'content-length' for name, _ in headers) and status not in (204, 304):
        response.raw_headers.append((b'content-length', str(len(body)).encode()))
    return response


async def raw_chunks(response: httpx.Response):
    # In-memory MockTransport responses are already consumed; sockets use aiter_raw.
    if response.is_stream_consumed:
        if response.content:
            yield response.content
    else:
        async for chunk in response.aiter_raw():
            yield chunk


def create_app(settings: GatewaySettings | None = None, stock_client: httpx.AsyncClient | None = None,
               controller_client: httpx.AsyncClient | None = None) -> FastAPI:
    config = settings or GatewaySettings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=config.stock_timeout, follow_redirects=False, trust_env=False,
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)) as stock:
            async with httpx.AsyncClient(timeout=config.enrichment_timeout, follow_redirects=False, trust_env=False,
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=10)) as controller:
                app.state.stock = stock_client or stock
                app.state.controller = controller_client or controller
                yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_path = re.compile(re.escape(config.subscription_prefix.encode()) + rb'[A-Za-z0-9_-]{1,128}(?:/[A-Za-z0-9_-]{1,64}){0,2}')
    # Exact read-only routes the stock subscription page (8.0.0 backend-contract) calls with its own API token.
    # GET_ALL of subscription-page-configs is the controller root, i.e. with a trailing slash.
    subpage_paths = re.compile(rb'/api/(?:subscriptions/subpage-config/[A-Za-z0-9_-]{1,128}'
                               rb'|subscription-page-configs/?(?:[0-9A-Fa-f-]{36})?|system/metadata'
                               rb'|users/by-username/[A-Za-z0-9_-]{1,64})')
    prefix = re.escape(config.subscription_prefix)
    route = re.compile(prefix + r'(?P<token>[A-Za-z0-9_-]{1,128})(?:/(?P<suffix>[A-Za-z0-9_-]{1,64}))?')
    app.state.stock = stock_client
    app.state.controller = controller_client
    app.state.render_total = 0
    app.state.render_errors = 0

    @app.get('/health/live')
    async def live():
        return {'status': 'alive'}

    @app.get('/health/ready')
    async def ready():
        try:
            # A reachable stock server is sufficient; controller outage is fail-open.
            response = await app.state.stock.send(httpx.Request('HEAD', config.stock_url))
            await response.aclose()
            return Response(status_code=200 if response.status_code < 500 else 503)
        except (httpx.HTTPError, AttributeError):
            return Response(status_code=503)

    @app.get('/metrics')
    async def metrics(request: Request):
        supplied = request.headers.get('authorization', '')
        if not config.metrics_token or not hmac.compare_digest(supplied.encode(), ('Bearer ' + config.metrics_token).encode()):
            return Response(status_code=401, headers={'cache-control': 'no-store'})
        return Response(f'awg_subscription_render_total {app.state.render_total}\n'
                        f'awg_subscription_render_errors_total {app.state.render_errors}\n',
                        media_type='text/plain; version=0.0.4')

    async def material(token: str) -> SubscriptionMaterial:
        req = httpx.Request('POST', config.controller_url.rstrip('/') + '/v1/internal/subscriptions/material',
                            headers={'authorization': 'Bearer ' + config.controller_token,
                                     'accept': 'application/json'}, json={'subscription_token': token})
        result = await app.state.controller.send(req, stream=True, follow_redirects=False)
        try:
            result.raise_for_status()
            if result.headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('compressed material unsupported')
            chunks, size = [], 0
            async for chunk in raw_chunks(result):
                size += len(chunk)
                if size > config.max_material_bytes:
                    raise ValueError('material exceeds size limit')
                chunks.append(chunk)
            parsed = SubscriptionMaterial.model_validate_json(b''.join(chunks))
            if parsed.generated_at.tzinfo is None:
                raise ValueError('material requires timezone')
            age = (datetime.now(timezone.utc) - parsed.generated_at).total_seconds()
            if not -5 <= age <= config.material_max_age or len(parsed.peers) > 256:
                raise ValueError('material stale or oversized')
            return parsed
        finally:
            await result.aclose()
            app.state.controller.cookies.clear()

    async def send_stock(request: Request, raw_path: bytes, query: bytes, body: bytes = b''):
        headers = sanitize_headers(request.scope['headers'], request=True, trust_forwarded=config.trust_forwarded)
        url = httpx.URL(config.stock_url).copy_with(raw_path=raw_path + (b'?' + query if query else b''))
        upstream = await app.state.stock.send(httpx.Request(request.method, url, headers=headers, content=body or None),
                                               stream=True, follow_redirects=False)
        app.state.stock.cookies.clear()
        return upstream

    def stream_back(upstream, buffered, iterator):
        async def stream():
            try:
                for chunk in buffered:
                    yield chunk
                async for chunk in iterator:
                    yield chunk
            finally:
                await upstream.aclose()
        response = StreamingResponse(stream(), status_code=upstream.status_code)
        response.raw_headers = sanitize_headers(upstream.headers.raw)
        return response

    async def awg_conf(request: Request, token: str):
        """Plain .conf for the AmneziaWG app. Stock decides access first (auth, HWID, limits)."""
        query = request.scope['query_string']
        upstream = await send_stock(request, (config.subscription_prefix + token).encode(), b'')
        await upstream.aclose()
        if stock_denied(upstream) or not config.controller_token:
            return Response(status_code=upstream.status_code if upstream.status_code != 200 else 403,
                            headers={'cache-control': 'no-store'})
        try:
            index = int(dict(item.split('=', 1) for item in query.decode().split('&') if '=' in item).get('i', '0'))
            async with asyncio.timeout(config.enrichment_timeout):
                peers = [p for p in (await material(token)).peers if FAMILIES['amneziavpn'].supports(p.protocol)]
            peer = peers[index]
            body = RawAWGRenderer().entry(peer).encode()
        except Exception:
            app.state.render_errors += 1
            return Response(status_code=404, headers={'cache-control': 'no-store'})
        return Response(body, media_type='text/plain; charset=utf-8', headers={
            'cache-control': 'private, no-store',
            'content-disposition': f'attachment; filename="{conf_filename(peer.name)}"'})

    @app.api_route('/{path:path}', methods=['GET', 'HEAD'])
    async def proxy(request: Request, path: str):
        # Only stock subscription routes are exposed; stock admin/auth API stays unreachable.
        # Match the undecoded path so encoded traversal cannot slip past the allowlist.
        raw = request.scope.get('raw_path') or request.scope['path'].encode()
        passthrough = config.subpage_passthrough and request.method == 'GET' and subpage_paths.fullmatch(raw)
        if allowed_path.fullmatch(raw) is None and not passthrough:
            return Response(status_code=404, headers={'cache-control': 'no-store'})
        matched = None if passthrough else route.fullmatch(request.url.path)
        if matched and matched['suffix'] == 'awg' and request.method == 'GET':
            try:
                return await awg_conf(request, matched['token'])
            except httpx.HTTPError:
                return Response('Stock subscription unavailable', status_code=502, headers={'cache-control': 'no-store'})
        body = b''
        if passthrough:
            # The subscription page sends GET requests with a JSON body (e.g. subpage-config requestHeaders).
            async for chunk in request.stream():
                body += chunk
                if len(body) > 64 * 1024:
                    return Response(status_code=413, headers={'cache-control': 'no-store'})
        try:
            upstream = await send_stock(request, raw, request.scope['query_string'], body)
        except httpx.HTTPError:
            return Response('Stock subscription unavailable', status_code=502, headers={'cache-control': 'no-store'})
        response_headers = sanitize_headers(upstream.headers.raw)
        # Subscription-info JSON: the page's `/info`, or the root URL opened in a browser (stock answers
        # `Accept: text/html` with the same document). Both get the AWG links in `links`.
        info = matched is not None and (matched['suffix'] == 'info' or (
            matched['suffix'] is None and upstream.headers.get('content-type', '').startswith('application/json')))
        cap = identify(request.headers.get('user-agent', ''))
        client = 'page' if matched is not None and matched['suffix'] == 'info' else cap.family
        eligible = (request.method == 'GET' and not stock_denied(upstream) and matched is not None
                    and (info or cap.renderer is not None) and bool(config.controller_token)
                    and not CONDITIONAL_HEADERS.intersection(request.headers)
                    and upstream.headers.get('content-encoding', 'identity').lower() == 'identity'
                    and 'no-transform' not in upstream.headers.get('cache-control', '').lower())
        if matched is not None and request.method == 'GET' and not eligible:
            reason = ('stock-denied' if stock_denied(upstream) else 'no-awg-for-client' if cap.renderer is None and not info
                      else 'no-controller-token' if not config.controller_token else 'stock-encoded-or-conditional')
            log.info('subscription client=%s awg=skipped reason=%s', client, reason)
        iterator = raw_chunks(upstream).__aiter__()
        buffered, size = [], 0
        if eligible:
            try:
                async for chunk in iterator:
                    buffered.append(chunk)
                    size += len(chunk)
                    if size > config.max_enrichment_bytes:
                        eligible = False
                        break
            except httpx.HTTPError:
                await upstream.aclose()
                return Response('Stock subscription interrupted', status_code=502, headers={'cache-control': 'no-store'})
        stock = b''.join(buffered)
        if eligible and not (info_document(stock) if info else accepts(stock, cap)):
            log.info('subscription client=%s awg=skipped reason=stock-format-not-extendable', client)
            eligible = False
        if not eligible:
            return stream_back(upstream, buffered, iterator)
        await upstream.aclose()
        app.state.render_total += 1
        try:
            async with asyncio.timeout(config.enrichment_timeout):
                peers = await material(matched['token'])
                if info:
                    result = enrich_info(stock, peers.peers)
                else:
                    result = enrich(stock, upstream.headers.get('content-type', ''), cap, peers.peers)
            # Counts only, never tokens or keys. "no ready AWG" = user not in a profile squad, or the
            # profile's node is not READY/online (see `awg status`).
            log.info('subscription client=%s awg=%s ready_peers=%d', client,
                     'added' if result != stock else 'unchanged', len(peers.peers))
            if result != stock:
                response_headers = [(name, value) for name, value in response_headers if name.lower() not in BODY_VALIDATORS]
                response_headers.append((b'cache-control', b'private, no-store'))
                return plain_response(200, response_headers, result)
        except Exception as error:
            # Never include exception text: it may contain token URLs or peer keys.
            app.state.render_errors += 1
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            log.warning('subscription client=%s awg=failed error=%s%s', client, type(error).__name__,
                        f' controller_status={status}' if status else '')
        return plain_response(upstream.status_code, response_headers, stock)

    return app
