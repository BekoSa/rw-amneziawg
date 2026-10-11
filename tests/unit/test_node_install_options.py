"""Invalid ingress configuration must fail before changing an installation or invoking Docker."""
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('flag,value', [('--trusttunnel-port', '0'), ('--trusttunnel-port', '65536'),
    ('--trusttunnel-port', '443\nTT_ENABLED=0'), ('--acme-http-port', '8080'), ('--acme-tls-port', '8443'),
    *[('--trusttunnel-port', str(port)) for port in (80, 8443, 9443, 10080, 10443)]])
def test_invalid_trusttunnel_ports_fail_before_installation(tmp_path, flag, value):
    destination = tmp_path / 'installation'
    result = subprocess.run(['sh', str(Path(__file__).resolve().parents[2] / 'install-node.sh'),
        '--dir', str(destination), '--trusttunnel', flag, value], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'Invalid TrustTunnel' in result.stderr
    assert not destination.exists()

@pytest.mark.parametrize('ports', ['443', '440-445'])
def test_trusttunnel_udp_must_not_overlap_awg(tmp_path, ports):
    destination = tmp_path / 'installation'
    result = subprocess.run(['sh', str(Path(__file__).resolve().parents[2] / 'install-node.sh'),
        '--dir', str(destination), '--trusttunnel', '--udp-ports', ports],
        capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'TrustTunnel UDP port conflicts with AmneziaWG' in result.stderr
    assert not destination.exists()
