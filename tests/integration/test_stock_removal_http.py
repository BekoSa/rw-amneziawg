"""Stock HTTP removal smoke without TUN; complete AWG restoration stays in live E2E."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.network,
    pytest.mark.skipif(not os.environ.get('STOCK_HTTP_PHASE'), reason='isolated stock HTTP lab required')]
PROFILE = '00000000-0000-4000-8000-0000000000b9'
NAME = 'removal_http_smoke'


def test_stock_extension_removal_and_restoration():
    phase = os.environ['STOCK_HTTP_PHASE']
    token = Path('/lab-credentials/stock-token').read_text().strip()
    with httpx.Client(base_url='http://stock-proxy:8080', trust_env=False, timeout=15,
            headers={'Authorization': 'Bearer ' + token}) as stock:
        assert stock.get('/api/auth/status').status_code == 200
        response = stock.get('/api/users/by-username/' + NAME)
        if response.status_code == 404:
            response = stock.post('/api/users', json={'username': NAME,
                'expireAt': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        user = response.raise_for_status().json()['response']
        assert user['username'] == NAME
        direct = stock.get('/api/sub/' + user['shortUuid'], headers={'user-agent': 'Happ/3.1.0'})
        assert direct.status_code == 200
        with httpx.Client(base_url='http://controller:8080', timeout=15, trust_env=False,
                headers={'Authorization': 'Bearer lab-admin-token'}) as extension:
            if phase == 'removed':
                with pytest.raises(httpx.HTTPError):
                    extension.get('/health/ready').raise_for_status()
                with pytest.raises(httpx.HTTPError):
                    httpx.get('http://gateway:8081/health/live', trust_env=False, timeout=3).raise_for_status()
                return
            extension.get('/health/ready').raise_for_status()
            if phase == 'before':
                extension.put('/api/v1/trusttunnel/profiles/' + PROFILE, json={
                    'profile_id': PROFILE, 'name': 'durable HTTP removal draft',
                    'endpoint': {'host': 'vpn.example.org', 'port': 14443},
                    'tls': {'domain': 'vpn.example.org', 'email': 'lab@example.org'},
                    'access': {'user_ids': []}, 'node_ids': []}).raise_for_status()
            profiles = extension.get('/api/v1/trusttunnel/profiles').raise_for_status().json()
            assert any(str(row.get('profile_id', row.get('id'))) == PROFILE for row in profiles['items'])
            extension.post('/api/v1/reconcile').raise_for_status()
            enriched = httpx.get('http://gateway:8081/api/sub/' + user['shortUuid'],
                headers={'user-agent': 'Happ/3.1.0'}, trust_env=False, timeout=15)
            assert enriched.status_code == direct.status_code
            assert enriched.content == direct.content
