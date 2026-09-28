import httpx
import pytest
from remnawave_client import RemnawaveClient, UpstreamError


@pytest.mark.asyncio
async def test_failed_page_is_not_authoritative_empty():
    client = RemnawaveClient('https://panel.example/api', 'test', transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(UpstreamError):
        await client.users()
    await client.close()


@pytest.mark.asyncio
async def test_only_targeted_404_is_deletion():
    client = RemnawaveClient('https://panel.example/api', 'test', transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert await client.user('00000000-0000-0000-0000-000000000001') is None
    with pytest.raises(UpstreamError):
        await client.users()
    await client.close()


@pytest.mark.asyncio
async def test_short_identity_cannot_inject_path():
    client = RemnawaveClient('https://panel.example/api', 'test')
    with pytest.raises(ValueError):
        await client.by_short_uuid('../users/admin')
    await client.close()


def upstream_v3_user(**changes):
    # Shape of GET /api/users/{id} in stock Remnawave 3.4.4 (numeric id, no uuid field).
    data = {'id': 42, 'shortUuid': 'AbCdEf123', 'username': 'alice', 'status': 'ACTIVE',
            'trafficLimitBytes': 0, 'trafficLimitStrategy': 'NO_RESET', 'expireAt': '2099-01-01T00:00:00.000Z',
            'lastTrafficResetAt': None, 'activeInternalSquads': [{'uuid': '00000000-0000-4000-8000-00000000000a', 'name': 'x'}],
            'userTraffic': {'usedTrafficBytes': 5, 'lifetimeUsedTrafficBytes': 5}, 'subscriptionUrl': 'https://sub/x'}
    data.update(changes)
    return data


def test_v3_numeric_identity_maps_to_stable_uuid():
    from remnawave_client import User, user_uuid_for
    first = User.model_validate(upstream_v3_user())
    assert first.user_uuid == user_uuid_for(42) == User.model_validate(upstream_v3_user(username='renamed')).user_uuid
    assert first.upstream_id == '42'
    assert User.model_validate(upstream_v3_user(id=43)).user_uuid != first.user_uuid
    assert [str(s) for s in first.squad_ids] == ['00000000-0000-4000-8000-00000000000a']


@pytest.mark.asyncio
async def test_targeted_lookup_uses_upstream_id_and_rejects_path_injection():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={'response': upstream_v3_user()})
    client = RemnawaveClient('https://panel.example/api', 'test', transport=httpx.MockTransport(handler))
    assert (await client.user('42')).upstream_id == '42'
    assert seen == ['/api/users/42']
    with pytest.raises(ValueError):
        await client.user('42/../../admin')
    await client.close()


@pytest.mark.asyncio
async def test_squads_by_name_and_internal_http_marks_proxy():
    def handler(request):
        assert request.headers['x-forwarded-proto'] == 'https'
        return httpx.Response(200, json={'response': {'total': 1, 'internalSquads': [
            {'uuid': '00000000-0000-4000-8000-00000000000a', 'name': 'AWG', 'info': {'membersCount': 3, 'inboundsCount': 0}}]}})
    client = RemnawaveClient('http://remnawave:3000/api', 'test', transport=httpx.MockTransport(handler))
    assert await client.squads() == [{'uuid': '00000000-0000-4000-8000-00000000000a', 'name': 'AWG', 'members': 3}]
    await client.close()
