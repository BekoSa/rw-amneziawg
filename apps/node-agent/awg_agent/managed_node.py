"""Durable resource coordination; protocol authorization remains in its own journal."""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5
from awg_contracts import (NodeCapabilities, NodeDesiredConfig, NodeApplyRequest, NodeActualState,
    NodeBindingActual, NodeValidationResult, ProtocolCapability, ValidationIssue, IngressPrepareRequest,
    IngressCommitRequest, IngressBinding, node_digest, envelope_digest, validate_node_envelope)
from .certificates import atomic_write
from .ingress_client import IngressError
from awg_config import ADAPTER_ID, FEATURES, VERSIONS


class NodeResourceService:
    def __init__(self, directory, envelope, ingress, awg=None, trusttunnel=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / 'node-resources.json'
        self.envelope, self.ingress = envelope, ingress
        self.lock = threading.RLock()
        self.record = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.record and self.record.get('envelope_digest') != envelope_digest(envelope):
            raise ValueError('ENVELOPE_MISMATCH')
        self.awg, self.trusttunnel = awg, trusttunnel

    def bind_services(self, awg, trusttunnel=None):
        self.awg, self.trusttunnel = awg, trusttunnel
        awg.lock = self.lock
        awg.resources = self
        if trusttunnel is not None:
            trusttunnel.lock = self.lock
            trusttunnel.certificates.publish_lock = self.lock
            trusttunnel.resources = self
            trusttunnel.certificates.available_methods = self.acme_methods

    def _save(self):
        self.record['envelope_digest'] = envelope_digest(self.envelope)
        atomic_write(self.path, json.dumps(self.record,sort_keys=True))

    def capabilities(self):
        protocols = [ProtocolCapability(adapter_id=ADAPTER_ID,versions=VERSIONS,
                                        features=[*FEATURES,'managed-ingress'])]
        if self.trusttunnel is not None:
            protocols.append(ProtocolCapability(adapter_id='trusttunnel',versions=['1.0'],features=['managed-ingress']))
        return NodeCapabilities(node_id=self.envelope.node_id,envelope=self.envelope,installed_protocols=protocols)

    def validate(self, desired):
        issues=[]
        try:
            validate_node_envelope(desired,self.envelope)
            if 'trusttunnel' in desired.enabled_protocols and self.trusttunnel is None:
                raise ValueError('BACKEND_NOT_READY')
        except ValueError:
            issues.append(ValidationIssue(code='ENVELOPE_MISMATCH',message='Node plan is outside the install envelope or installed capabilities'))
        # Prepare is the authoritative bind probe. Validation never mutates accepted watermark.
        if not issues:
            try:
                result=self.ingress.validate(desired)
                issues.extend(result.issues)
            except Exception as exc:
                code=exc.code if isinstance(exc,IngressError) else 'INGRESS_UNAVAILABLE'
                issues.append(ValidationIssue(code=code,message='Ingress validation failed'))
        return NodeValidationResult(valid=not issues,revision=desired.revision,digest=node_digest(desired),issues=issues)

    def apply(self,request):
        with self.lock:
            desired=request.desired
            validate_node_envelope(desired,self.envelope)
            if 'trusttunnel' in desired.enabled_protocols and self.trusttunnel is None:
                raise ValueError('BACKEND_NOT_READY')
            digest=node_digest(desired)
            accepted=self.record.get('accepted_revision')
            if accepted is not None:
                if desired.revision<accepted: raise ValueError('STALE_REVISION')
                if desired.revision==accepted and digest!=self.record['accepted_digest']: raise ValueError('REVISION_CONFLICT')
            if accepted is None or desired.revision>accepted:
                if request.expected_applied_revision!=self.record.get('committed_revision'):
                    raise ValueError('APPLIED_REVISION_MISMATCH')
            # The sidecar may have committed before the Agent crashed recording its ack.
            # Adopt only an exact observed revision/digest; never infer success from acceptance.
            if accepted == desired.revision:
                try:
                    observed = self.ingress.state()
                    if observed.applied_revision == desired.revision and observed.applied_digest == digest:
                        self.record.update(committed_revision=desired.revision,committed_digest=digest,
                            committed=desired.model_dump(mode='json'),phase='committed',error=None)
                        self.record.pop('ticket',None)
                        self._save()
                        return self._actual(observed)
                except Exception:
                    pass
            self.record.update(accepted_revision=desired.revision,accepted_digest=digest,
                               desired=desired.model_dump(mode='json'),phase='applying')
            self._save()
            ticket=None
            try:
                if self.trusttunnel is not None and 'trusttunnel' in desired.enabled_protocols:
                    self.trusttunnel.runtime.ensure_ingress()
                ticket=self.ingress.prepare(IngressPrepareRequest(desired=desired,
                    expected_applied_revision=self.record.get('committed_revision')))
                self.record['ticket']=ticket.model_dump(mode='json'); self._save()
                observed=self.ingress.commit(IngressCommitRequest(**ticket.model_dump()))
                if observed.applied_revision!=desired.revision or observed.applied_digest!=digest:
                    raise IngressError()
                self.record.update(committed_revision=desired.revision,committed_digest=digest,
                                   committed=desired.model_dump(mode='json'),phase='committed',error=None)
                self.record.pop('ticket',None); self._save()
                return self.state()
            except Exception as exc:
                if ticket is not None:
                    try: self.ingress.abort(IngressCommitRequest(**ticket.model_dump()))
                    except Exception: pass
                code=exc.code if isinstance(exc,IngressError) else 'INGRESS_UNAVAILABLE'
                self.record.update(phase='error',error=code); self._save()
                return self._actual(error=ValidationIssue(code=code,message='Node ingress apply failed'))

    def _actual(self, observed=None,error=None):
        ready=bool(observed and self.record.get('committed_revision') is not None
                   and self.record.get('committed_digest') is not None
                   and self.record.get('accepted_revision')==self.record.get('committed_revision')
                   and self.record.get('committed_digest')==observed.applied_digest
                   and self.record.get('committed_revision')==observed.applied_revision)
        bindings=[]
        if observed:
            for item in observed.bindings:
                b=item.binding
                backend=False
                try:
                    svc=self.awg if b.backend_role=='awg' else self.trusttunnel
                    actual=svc.state(b.deployment_id) if svc is not None else None
                    if b.backend_role.startswith('acme-'):
                        proc=svc.runtime.ingress if svc else None
                        backend=bool(proc and proc.poll() is None)
                    else:
                        effective=(svc._load(b.deployment_id) or {}).get('effective') if svc else None
                        matches=bool(effective and effective['profile']['endpoint']['port']==b.public_port)
                        backend=bool(actual and actual.state=='READY' and actual.healthy and matches)
                except Exception:
                    backend=False
                bindings.append(NodeBindingActual(binding=b,state=item.state,
                    backend_ready=backend,error=item.error))
            ready=ready and all(b.state=='ACTIVE' and b.backend_ready for b in bindings)
        return NodeActualState(node_id=self.envelope.node_id,
            desired_revision=self.record.get('accepted_revision'),accepted_revision=self.record.get('accepted_revision'),
            applied_revision=self.record.get('committed_revision') if observed and observed.applied_digest==self.record.get('committed_digest') else None,
            applied_digest=self.record.get('committed_digest') if observed and observed.applied_digest==self.record.get('committed_digest') else None,
            last_known_good_revision=self.record.get('committed_revision'),
            state='ERROR' if error else ('READY' if ready else 'APPLYING' if self.record else 'DRAFT'),
            healthy=ready and error is None,bindings=bindings,runtime_epoch=observed.runtime_epoch if observed else None,
            observed_at=datetime.now(timezone.utc),error=error)

    def state(self):
        with self.lock:
            try: return self._actual(self.ingress.state())
            except Exception: return self._actual(error=ValidationIssue(code='INGRESS_UNAVAILABLE',message='Node ingress is unavailable'))

    def backend_port(self,deployment_id,role):
        with self.lock:
            plan=self.record.get('committed') or {}
            bindings=[b for b in plan.get('bindings',[]) if b['deployment_id']==str(deployment_id) and b['backend_role']==role]
            if role=='awg': bindings=[b for b in bindings if b['transport']=='udp']
            if not bindings: raise ValueError('BACKEND_NOT_READY')
            return bindings[0]['backend_port']

    def require_protocol(self,desired,role):
        plan=self.record.get('committed') or {}
        port=desired.profile.endpoint.port
        bindings=[b for b in plan.get('bindings',[]) if b['deployment_id']==str(desired.deployment_id)
                  and b['backend_role']==role and b['public_port']==port]
        transports={b['transport'] for b in bindings}
        required={'udp'} if role=='awg' else {'udp','tcp'}
        if desired.profile.enabled and not required.issubset(transports): raise ValueError('BACKEND_NOT_READY')
        if role=='trusttunnel' and desired.profile.enabled:
            roles={b['backend_role'] for b in plan.get('bindings',[]) if b['deployment_id']==str(desired.deployment_id)}
            http='acme-http' in roles
            alpn='acme-tls-alpn' in roles or any(b['transport']=='tcp' and b['public_port']==443 for b in bindings)
            challenge=desired.profile.tls.challenge
            if (challenge=='http-01' and not http) or (challenge=='tls-alpn-01' and not alpn) or (challenge=='auto' and not (http or alpn)):
                raise ValueError('ACME_INGRESS_UNAVAILABLE')

    def acme_methods(self):
        bindings=(self.record.get('committed') or {}).get('bindings',[])
        available=[]
        if any(b['backend_role']=='acme-http' for b in bindings): available.append('http-01')
        if any(b['backend_role']=='acme-tls-alpn' or (b['backend_role']=='trusttunnel' and b['transport']=='tcp' and b['public_port']==443) for b in bindings): available.append('tls-alpn-01')
        return available

    def recover(self):
        if self.record.get('desired'):
            desired=NodeDesiredConfig.model_validate(self.record['desired'])
            return self.apply(NodeApplyRequest(desired=desired,expected_applied_revision=self.record.get('committed_revision')))
        return self.state()

    def bootstrap_resources(self):
        if self.record: return self.recover()
        bindings=[]; protocols=set()
        if self.awg:
            records=list(self.awg.db.execute('SELECT id,payload FROM deployments ORDER BY id'))
            for offset,(identifier,payload) in enumerate(records):
                d=json.loads(payload)['desired']
                if not d['profile'].get('enabled',True): continue
                protocols.add('amneziawg')
                bindings.append(IngressBinding(binding_id=uuid5(self.envelope.envelope_id,identifier+':awg'),
                    deployment_id=UUID(identifier),transport='udp',public_address=self.envelope.bind_addresses[0],public_port=d['profile']['endpoint']['port'],
                    backend_role='awg',backend_port=20000+offset))
        if self.trusttunnel:
            for identifier in self.trusttunnel.identifiers():
                d=self.trusttunnel._load(identifier)['desired']
                if not d['profile'].get('enabled',True): continue
                protocols.add('trusttunnel'); port=d['profile']['endpoint']['port']
                items=[('tcp',port,'trusttunnel',8444),('udp',port,'trusttunnel',9443)]
                challenge=d['profile']['tls']['challenge']
                if challenge in ('http-01','auto') and os.environ.get('TT_HTTP_PORT','80') != '0': items.append(('tcp',80,'acme-http',8080))
                if challenge in ('tls-alpn-01','auto') and port!=443 and os.environ.get('TT_TLS_ALPN_PORT','443') != '0': items.append(('tcp',443,'acme-tls-alpn',8444))
                for transport,public,role,backend in items:
                    bindings.append(IngressBinding(binding_id=uuid5(self.envelope.envelope_id,f'{identifier}:{role}:{transport}'),
                        deployment_id=identifier,transport=transport,public_address=self.envelope.bind_addresses[0],public_port=public,backend_role=role,backend_port=backend))
        if not bindings: return self.state()
        desired=NodeDesiredConfig(node_id=self.envelope.node_id,revision=1,envelope_digest=envelope_digest(self.envelope),
                                 enabled_protocols=sorted(protocols),bindings=bindings)
        return self.apply(NodeApplyRequest(desired=desired))

    def close(self):
        if hasattr(self.ingress,'close'): self.ingress.close()


class ManagedAWGRuntime:
    """Translate only runtime listen port, keeping public journal and digest immutable."""
    def __init__(self,runtime,resources): self.runtime,self.resources=runtime,resources
    def _internal(self,desired):
        internal=desired.model_copy(deep=True)
        internal.profile.endpoint.port=self.resources.backend_port(desired.deployment_id,'awg')
        return internal
    def apply(self,desired,private_key):
        if not desired.profile.enabled: return self.runtime.stop(desired.deployment_id)
        return self.runtime.apply(self._internal(desired),private_key)
    def healthy(self,desired,public_key):
        if not desired.profile.enabled: return True
        return self.runtime.healthy(self._internal(desired),public_key)
    def counters(self,desired):
        # Counters identify the existing interface by deployment UUID, never by
        # endpoint port. Final traffic must remain readable after ingress revoke.
        return self.runtime.counters(desired)
    def stop(self,identifier): return self.runtime.stop(identifier)
