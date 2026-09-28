import copy
import pytest
from scripts.check_network_safety import violations

SAFE = {'services': {'app': {'networks': {'lab': {}}}}, 'networks': {'lab': {'driver': 'bridge', 'internal': True}}}

@pytest.mark.parametrize('key,value', [
    ('network_mode', 'host'), ('network_mode', 'container:victim'),
    ('privileged', True), ('pid', 'host'), ('ipc', 'host'),
    ('uts', 'host'), ('cap_add', ['SYS_ADMIN']),
    ('devices', [{'source': '/dev/sda', 'target': '/dev/sda'}]),
    ('ports', [{'target': 80, 'published': '80'}]),
    ('security_opt', ['seccomp=unconfined']),
])
def test_dangerous_service_rejected(key, value):
    config = copy.deepcopy(SAFE)
    config['services']['app'][key] = value
    assert violations(config)

@pytest.mark.parametrize('source', ['/proc', '/sys', '/lib/modules', '/var/run/netns', '/var/run/docker.sock', '/', '/proc/1/ns/net', '/sys/../proc'])
def test_host_mount_rejected(source):
    config = copy.deepcopy(SAFE)
    config['services']['app']['volumes'] = [{'type': 'bind', 'source': source, 'target': '/host', 'read_only': True}]
    assert violations(config)

def test_volume_driver_bind_bypass_rejected():
    config = copy.deepcopy(SAFE)
    config['volumes'] = {'escape': {'driver_opts': {'type': 'none', 'device': '/', 'o': 'bind'}}}
    assert violations(config)

def test_safe_bridge():
    assert not violations(SAFE)

@pytest.mark.parametrize('patch', [
    {'build': {'network': 'host'}}, {'cgroup': 'host'},
])
def test_indirect_host_namespace_rejected(patch):
    config = copy.deepcopy(SAFE)
    config['services']['app'].update(patch)
    assert violations(config)

def test_network_driver_options_rejected():
    config = copy.deepcopy(SAFE)
    config['networks']['lab']['driver_opts'] = {'com.docker.network.bridge.name': 'vpn0'}
    assert violations(config)

def test_tooling_bind_must_match_exact_workspace():
    config = {'services': {'tooling': {'volumes': [{'type': 'bind', 'source': '/etc', 'target': '/workspace', 'read_only': True}]}}}
    assert violations(config, '/home/project')


def test_safe_baseline_passes():
    assert not violations(copy.deepcopy(SAFE))


def test_non_internal_network_only_for_hardened_ui():
    config = copy.deepcopy(SAFE)
    config['networks']['lab']['internal'] = False
    assert violations(config), 'arbitrary non-internal network'
    config = copy.deepcopy(SAFE)
    config['networks']['ui-publish'] = {'driver': 'bridge'}
    config['services']['app']['networks']['ui-publish'] = {}
    assert violations(config), 'only the panel proxy may join ui-publish'
    ui = {'networks': {'lab': {}, 'ui-publish': {}}, 'read_only': True, 'security_opt': ['no-new-privileges:true']}
    config = copy.deepcopy(SAFE)
    config['networks']['ui-publish'] = {'driver': 'bridge'}
    config['services']['panel'] = ui
    assert not violations(config)
    config['services']['panel'] = {**ui, 'read_only': False}
    assert violations(config)


@pytest.mark.parametrize('name,source,target,read_only', [
    ('panel', '/ws/deploy/dev/panel/Caddyfile', '/etc/caddy/Caddyfile', False),
    ('panel', '/ws', '/etc/caddy/Caddyfile', True),
    ('panel', '/etc/passwd', '/etc/caddy/Caddyfile', True),
    ('app', '/ws/deploy/dev/panel/Caddyfile', '/etc/caddy/Caddyfile', True),
])
def test_panel_bind_exception_is_exact(name, source, target, read_only):
    config = copy.deepcopy(SAFE)
    config['services'][name] = {'networks': {'lab': {}}, 'volumes': [
        {'type': 'bind', 'source': source, 'target': target, 'read_only': read_only}]}
    assert violations(config, '/ws')
