from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from awg_contracts import TrustTunnelApplyRequest, TrustTunnelDesired


def desired(revision=1):
    node = '00000000-0000-0000-0000-000000000002'
    return TrustTunnelDesired.model_validate({
        'deployment_id': '00000000-0000-0000-0000-000000000001', 'node_id': node, 'revision': revision,
        'profile': {'profile_id': '00000000-0000-0000-0000-000000000003', 'name': 'tt',
                    'node_ids': [node], 'endpoint': {'host': 'vpn.example.com', 'port': 443},
                    'tls': {'domain': 'vpn.example.com', 'email': 'ops@example.com'}},
        'clients': [{'peer_id': '00000000-0000-0000-0000-000000000004',
                     'user_uuid': '00000000-0000-0000-0000-000000000005',
                     'username': 'opaque', 'password': 'secret' * 8}]})


class Runtime:
    def __init__(self):
        self.current = None
        self.calls = 0
        self.epoch = uuid4()
        self.fail = False
    def validate(self, config): pass
    def ensure_ingress(self): pass
    def apply(self, config, bundle):
        self.calls += 1
        self.current = config
        if self.fail:
            self.fail = False
            raise RuntimeError('secret failure')
    def healthy(self, config, bundle): return self.current == config
    def served_fingerprint(self, config, bundle): return bundle['fingerprint'] if self.current else None
    def stop(self): self.current = None
    def close(self): self.stop()
    def export(self, config, peer, format): return 'native secret'


class Certificates:
    def __init__(self):
        self.calls = 0
        self.bundle = {'certificate_state': 'VALID', 'certificate_generation': 1,
                       'certificate_environment': 'production', 'fingerprint': 'a' * 64,
                       'not_after': datetime.now(timezone.utc) + timedelta(days=30)}
    def current(self, tls): return self.bundle
    def ensure(self, tls): self.calls += 1; return self.bundle
    def metadata(self, tls): return self.bundle or {'certificate_state': 'ABSENT'}
    def validate(self, tls): pass


def service(tmp_path):
    from awg_agent.trusttunnel import TrustTunnelService
    return TrustTunnelService(tmp_path, desired().node_id, Runtime(), Certificates())


def test_retry_cas_watermark_and_restart(tmp_path):
    agent = service(tmp_path)
    first = desired()
    assert agent.apply(TrustTunnelApplyRequest(desired=first)).state == 'READY'
    assert agent.apply(TrustTunnelApplyRequest(desired=first)).state == 'READY'
    assert agent.runtime.calls == 1
    changed = first.model_copy(deep=True)
    changed.clients[0].password = 'rotated' * 8
    with pytest.raises(ValueError, match='digest conflict'):
        agent.apply(TrustTunnelApplyRequest(desired=changed))
    with pytest.raises(ValueError, match='compare-and-swap'):
        agent.apply(TrustTunnelApplyRequest(desired=desired(2)))
    agent.close()
    recovered = service(tmp_path)
    recovered.recover()
    assert recovered.state(first.deployment_id).state == 'READY'
    assert recovered.runtime.current.clients[0].password == first.clients[0].password
    recovered.close()


def test_renewed_certificate_load_failure_keeps_old_bundle_and_revocation(tmp_path, monkeypatch):
    from awg_agent.certificates import CertificateManager
    from awg_agent.trusttunnel import TrustTunnelService
    from tests.unit.agent.test_certificates import pem
    certificates = CertificateManager(tmp_path / 'certificates')
    issued = [pem(), pem()]
    def issue(argv):
        root = certificates.account_path(desired().profile.tls) / 'certificates'
        cert, key = issued.pop(0)
        (root / 'vpn.example.com.crt').write_bytes(cert)
        (root / 'vpn.example.com.key').write_bytes(key)
    monkeypatch.setattr(certificates, '_run', issue)
    old = certificates.ensure(desired().profile.tls)
    runtime = Runtime()
    agent = TrustTunnelService(tmp_path / 'agent', desired().node_id, runtime, certificates)
    agent.apply(TrustTunnelApplyRequest(desired=desired()))
    record = certificates._load(desired().profile.tls)
    record['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    certificates._save(desired().profile.tls, record)
    new = certificates.ensure(desired().profile.tls)
    assert new['fingerprint'] != old['fingerprint']
    original_apply = runtime.apply
    def apply(config, bundle):
        if bundle['fingerprint'] != old['fingerprint']:
            runtime.stop()
            raise RuntimeError('new certificate rejected')
        return original_apply(config, bundle)
    monkeypatch.setattr(runtime, 'apply', apply)
    revoke = desired(2)
    revoke.clients = []
    actual = agent.apply(TrustTunnelApplyRequest(desired=revoke, expected_applied_revision=1))
    assert actual.state == 'DEGRADED' and actual.healthy
    assert actual.served_fingerprint == old['fingerprint'] == actual.fingerprint
    assert runtime.current.clients == []
    assert actual.certificate_error_code == 'CERTIFICATE_LOAD_FAILED'
    agent.close()


def test_failed_revoke_and_rotation_never_resurrect_credentials(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(TrustTunnelApplyRequest(desired=first))
    revoke = desired(2)
    revoke.clients = []
    agent.runtime.fail = True
    actual = agent.apply(TrustTunnelApplyRequest(desired=revoke, expected_applied_revision=1))
    assert actual.state == 'DEGRADED' and actual.applied_revision is None
    assert agent.runtime.current.clients == []
    with pytest.raises(ValueError, match='stale'):
        agent.apply(TrustTunnelApplyRequest(desired=first))
    agent.close()
    agent = service(tmp_path)
    agent.recover()
    assert agent.runtime.current.clients == []
    agent.close()


def test_crash_after_acceptance_rejects_old_revision(tmp_path, monkeypatch):
    agent = service(tmp_path)
    first = desired()
    agent.apply(TrustTunnelApplyRequest(desired=first))
    def crash(*args): raise KeyboardInterrupt('crash')
    monkeypatch.setattr(agent, '_execute', crash)
    with pytest.raises(KeyboardInterrupt):
        agent.apply(TrustTunnelApplyRequest(desired=desired(2), expected_applied_revision=1))
    agent.close()
    agent = service(tmp_path)
    with pytest.raises(ValueError, match='stale'):
        agent.apply(TrustTunnelApplyRequest(desired=first))
    agent.recover()
    assert agent.state(first.deployment_id).applied_revision == 2
    agent.close()


def test_offline_renewal_expiry_and_staging_exclude_ready(tmp_path):
    agent = service(tmp_path)
    first = desired()
    agent.apply(TrustTunnelApplyRequest(desired=first))
    agent.run_once()
    assert agent.certificates.calls == 1
    agent.certificates.bundle['not_after'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert agent.state(first.deployment_id).state == 'DEGRADED'
    agent.certificates.bundle['not_after'] = datetime.now(timezone.utc) + timedelta(days=1)
    agent.certificates.bundle['certificate_environment'] = 'staging'
    assert agent.state(first.deployment_id).state == 'DEGRADED'
    agent.close()


def test_initial_issuance_does_not_block_apply_and_latest_revoke_wins(tmp_path):
    agent = service(tmp_path)
    agent.certificates.bundle = None
    first = desired()
    assert agent.apply(TrustTunnelApplyRequest(desired=first)).state == 'APPLYING'
    revoke = desired(2)
    revoke.clients = []
    assert agent.apply(TrustTunnelApplyRequest(desired=revoke)).state == 'APPLYING'
    agent.certificates = Certificates()
    agent.run_once()
    assert agent.runtime.current.clients == []
    assert agent.state(first.deployment_id).applied_revision == 2
    agent.close()


def test_validate_is_read_only_and_wrong_node_rejected(tmp_path):
    agent = service(tmp_path)
    before = list(tmp_path.rglob('*'))
    assert agent.validate(desired()).valid
    assert list(tmp_path.rglob('*')) == before
    assert agent.certificates.calls == 0
    wrong = desired().model_copy(update={'node_id': uuid4()})
    assert not agent.validate(wrong).valid
    agent.close()


def test_management_api_tt_export_is_explicit_protected_wire(tmp_path):
    from fastapi.testclient import TestClient
    from awg_agent.api import create_app
    from tests.unit.agent.test_agent import service as awg_service
    awg = awg_service(tmp_path / 'awg')
    tt = service(tmp_path / 'tt')
    first = desired()
    client = TestClient(create_app(awg, trusttunnel=tt))
    from awg_contracts import wire_payload
    response = client.put(f'/v1/trusttunnel/deployments/{first.deployment_id}/apply',
                          json=wire_payload(TrustTunnelApplyRequest(desired=first)))
    assert response.status_code == 200 and response.json()['state'] == 'READY'
    response = client.post(f'/v1/trusttunnel/deployments/{first.deployment_id}/export',
                           json={'peer_id': str(first.clients[0].peer_id), 'format': 'deeplink'})
    assert response.status_code == 200 and response.json()['content'] == 'native secret'
    tt.certificates.bundle['certificate_environment'] = 'staging'
    assert client.get('/health/ready').status_code == 503
    tt.close()
    awg.close()
