#!/bin/sh
# Lab-only end-to-end check of install.sh / install-node.sh / uninstall.sh against the running stock lab.
# Everything lives in Docker bridge networks and artifacts/installer-lab; ports bind to 127.0.0.1 only.
# Run through `scripts/lab.sh installer` (host snapshot before/after is taken there).
set -eu
cd "$(dirname "$0")/.."
ROOT=$PWD/artifacts/installer-lab
NET=awg-dev_stock-api
EXT=$ROOT/ext
NODE=$ROOT/node
SUB=$ROOT/subpage
rm -rf "$ROOT"; mkdir -p "$SUB"
say() { printf '\033[1;35m[installer-test]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[installer-test] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }
cleanup() {
    status=$?
    [ "${KEEP:-0}" = 1 ] && exit "$status"  # leave containers for debugging
    [ -f "$ROOT/token" ] && stock_api "
old = api.get('/api/users/by-username/installer_awg')
if old.status_code == 200: api.delete('/api/users/%s' % old.json()['response']['id'])" >/dev/null 2>&1 || true
    [ -f "$EXT/compose.yaml" ] && ./uninstall.sh --dir "$EXT" --purge >/dev/null 2>&1 || true
    [ -f "$NODE/compose.yaml" ] && docker compose -f "$NODE/compose.yaml" --env-file "$NODE/.env" down -v >/dev/null 2>&1 || true
    [ -f "$SUB/docker-compose.yml" ] && docker compose -p remnawave-subpage --project-directory "$SUB" -f "$SUB/docker-compose.yml" down >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT INT TERM
in_net() { docker run --rm --network "$NET" -v "$ROOT:/r:ro" awg-dev-tooling:local python -c "$1"; }

docker run --rm -v awg-dev_lab-credentials:/c:ro awg-dev-tooling:local cat /c/stock-token > "$ROOT/token"
chmod 600 "$ROOT/token"
stock_api() {  # stock_api PYTHON: run against the stock API with the lab token (x-forwarded headers as a proxy)
    docker run --rm --network "$NET" -e TOKEN="$(cat "$ROOT/token")" awg-dev-tooling:local python -c "
import os, httpx
api = httpx.Client(base_url='http://awg-dev-remnawave-1:3000', timeout=20, headers={'Authorization': 'Bearer ' + os.environ['TOKEN'],
    'x-forwarded-proto': 'https', 'x-forwarded-for': '127.0.0.1'})
$1"
}
say 'Own test user in squad "AWG E2E" (works on a fresh CI stack too)'
SHORT=$(stock_api "
from datetime import datetime, timedelta, timezone
squads = api.get('/api/internal-squads').json()['response']['internalSquads']
squad = next((s['uuid'] for s in squads if s['name'] == 'AWG E2E'), None) or \
    api.post('/api/internal-squads', json={'name': 'AWG E2E', 'inbounds': []}).json()['response']['uuid']
old = api.get('/api/users/by-username/installer_awg')
if old.status_code == 200: api.delete('/api/users/%s' % old.json()['response']['id'])
exp = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
print(api.post('/api/users', json={'username': 'installer_awg', 'expireAt': exp, 'activeInternalSquads': [squad]}).json()['response']['shortUuid'])")
say 'Stock subscription page installed the official way (own compose project and .env)'
cat > "$SUB/docker-compose.yml" <<YAML
services:
  remnawave-subscription-page:
    image: remnawave/subscription-page:8.0.0
    container_name: remnawave-subscription-page
    env_file: [.env]
    networks: [remnawave-network]
networks:
  remnawave-network: {external: true, name: $NET}
YAML
printf 'APP_PORT=3010\nREMNAWAVE_PANEL_URL=http://awg-dev-remnawave-1:3000\nREMNAWAVE_API_TOKEN=%s\n' "$(cat "$ROOT/token")" > "$SUB/.env"
docker compose -p remnawave-subpage --project-directory "$SUB" -f "$SUB/docker-compose.yml" up -d

say 'install.sh (non-interactive)'
./install.sh --dir "$EXT" --network "$NET" --api-token-file "$ROOT/token" --subpage yes --webhook no --yes
grep -qx 'REMNAWAVE_PANEL_URL=http://awg-gateway:8081' "$SUB/.env" || fail 'subscription page not routed through the Gateway'
[ -s "$EXT/stock-changes" ] || fail 'changed stock keys are not recorded'
echo 'ADMIN_EDIT_AFTER_INSTALL=kept' >> "$SUB/.env"  # an operator edit that uninstall must not roll back
"$EXT/awg" status >/dev/null || fail 'TUI status command failed'

say 'Node key issued in the TUI container, then install-node.sh'
SECRET=$(docker compose -f "$EXT/compose.yaml" --env-file "$EXT/.env" --profile tools run --rm -T tui python -c \
  "import os,uuid; from pathlib import Path; from awg_tui.pki import node_bundle; print(node_bundle(Path('/pki-ca'), os.environ['AWG_CONTROLLER_ID'], uuid.uuid4()))")
printf '%s' "$SECRET" > "$ROOT/node.key"; chmod 600 "$ROOT/node.key"
AWG_NODE_INTERNAL_NETWORK=true ./install-node.sh --dir "$NODE" --secret-file "$ROOT/node.key" --management-port 18445 --management-bind 127.0.0.1 --udp-bind 127.0.0.1
NODE_ID=$(sed -n 's/^AWG_NODE_ID=//p' "$NODE/pki/node.env")
PORT=$(sed -n 's/^AWG_UDP_PORTS=//p' "$NODE/.env")
[ "$PORT" -ge 20000 ] && [ "$PORT" -le 59999 ] || fail "install-node.sh did not pick a random high UDP port: $PORT"
say "install-node.sh picked random UDP port $PORT"
# Lab only: the Controller reaches the node through the lab network instead of a public address.
docker network connect "$NET" awg-node-awg-agent-1

say 'Register node, create an AWG 3.1 profile for squad "AWG E2E" via the admin API (as the TUI does)'
ADMIN=$(sed -n 's/^AWG_ADMIN_TOKEN=//p' "$EXT/.env")
docker compose -f "$EXT/compose.yaml" --env-file "$EXT/.env" --profile tools run --rm -T -e NODE_ID="$NODE_ID" tui python - <<'PY'
import asyncio, os, uuid
from awg_config import random_parameters
from awg_tui.api import ControllerAPI
async def main():
    api = ControllerAPI(os.environ['AWG_CONTROLLER_URL'], os.environ['AWG_ADMIN_TOKEN'])
    node = os.environ['NODE_ID']
    await api.register_node({'node_id': node, 'name': 'installer-node', 'management_url': 'https://awg-node-awg-agent-1:8443'})
    squad = next(s['uuid'] for s in await api.squads() if s['name'] == 'AWG E2E')
    node_view = next(n for n in await api.items('nodes') if n['registration']['node_id'] == node)
    ports = node_view['capabilities']['listen_ports']
    assert len(ports) == 1, ports  # the random port published by install-node.sh, reported by the Agent
    wrong = {'profile_id': str(uuid.uuid4()), 'name': 'Wrong port', 'endpoint': {'host': 'node.example', 'port': ports[0] + 1},
             'network': {'ipv4_pool': '10.243.0.0/24', 'server_ipv4': '10.243.0.1'},
             'protocol': {'adapter_id': 'amneziawg-go-v3', 'version': '3.1', 'parameters': random_parameters('3.1')},
             'access': {'squad_ids': [squad]}, 'node_ids': [node]}
    await api.save_profile(wrong)
    assert not (await api.validate_profile(wrong['profile_id']))['valid'], 'a port not open on the node must fail Validate'
    print('validate rejects a port that is not open on the node')
    profile = {'profile_id': str(uuid.uuid4()), 'name': 'Installer | AWG 3.1', 'endpoint': {'host': 'node.example', 'port': ports[0]},
               'network': {'ipv4_pool': '10.242.0.0/24', 'server_ipv4': '10.242.0.1'},
               'protocol': {'adapter_id': 'amneziawg-go-v3', 'version': '3.1', 'parameters': random_parameters('3.1')},
               'access': {'squad_ids': [squad]}, 'node_ids': [node]}
    await api.save_profile(profile)
    result = await api.validate_profile(profile['profile_id'])
    assert result['valid'], result
    await api.apply_profile(profile['profile_id'], result['draft_digest'])
    for _ in range(60):
        nodes = await api.items('nodes')
        if any(d['state'] == 'READY' for n in nodes for d in n['deployments']):
            print('node READY'); return
        await asyncio.sleep(2)
    raise SystemExit('node never became READY')
asyncio.run(main())
PY
[ -n "$ADMIN" ] || fail 'no admin token'

say 'Subscription served by the STOCK subscription page now contains AWG (installer_awg is in the squad)'
docker run --rm --network "$NET" awg-dev-tooling:local python -c "
import time, httpx, yaml
PROXY = {'x-forwarded-proto': 'https', 'x-forwarded-for': '203.0.113.10'}  # as the TLS reverse proxy sends
r = None
for _ in range(60):
    try:
        r = httpx.get('http://remnawave-subscription-page:3010/$SHORT', headers={'user-agent': 'mihomo/1.19.31', **PROXY}, timeout=20)
    except httpx.HTTPError:
        time.sleep(2); continue  # the page restarts after install.sh changed its REMNAWAVE_PANEL_URL
    doc = yaml.safe_load(r.text) if r.status_code == 200 else {}
    names = [p.get('name') for p in (doc or {}).get('proxies') or []]
    if 'Installer | AWG 3.1' in names:
        print('subpage mihomo:', names); break
    time.sleep(2)
else:
    raise SystemExit('AWG missing from stock subscription page response: %r' % ((r.status_code, r.text[:300]) if r else None,))
r = httpx.get('http://remnawave-subscription-page:3010/$SHORT', headers={'user-agent': 'Happ/3.1.0', **PROXY}, timeout=20)
assert r.status_code == 200 and b'amneziawg://' not in r.content, 'Happ must get the untouched stock body'
print('subpage happ: stock only')
" || fail 'subscription page check'

say 'uninstall.sh restores the stock subscription page'
./uninstall.sh --dir "$EXT"
grep -qx 'REMNAWAVE_PANEL_URL=http://awg-dev-remnawave-1:3000' "$SUB/.env" || fail 'subscription page .env not restored'
grep -qx 'ADMIN_EDIT_AFTER_INSTALL=kept' "$SUB/.env" || fail 'uninstall rolled back an unrelated operator edit'
docker run --rm --network "$NET" awg-dev-tooling:local python -c "
import time, httpx
PROXY = {'x-forwarded-proto': 'https', 'x-forwarded-for': '203.0.113.10'}
for _ in range(30):
    try:
        r = httpx.get('http://remnawave-subscription-page:3010/$SHORT', headers={'user-agent': 'Happ/3.1.0', **PROXY}, timeout=10)
        if r.status_code == 200: print('stock subscription page works without the extension'); break
    except httpx.HTTPError: pass
    time.sleep(2)
else: raise SystemExit('stock subscription page broken after uninstall')
" || fail 'stock after uninstall'
say 'INSTALLER: PASS'
