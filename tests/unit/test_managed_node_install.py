"""Managed installation confines host networking and restores the old install on failure."""
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_only_ingress_uses_production_host_network():
    compose = yaml.safe_load((ROOT / 'deploy/production/node.managed.compose.yaml').read_text())
    agent, ingress = compose['services']['awg-agent'], compose['services']['node-ingress']
    assert agent.get('network_mode') != 'host'
    assert ingress['network_mode'] == 'host'
    assert ingress['cap_drop'] == ['ALL']
    assert ingress['cap_add'] == ['NET_BIND_SERVICE']
    assert 'NET_ADMIN' not in ingress['cap_add']
    assert not ingress.get('devices')
    assert len(agent['ports']) == 1 and agent['ports'][0].endswith(':8443/tcp')
    assert agent['environment']['NODE_INSTALL_ENVELOPE'] == '/node-config/envelope.json'
    assert agent['networks']['awg']['ipv4_address'] == '${NODE_BACKEND_IP:?run install-node.sh}'
    for service in compose['services'].values():
        assert not service.get('privileged')
        assert not any('docker.sock' in str(volume) for volume in service.get('volumes', []))


def test_failed_managed_activation_restores_existing_install(tmp_path):
    install = tmp_path / 'node'
    (install / 'pki').mkdir(parents=True)
    identity = '00000000-0000-4000-8000-000000000031'
    (install / 'pki/agent.crt').write_text('existing certificate')
    (install / 'pki/node.env').write_text(f'AWG_NODE_ID={identity}\nAWG_NODE_MANAGEMENT_PORT=2525\n')
    old = {'compose.yaml': '# old working compose\n', '.env': 'AWG_MANAGEMENT_PORT=2525\nAWG_UDP_PORTS=49008\n'}
    for name, contents in old.items():
        (install / name).write_text(contents)
    fake = tmp_path / 'bin'
    fake.mkdir()
    log = tmp_path / 'docker.log'
    docker = fake / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$* agent=${AWG_AGENT_IMAGE:-} ingress=${AWG_INGRESS_IMAGE:-}" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *'inspect --format'*'awg-node-awg-agent-1'*) echo 'sha256:old-agent'; exit 0;;
  *'inspect --format'*'awg-node-node-ingress-1'*) echo 'sha256:old-ingress'; exit 0;;
  *'network inspect'*) echo '172.29.64.0/24'; exit 0;;
  *'provision_managed_node.py'*) printf 'NODE_BACKEND_IP=172.29.64.2\\nNODE_BRIDGE_SUBNET=172.29.64.0/24\\n'; exit 0;;
  *'compose'*' up '*)
    if grep -q 'node-ingress:' "$AWG_INSTALL_DIR/compose.yaml"; then echo 'forced managed activation failure' >&2; exit 1; fi;;
esac
exit 0
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'], FAKE_DOCKER_LOG=str(log),
        AWG_INSTALL_DIR=str(install), AWG_INSTALL_SOURCE=str(ROOT),
        AWG_INSTALL_RAW_BASE=str(ROOT), AWG_INSTALL_REGISTRY='test/extension',
        AWG_INSTALL_IMAGE_TAG='local', AWG_INSTALL_NO_PULL='1', AWG_INSTALL_MANAGEMENT_BIND='192.168.3.4', AWG_INSTALL_UDP_BIND='192.168.3.4')
    result = subprocess.run(['sh', str(ROOT / 'scripts/install-managed-node.sh')],
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'forced managed activation failure' in result.stderr
    for name, contents in old.items():
        assert (install / name).read_text() == contents
    assert (install / 'pki/agent.crt').read_text() == 'existing certificate'
    rollback = [line for line in log.read_text().splitlines() if ' up ' in line][-1]
    assert 'agent=sha256:old-agent ingress=sha256:old-ingress' in rollback
    assert '--bind-address 192.168.3.4' in log.read_text()
    assert log.read_text().count(' up ') == 2, 'new plan must fail, then the old plan must restart'
