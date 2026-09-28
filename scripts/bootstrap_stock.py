"""Bootstrap disposable stock only through its public HTTP API; no stock SQL."""
import json
from pathlib import Path
import secrets
import httpx

ROOT = Path('/lab-credentials')


def main():
    ROOT.mkdir(exist_ok=True)
    credentials = ROOT / 'stock-admin.json'
    if credentials.exists():
        admin = json.loads(credentials.read_text())
    else:
        admin = {'username': 'awglabadmin', 'password': secrets.token_urlsafe(32)}
        credentials.write_text(json.dumps(admin))
        credentials.chmod(0o600)
    token_path = ROOT / 'stock-token'
    with httpx.Client(base_url='http://stock-proxy:8080', timeout=20, trust_env=False) as client:
        if token_path.exists():
            probe = client.get('/api/users', params={'size': 1}, headers={'Authorization': 'Bearer ' + token_path.read_text().strip()})
            if probe.status_code == 200:
                print('Stock API token already provisioned')
                return
        status = client.get('/api/auth/status')
        status.raise_for_status()
        # Register is allowed only on an empty, project-owned stock database.
        if status.json()['response']['isRegisterAllowed']:
            result = client.post('/api/auth/register', json=admin)
        else:
            result = client.post('/api/auth/login', json=admin)
        if result.status_code not in (200, 201):
            raise RuntimeError(f'Stock authentication failed: HTTP {result.status_code}')
        # Stock 3.x accepts admin session JWTs only from its dashboard. This one call stands in for the
        # operator creating an API token in the dashboard; the extension itself only uses that token.
        session = {'Authorization': 'Bearer ' + result.json()['response']['accessToken'],
                   'x-remnawave-client-type': 'browser'}
        created = client.post('/api/tokens', headers=session, json={'name': 'awg-extension-lab', 'expiresInDays': 30})
        if created.status_code not in (200, 201):
            raise RuntimeError(f'Stock API token creation failed: HTTP {created.status_code}')
        token_path.write_text(created.json()['response']['token'])
        token_path.chmod(0o644)  # Named volume mounted only into trusted lab processes.
        print('Stock public API token provisioned; credentials remain in named volume')


if __name__ == '__main__':
    main()
