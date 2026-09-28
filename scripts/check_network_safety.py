"""Validate resolved `docker compose config --format json`; run in tooling only."""
import json
import posixpath
import sys


def violations(config, workspace=None):
    errors = []
    # Non-internal networks can reach the host's routing (and its VPN); only the dev panel proxy may use one,
    # solely to publish a loopback port.
    egress_allowed = {'ui-publish': {'panel'}}
    for name, network in config.get('networks', {}).items():
        if network.get('external') or network.get('driver_opts') or network.get('driver', 'bridge') != 'bridge':
            errors.append(f'network {name}: only project-owned bridge networks allowed')
        if not network.get('internal') and name not in egress_allowed:
            errors.append(f'network {name}: must be internal')
    for name, volume in config.get('volumes', {}).items():
        if volume.get('driver_opts') or volume.get('external'):
            errors.append(f'volume {name}: external/driver_opts forbidden')
    for name, service in config.get('services', {}).items():
        def reject(reason):
            errors.append(f'{name}: {reason}')
        for network in service.get('networks') or {}:
            if network in egress_allowed and name not in egress_allowed[network]:
                reject(f'not allowed on non-internal network {network}')
        if name in {service for services in egress_allowed.values() for service in services}:
            if service.get('cap_add') or not service.get('read_only') or 'no-new-privileges:true' not in service.get('security_opt', []):
                reject('externally attached service must be read_only, no-new-privileges, without cap_add')
        if service.get('network_mode') not in (None, 'none'):
            reject('network_mode must be omitted or none')
        for key in ('pid', 'ipc', 'uts', 'userns_mode'):
            if service.get(key) == 'host':
                reject(f'{key}: host forbidden')
        if service.get('privileged'):
            reject('privileged forbidden')
        if isinstance(service.get('build'), dict) and service['build'].get('network') == 'host':
            reject('build network host forbidden')
        if service.get('cgroup') == 'host':
            reject('cgroup host forbidden')
        if service.get('cap_add') and (name not in {'awg-agent', 'awg-client'} or set(service['cap_add']) - {'NET_ADMIN', 'NET_RAW'}):
            reject('cap_add only NET_ADMIN/NET_RAW on AWG isolated services')
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict):
                reject('unresolved mount (use compose config JSON)')
                continue
            if mount.get('type') == 'bind':
                source = posixpath.normpath(mount.get('source', ''))
                # Read-only binds only: the workspace into tooling, and the panel proxy's own Caddyfile.
                # All runtime state uses named volumes.
                tooling = name == 'tooling' and source == workspace and mount.get('target') == '/workspace'
                panel = (name == 'panel' and workspace is not None and source == posixpath.join(workspace, 'deploy/dev/panel/Caddyfile')
                         and mount.get('target') == '/etc/caddy/Caddyfile')
                if workspace is None or not mount.get('read_only') or not (tooling or panel):
                    reject(f'unsafe bind mount {source}')
        for device in service.get('devices', []):
            if name not in {'awg-agent', 'awg-client'} or not isinstance(device, dict) or device.get('source') != '/dev/net/tun' or device.get('target') != '/dev/net/tun':
                reject('only AWG /dev/net/tun device permitted')
        for port in service.get('ports', []):
            if not isinstance(port, dict) or port.get('host_ip') != '127.0.0.1':
                reject('published ports must bind IPv4 loopback explicitly')
        for option in service.get('security_opt', []):
            if 'unconfined' in option:
                reject('unconfined security profile forbidden')
        for key in service.get('sysctls', {}):
            if name != 'awg-agent' or key not in {'net.ipv4.ip_forward', 'net.ipv6.conf.all.forwarding'}:
                reject(f'unsupported sysctl {key}')
    return errors


if __name__ == '__main__':
    errors = violations(json.load(sys.stdin), sys.argv[1] if len(sys.argv) > 1 else None)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        sys.exit(1)
    print('Resolved Compose network safety: PASS')
