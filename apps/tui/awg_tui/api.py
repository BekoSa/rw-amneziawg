"""Thin async client for the Controller admin API. Error bodies are never shown: they may echo secrets."""
from __future__ import annotations

import httpx

MESSAGES = {401: 'токен администратора отклонён', 404: 'объект не найден', 409: 'операция отклонена Controller',
            422: 'данные не прошли проверку схемы', 503: 'зависимость Controller недоступна (Remnawave или Agent)'}


REASONS = {
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
    pass


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
                reason = response.json().get('reason')
            except ValueError:
                reason = None
            if reason in REASONS:
                raise APIError(REASONS[reason])
            raise APIError(f'{MESSAGES.get(response.status_code, "ошибка Controller")} (HTTP {response.status_code})')
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
