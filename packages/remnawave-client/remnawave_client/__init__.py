"""Narrow read-only adapter for documented Remnawave public API.

Remnawave 3.x identifies users by a numeric `id` (no `uuid` field); 2.x used `uuid`.
The extension keys everything by a stable UUID: 2.x uses the upstream UUID verbatim,
3.x derives UUIDv5 from the numeric id. `upstream_id` keeps the value needed for
targeted API lookups. Nothing outside this module depends on the upstream DTO shape.
"""
from datetime import datetime
from uuid import UUID, uuid5
import hashlib
import hmac
import re
import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from awg_contracts import RemnawaveUser


class UpstreamError(RuntimeError):
    pass


class Squad(BaseModel):
    uuid: UUID


class Traffic(BaseModel):
    usedTrafficBytes: int = Field(ge=0)


USER_NAMESPACE = UUID('6f1c0a52-8a4e-5d0f-9d57-3a1f3c1c7e21')


def user_uuid_for(upstream_id: int) -> UUID:
    return uuid5(USER_NAMESPACE, f'remnawave-user:{upstream_id}')


class UpstreamUser(BaseModel):
    id: int | None = Field(default=None, ge=1)
    uuid: UUID | None = None
    shortUuid: str = Field(repr=False)
    username: str
    status: str
    expireAt: datetime
    activeInternalSquads: list[Squad]
    trafficLimitBytes: int = Field(ge=0)
    userTraffic: Traffic
    lastTrafficResetAt: datetime | None = None

    @model_validator(mode='after')
    def identified(self):
        if self.id is None and self.uuid is None:
            raise ValueError('upstream user has no identity')
        return self

    @field_validator('expireAt', 'lastTrafficResetAt')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('upstream time must have timezone')
        return value


class User(RemnawaveUser):
    last_traffic_reset_at: datetime | None = None

    @model_validator(mode='before')
    @classmethod
    def normalize(cls, data):
        if isinstance(data, dict) and ('uuid' in data or 'id' in data) and 'user_uuid' not in data:
            value = UpstreamUser.model_validate(data)
            identity = value.uuid if value.uuid is not None else user_uuid_for(value.id)
            upstream_id = str(value.uuid) if value.uuid is not None else str(value.id)
            return dict(user_uuid=identity, upstream_id=upstream_id, username=value.username, status=value.status,
                expire_at=value.expireAt, squad_ids=[s.uuid for s in value.activeInternalSquads],
                traffic_used_bytes=value.userTraffic.usedTrafficBytes, traffic_limit_bytes=value.trafficLimitBytes,
                subscription_id=value.shortUuid, last_traffic_reset_at=value.lastTrafficResetAt)
        return data


def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)


class RemnawaveClient:
    def __init__(self, base_url: str, token: str, *, transport=None):
        headers = {'Authorization': 'Bearer '+token}
        if base_url.startswith('http://'):
            # Internal Docker-network access to stock, as the official subscription page does it.
            headers.update({'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'})
        self.http = httpx.AsyncClient(base_url=base_url.rstrip('/')+'/',
            headers=headers, timeout=15, follow_redirects=False,
            transport=transport, trust_env=False)

    async def close(self):
        await self.http.aclose()

    async def _get(self, path, *, missing=False, params=None):
        try:
            response = await self.http.get(path, params=params)
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or 'response' not in body:
                raise ValueError('missing response envelope')
            return body['response']
        except (httpx.HTTPError, ValueError):
            raise UpstreamError('Remnawave fetch failed') from None

    async def user(self, upstream_id):
        text = str(upstream_id)
        if not (text.isascii() and text.isdigit() and int(text) > 0):
            text = str(UUID(text))  # 2.x identity; anything else is rejected before building a path.
        value = await self._get('users/'+text, missing=True)
        try:
            return None if value is None else User.model_validate(value)
        except ValueError:
            raise UpstreamError('Invalid Remnawave user') from None

    async def by_short_uuid(self, short_uuid):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', short_uuid):
            raise ValueError('invalid subscription identity')
        value = await self._get('users/by-short-uuid/'+short_uuid, missing=True)
        try:
            user = None if value is None else User.model_validate(value)
            if user is not None and user.subscription_id != short_uuid:
                raise UpstreamError('Subscription identity mismatch')
            return user
        except ValueError:
            raise UpstreamError('Invalid Remnawave user') from None

    async def squads(self):
        """Internal Squads (id, name, member count) for choosing AWG entitlements by name."""
        data = await self._get('internal-squads')
        try:
            return [{'uuid': str(UUID(str(item['uuid']))), 'name': str(item['name']),
                     'members': int((item.get('info') or {}).get('membersCount', 0))} for item in data['internalSquads']]
        except (KeyError, TypeError, ValueError):
            raise UpstreamError('Invalid Remnawave squads') from None

    async def users(self):
        users = {}
        start = 0
        for _ in range(10000):
            data = await self._get('users', params={'start': start, 'size': 500})
            try:
                page = [User.model_validate(value) for value in data['users']]
                total = int(data['total'])
                if total < 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise UpstreamError('Invalid Remnawave page') from None
            for user in page:
                users[user.user_uuid] = user
            start += len(page)
            if start >= total:
                return users
            if not page:
                raise UpstreamError('Incomplete Remnawave pagination')
        raise UpstreamError('Remnawave pagination limit exceeded')
