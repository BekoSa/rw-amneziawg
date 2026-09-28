"""Credentials for reading private material must never grant administration."""
from types import SimpleNamespace

import httpx
import pytest

from awg_controller.app import create_app


def controller_app():
    # Authentication/validation must reject before any DB or upstream side effect.
    service = SimpleNamespace(settings=SimpleNamespace(
        admin_token='test-admin-credential', gateway_token='test-gateway-credential'))
    return create_app(service)


@pytest.mark.asyncio
@pytest.mark.parametrize('authorization', ['', 'Bearer wrong', 'Bearer test-gateway-credential'])
async def test_gateway_credential_cannot_administer_nodes(authorization):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=controller_app()),
                                base_url='http://controller') as client:
        result = await client.get('/api/v1/nodes', headers={'authorization': authorization})
    assert result.status_code == 401
    assert result.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_admin_credential_cannot_read_internal_subscription_keys():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=controller_app()),
                                base_url='http://controller') as client:
        result = await client.post('/v1/internal/subscriptions/material',
                                   headers={'authorization': 'Bearer test-admin-credential'},
                                   json={'subscription_token': 'test-short-uuid'})
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_subscription_validation_does_not_echo_private_input():
    secret = 'secret-that-must-never-appear-in-an-error'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=controller_app()),
                                base_url='http://controller') as client:
        result = await client.post('/v1/internal/subscriptions/material',
                                   headers={'authorization': 'Bearer test-gateway-credential'},
                                   json={'subscription_token': {'private_key': secret}})
    assert result.status_code == 422
    assert secret not in result.text
    assert 'input' not in result.json()
    assert result.headers['x-correlation-id'] == result.json()['correlation_id']


@pytest.mark.asyncio
async def test_admin_validation_does_not_echo_private_input():
    secret = 'accidentally-pasted-server-private-key'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=controller_app()),
                                base_url='http://controller') as client:
        result = await client.put('/api/v1/profiles/00000000-0000-0000-0000-000000000001',
                                  headers={'authorization': 'Bearer test-admin-credential'},
                                  json={'server_private_key': secret})
    assert result.status_code == 422
    assert secret not in result.text
    assert result.json()['code'] == 'INVALID_REQUEST'
