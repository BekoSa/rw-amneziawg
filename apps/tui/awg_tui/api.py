"""Thin async client for the Controller admin API. Error bodies are never shown: they may echo secrets."""
from __future__ import annotations

import httpx

MESSAGES = {401: 'токен администратора отклонён', 404: 'объект не найден', 409: 'операция отклонена Controller',
            422: 'данные не прошли проверку схемы', 503: 'зависимость Controller недоступна (Remnawave или Agent)'}


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
