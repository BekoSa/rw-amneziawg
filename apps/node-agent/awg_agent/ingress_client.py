"""Authenticated local control client; it never accepts a remote destination."""
from __future__ import annotations
import hashlib
import hmac
import json
import threading
from pathlib import Path
import httpx
from awg_contracts import IngressPrepared, NodeActualState, NodeValidationResult


class IngressError(RuntimeError):
    def __init__(self, code='INGRESS_UNAVAILABLE'):
        self.code = code
        super().__init__(code)


class IngressClient:
    def __init__(self, socket_path, secret_path, envelope, timeout=5.0):
        self.envelope = envelope
        self.secret = Path(secret_path).read_bytes().strip()
        if len(self.secret) < 32:
            raise ValueError('Invalid ingress credential')
        self.http = httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)),
                                 base_url='http://ingress', timeout=timeout, trust_env=False)
        self.lock = threading.RLock()
        self.session = None
        self.nonce = 0

    def _request(self, method, path, payload=None):
        body = json.dumps(payload.model_dump(mode='json'), sort_keys=True, separators=(',', ':')).encode() if payload is not None else b''
        with self.lock:
            try:
                for attempt in range(2):
                    if self.session is None:
                        response = self.http.get('/v1/ingress/session')
                        response.raise_for_status()
                        self.session = response.json()
                        self.nonce = int(self.session['next_nonce']) - 1
                    self.nonce += 1
                    message = '\n'.join([self.session['session_id'], self.session['challenge'], method, path,
                                         str(self.nonce), hashlib.sha256(body).hexdigest()]).encode()
                    headers = {'X-Ingress-Session': self.session['session_id'], 'X-Ingress-Nonce': str(self.nonce),
                               'X-Ingress-MAC': hmac.new(self.secret, message, hashlib.sha256).hexdigest(),
                               'Content-Type': 'application/json'}
                    response = self.http.request(method, path, content=body, headers=headers)
                    if response.status_code == 401 and attempt == 0:
                        self.session = None
                        continue
                    if not response.is_success:
                        code = response.json().get('code', 'INGRESS_UNAVAILABLE')
                        allowed = {'PORT_IN_USE','STALE_REVISION','REVISION_CONFLICT','APPLIED_REVISION_MISMATCH',
                                   'ENVELOPE_MISMATCH','TICKET_MISMATCH','INGRESS_UNAVAILABLE'}
                        raise IngressError(code if code in allowed else 'INGRESS_UNAVAILABLE')
                    if response.status_code == 204 and path == '/v1/ingress/abort':
                        return None
                    return response.json()
            except IngressError:
                raise
            except Exception:
                self.session = None
                raise IngressError() from None

    def validate(self, desired):
        return NodeValidationResult.model_validate(self._request('POST','/v1/ingress/validate',desired))
    def prepare(self, request):
        return IngressPrepared.model_validate(self._request('POST','/v1/ingress/prepare',request))
    def commit(self, request):
        return NodeActualState.model_validate(self._request('POST','/v1/ingress/commit',request))
    def abort(self, request):
        self._request('POST','/v1/ingress/abort',request)
    def state(self):
        return NodeActualState.model_validate(self._request('GET','/v1/ingress/state'))
    def close(self):
        self.http.close()
