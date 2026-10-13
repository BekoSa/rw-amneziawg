"""Thin async client for the Controller admin API. Error bodies are never shown: they may echo secrets."""
from __future__ import annotations

import httpx

MESSAGES = {401: 'токен администратора отклонён', 404: 'объект не найден', 409: 'операция отклонена Controller',
            422: 'данные не прошли проверку схемы', 503: 'зависимость Controller недоступна (Remnawave или Agent)'}


REASONS = {
    'TRUSTTUNNEL_UNSUPPORTED': 'TrustTunnel не установлен на ноде: выполните одноразовую миграцию ноды',
    'UNSUPPORTED_NODE_API': 'нода не поддерживает managed resources: требуется одноразовая миграция',
    'NODE_MIGRATION_REQUIRED': 'требуется одноразовая миграция ноды обновлённым установщиком',
    'PORT_IN_USE': 'порт занят другим сервисом: выберите свободный порт в профиле',
    'RESERVED_CONTROL_PORT': 'порт зарезервирован для управления нодой: выберите другой',
    'BACKEND_NOT_READY': 'runtime протокола не готов: проверьте состояние ноды',
    'INGRESS_UNAVAILABLE': 'ingress ноды недоступен: проверьте состояние ноды',
    'ENVELOPE_MISMATCH': 'границы установки ноды не совпадают: требуется проверка миграции',
    'ACME_INGRESS_UNAVAILABLE': 'для ACME нет доступного TCP 80/443: выберите доступный challenge',
    'AGENT_TIMEOUT': 'нода не отвечает: проверьте адрес, порт управления, firewall провайдера и правило DOCKER-USER',
    'AGENT_UNREACHABLE': 'нода недоступна по сети: проверьте адрес ноды (DNS/IP) и маршрут',
    'AGENT_REFUSED': 'порт управления закрыт: Agent не запущен или опубликован на другом порту (install-node.sh --management-port)',
    'AGENT_TLS_UNTRUSTED': 'сертификат ноды не от этой установки или ID ноды не совпадает — выпустите новый ключ',
    'AGENT_TLS_FAILED': 'ошибка TLS при подключении к ноде',
    'AGENT_REJECTED_CONTROLLER': 'нода не приняла Controller: её ключ выпущен другой установкой расширения',
    'AGENT_IDENTITY_MISMATCH': 'по этому адресу отвечает другая нода: проверьте ID ноды',
    'AGENT_HTTP_ERROR': 'Agent вернул ошибку', 'AGENT_BAD_RESPONSE': 'Agent ответил некорректно',
    'REMNAWAVE_TOKEN_REJECTED': 'Remnawave отклонил API-токен (истёк?): sudo /opt/awg-extension/awg set-token',
    'REMNAWAVE_UNAVAILABLE': 'Remnawave недоступен',
}


class APIError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class ControllerAPI:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=base_url.rstrip('/'), headers={'Authorization': 'Bearer ' + token},
                                      timeout=60, trust_env=False, follow_redirects=False, transport=transport)

    async def close(self):
        await self.http.aclose()

    async def call(self, method: str, path: str, body=None):
        try:
            response = await self.http.request(method, path, json=body)
        except httpx.HTTPError:
            raise APIError('Controller недоступен') from None
        if response.status_code >= 400:
            try:
                payload = response.json()
                reason = payload.get('reason') if isinstance(payload, dict) else None
            except ValueError:
                reason = None
            if reason in REASONS:
                raise APIError(REASONS[reason], status_code=response.status_code)
            raise APIError(f'{MESSAGES.get(response.status_code, "ошибка Controller")} (HTTP {response.status_code})',
                           status_code=response.status_code)
        return response.json() if response.content else {}

    async def items(self, name: str) -> list[dict]:
        return (await self.call('GET', f'/api/v1/{name}'))['items']

    async def squads(self) -> list[dict]:
        return await self.items('remnawave/squads')

    async def save_profile(self, profile: dict):
        return await self.call('PUT', f'/api/v1/profiles/{profile["profile_id"]}', profile)

    async def validate_profile(self, profile_id: str) -> dict:
        return await self.call('POST', f'/api/v1/profiles/{profile_id}/validate')

    async def apply_profile(self, profile_id: str, digest: str):
        return await self.call('POST', f'/api/v1/profiles/{profile_id}/apply', {'expected_digest': digest})

    async def register_node(self, registration: dict):
        return await self.call('POST', '/api/v1/nodes', registration)

    async def reconcile(self):
        return await self.call('POST', '/api/v1/reconcile')

    async def rotate_key(self, user_uuid: str):
        return await self.call('POST', f'/api/v1/users/{user_uuid}/rotate-key')

    async def save_trusttunnel_profile(self, profile: dict):
        return await self.call('PUT', f'/api/v1/trusttunnel/profiles/{profile["profile_id"]}', profile)

    async def validate_trusttunnel_profile(self, profile_id: str):
        return await self.call('POST', f'/api/v1/trusttunnel/profiles/{profile_id}/validate')

    async def apply_trusttunnel_profile(self, profile_id: str, digest: str):
        return await self.call('POST', f'/api/v1/trusttunnel/profiles/{profile_id}/apply', {'expected_digest': digest})

    async def rotate_trusttunnel_credentials(self, user_uuid: str):
        return await self.call('POST', f'/api/v1/trusttunnel/users/{user_uuid}/credentials/rotate')

    async def export_trusttunnel_peer(self, peer_id: str, export_format: str):
        return await self.call('POST', f'/api/v1/trusttunnel/peers/{peer_id}/export', {'format': export_format})

    async def node_resources(self, node_id: str, enabled_protocols=None):
        query = '' if enabled_protocols is None else '?enabled_protocols=' + ','.join(enabled_protocols)
        return await self.call('GET', f'/api/v1/nodes/{node_id}/resources' + query)

    async def node_resource_capabilities(self, node_id: str):
        return await self.call('GET', f'/api/v1/nodes/{node_id}/resources/capabilities')

    async def validate_node_resources(self, node_id: str, desired: dict):
        return await self.call('POST', f'/api/v1/nodes/{node_id}/resources/validate', desired)

    async def apply_node_resources(self, node_id: str, desired: dict, applied_revision):
        return await self.call('PUT', f'/api/v1/nodes/{node_id}/resources',
                               {'desired': desired, 'expected_applied_revision': applied_revision})
