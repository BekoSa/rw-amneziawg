"""Extension-owned resource intent and negotiated ingress publication gate."""
from datetime import datetime, timedelta, timezone
import ipaddress
from uuid import UUID, uuid5
from psycopg.types.json import Jsonb
from awg_contracts import AWGProfile, TrustTunnelProfile, NodeRegistration
from awg_contracts.managed_node import (NodeCapabilities, NodeDesiredConfig,
    NodeActualState, IngressBinding, node_digest, envelope_digest, validate_node_envelope)
from .agent_client import AgentError

NODE_LOCK_ID = 472319815


class NodeResourceError(ValueError):
    def __init__(self, reason):
        super().__init__('Node resource operation rejected')
        self.reason = reason


async def one(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchone()


async def rows(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchall()


def binding_id(deployment, role, transport, address):
    return uuid5(UUID(str(deployment)), f'node-binding:{role}:{transport}:{address}')


def allocate_awg_backend(used):
    for port in range(20000, 30000):
        if port not in used:
            return port
    raise ValueError('BACKEND_PORT_EXHAUSTED')


class NodeResourceController:
    def __init__(self, db, agents, settings):
        self.db, self.agents, self.settings = db, agents, settings

    async def negotiate(self, conn, registration):
        method = getattr(self.agents, 'node_capabilities', None)
        capabilities = await method(registration) if callable(method) else None
        if capabilities is not None:
            capabilities = NodeCapabilities.model_validate(capabilities)
            if capabilities.node_id != registration.node_id:
                raise AgentError('Node identity mismatch', 'AGENT_IDENTITY_MISMATCH')
            if not capabilities.supports_dynamic_ingress or '1.0' not in capabilities.ingress_protocol_versions:
                raise AgentError('Unsupported node API', 'UNSUPPORTED_NODE_API')
            previous = await one(conn, 'SELECT capabilities FROM node_resources WHERE node_id=%s', (registration.node_id,))
            if previous and previous['capabilities']:
                installed = NodeCapabilities.model_validate(previous['capabilities'])
                if envelope_digest(installed.envelope) != envelope_digest(capabilities.envelope):
                    raise AgentError('Installed envelope changed', 'ENVELOPE_MISMATCH')
        await conn.execute('''INSERT INTO node_resources(node_id,capabilities) VALUES(%s,%s)
            ON CONFLICT(node_id) DO UPDATE SET capabilities=EXCLUDED.capabilities''',
            (registration.node_id, Jsonb(capabilities.model_dump(mode='json')) if capabilities else None))
        return capabilities

    async def _registration(self, conn, node_id):
        row = await one(conn, 'SELECT registration FROM awg_nodes WHERE id=%s', (node_id,))
        if row is None:
            raise ValueError('Unknown node')
        return NodeRegistration.model_validate(row['registration'])

    async def _build(self, conn, capabilities, *, protocols=None, override=None, actual=None):
        node_id = capabilities.node_id
        record = await one(conn, 'SELECT * FROM node_resources WHERE node_id=%s', (node_id,))
        installed = {('trusttunnel' if p.adapter_id == 'trusttunnel' else 'amneziawg')
                     for p in capabilities.installed_protocols
                     if p.adapter_id == 'trusttunnel' or p.adapter_id.startswith(('awg', 'amneziawg'))}
        enabled = sorted(protocols if protocols is not None else
                         record['enabled_protocols'] if record and record['enabled_protocols'] is not None else installed)
        if not set(enabled).issubset(installed):
            raise AgentError('Protocol is not installed', 'UNSUPPORTED_PROTOCOL')
        bindings = []
        auto_deployments = set()
        addresses = sorted(capabilities.envelope.bind_addresses)
        registration = await self._registration(conn, node_id)

        def append(deployment, role, transport, public_port, backend_port):
            for address in addresses:
                current_ip = ipaddress.ip_address(address)
                for existing in bindings:
                    existing_ip = ipaddress.ip_address(existing.public_address)
                    if (existing.transport == transport and existing.public_port == public_port and
                            existing_ip.version == current_ip.version and
                            (existing_ip == current_ip or existing_ip.is_unspecified or current_ip.is_unspecified)):
                        raise NodeResourceError('PORT_IN_USE')
                bindings.append(IngressBinding(binding_id=binding_id(deployment, role, transport, address),
                    deployment_id=deployment, public_address=address, public_port=public_port,
                    transport=transport, backend_role=role, backend_port=backend_port))

        awg = await rows(conn, '''SELECT d.id,f.active FROM awg_deployments d JOIN awg_profiles f ON f.id=d.profile_id
            WHERE d.node_id=%s AND f.active IS NOT NULL ORDER BY d.id''', (node_id,))
        tt = await rows(conn, '''SELECT d.id,f.active FROM tt_deployments d JOIN tt_profiles f ON f.id=d.profile_id
            WHERE d.node_id=%s AND f.active IS NOT NULL ORDER BY d.id''', (node_id,))
        # Profile validation can preview one draft without making it active.
        if override is not None:
            target = tt if isinstance(override.profile, TrustTunnelProfile) else awg
            target[:] = [item for item in target if item['id'] != override.deployment_id]
            target.append({'id':override.deployment_id, 'active':override.profile.model_dump(mode='json')})
        for item in sorted(awg, key=lambda item:str(item['id'])):
            profile = AWGProfile.model_validate(item['active'])
            if not (registration.enabled and profile.enabled and node_id in profile.node_ids and 'amneziawg' in enabled):
                continue
            allocation = await one(conn, 'SELECT port FROM node_awg_backends WHERE deployment_id=%s', (item['id'],))
            if allocation is None:
                used = {row['port'] for row in await rows(conn, 'SELECT port FROM node_awg_backends WHERE node_id=%s', (node_id,))}
                restored = {entry.binding.backend_port for entry in actual.bindings
                    if entry.binding.deployment_id == item['id'] and entry.binding.backend_role == 'awg'} if actual else set()
                if len(restored) > 1 or restored & used:
                    raise ValueError('BACKEND_ALLOCATION_CONFLICT')
                allocation = {'port':next(iter(restored)) if restored else allocate_awg_backend(used)}
                await conn.execute('INSERT INTO node_awg_backends(node_id,deployment_id,port) VALUES(%s,%s,%s)',
                    (node_id, item['id'], allocation['port']))
            append(item['id'], 'awg', 'udp', profile.endpoint.port, allocation['port'])
        for item in sorted(tt, key=lambda item:str(item['id'])):
            profile = TrustTunnelProfile.model_validate(item['active'])
            if not (registration.enabled and profile.enabled and node_id in profile.node_ids and 'trusttunnel' in enabled):
                continue
            append(item['id'], 'trusttunnel', 'tcp', profile.endpoint.port, 8444)
            append(item['id'], 'trusttunnel', 'udp', profile.endpoint.port, 9443)
            if profile.tls.challenge == 'auto':
                auto_deployments.add(item['id'])
            if profile.tls.challenge in {'http-01', 'auto'}:
                append(item['id'], 'acme-http', 'tcp', 80, 8080)
            if profile.tls.challenge in {'tls-alpn-01', 'auto'} and profile.endpoint.port != 443:
                append(item['id'], 'acme-tls-alpn', 'tcp', 443, 8444)
        revision = record['next_revision'] if record else 1
        if actual and actual.accepted_revision is not None and not (record and record['desired']):
            revision = max(revision, actual.accepted_revision+1)
        desired = NodeDesiredConfig(node_id=node_id, revision=revision,
            envelope_digest=envelope_digest(capabilities.envelope), enabled_protocols=enabled,
            bindings=sorted(bindings, key=lambda binding:str(binding.binding_id)))
        try:
            validate_node_envelope(desired, capabilities.envelope)
        except ValueError as failure:
            allowed = {'ENVELOPE_MISMATCH','RESERVED_CONTROL_PORT','UNAPPROVED_BIND_ADDRESS','BINDING_LIMIT_EXCEEDED'}
            raise NodeResourceError(str(failure) if str(failure) in allowed else 'INVALID_RESOURCE_PLAN') from None
        # A busy stock 443 must not prevent auto from selecting available HTTP80.
        # Only typed optional ACME roles are removed; mandatory TT data is retained.
        if auto_deployments:
            full_result = await self.agents.node_validate(registration, desired)
            if not full_result.valid:
                for omitted in ('acme-tls-alpn', 'acme-http'):
                    alternative = desired.model_copy(update={'bindings':[binding for binding in desired.bindings
                        if not (binding.deployment_id in auto_deployments and binding.backend_role == omitted)]})
                    if alternative.bindings == desired.bindings:
                        continue
                    result = await self.agents.node_validate(registration, alternative)
                    if result.valid and result.digest == node_digest(alternative) and result.revision == alternative.revision:
                        desired = alternative
                        break
        if record and record['desired']:
            previous = NodeDesiredConfig.model_validate(record['desired'])
            if node_digest(previous) == node_digest(desired.model_copy(update={'revision':previous.revision})):
                desired = previous
        return desired

    async def _persist(self, conn, desired):
        digest = node_digest(desired)
        existing = await one(conn, 'SELECT digest FROM node_resource_revisions WHERE node_id=%s AND revision=%s',
                             (desired.node_id, desired.revision))
        if existing and existing['digest'] != digest:
            raise ValueError('REVISION_DIGEST_CONFLICT')
        if not existing:
            await conn.execute('INSERT INTO node_resource_revisions(node_id,revision,digest,desired) VALUES(%s,%s,%s,%s)',
                (desired.node_id, desired.revision, digest, Jsonb(desired.model_dump(mode='json'))))
        await conn.execute('''UPDATE node_resources SET desired=%s,next_revision=GREATEST(next_revision,%s),
            enabled_protocols=%s WHERE node_id=%s''', (Jsonb(desired.model_dump(mode='json')),
            desired.revision+1, Jsonb(desired.enabled_protocols), desired.node_id))

    async def _observe(self, conn, registration):
        actual = await self.agents.node_state(registration)
        if actual.node_id != registration.node_id:
            raise AgentError('Node identity mismatch', 'AGENT_IDENTITY_MISMATCH')
        await conn.execute('''UPDATE node_resources SET actual=%s,last_seen_at=now(),last_error_code=NULL WHERE node_id=%s''',
            (Jsonb(actual.model_dump(mode='json')), registration.node_id))
        return actual

    async def ensure(self, conn, registration):
        """Commit sockets before protocol Apply; APPLYING ACME bootstrap is expected."""
        # Delivery connections are autocommit. Persist the immutable revision in
        # a separate transaction before any remote runtime mutation.
        await conn.execute('SELECT pg_advisory_lock(%s)', (NODE_LOCK_ID,))
        try:
            async with conn.transaction():
                capabilities = await self.negotiate(conn, registration)
                if capabilities is None:
                    return None
                actual = await self._observe(conn, registration)
                desired = await self._build(conn, capabilities, actual=actual)
                await self._persist(conn, desired)
            if actual.applied_revision != desired.revision or actual.applied_digest != node_digest(desired):
                result = await self.agents.node_validate(registration, desired)
                if not result.valid or result.digest != node_digest(desired) or result.revision != desired.revision:
                    reason = result.issues[0].code if result.issues else 'NODE_VALIDATION_FAILED'
                    raise AgentError('Node resource plan rejected', reason)
                actual = await self.agents.node_apply(registration, desired, actual.applied_revision)
                await conn.execute('UPDATE node_resources SET actual=%s,last_seen_at=now() WHERE node_id=%s',
                    (Jsonb(actual.model_dump(mode='json')), registration.node_id))
                # Reservation must be accepted. Backend readiness is checked only
                # after the independent protocol apply, avoiding ACME deadlock.
                if actual.accepted_revision != desired.revision or actual.state.value in {'ERROR','DEGRADED'}:
                    raise AgentError('Node resources unavailable', actual.error.code if actual.error else 'INGRESS_UNAVAILABLE')
            return desired
        except Exception as failure:
            await conn.execute('UPDATE node_resources SET last_error_code=%s WHERE node_id=%s',
                (getattr(failure, 'reason', 'NODE_RECONCILE_FAILED'), registration.node_id))
            raise
        finally:
            await conn.execute('SELECT pg_advisory_unlock(%s)', (NODE_LOCK_ID,))

    async def refresh(self, conn, registration):
        record = await one(conn, 'SELECT capabilities FROM node_resources WHERE node_id=%s', (registration.node_id,))
        if record and record['capabilities']:
            await self._observe(conn, registration)

    async def protocol_enabled(self, conn, node_id, protocol):
        record = await one(conn, 'SELECT capabilities,enabled_protocols FROM node_resources WHERE node_id=%s', (node_id,))
        return (not record or not record['capabilities'] or record['enabled_protocols'] is None or
                protocol in record['enabled_protocols'])

    async def existing_path(self, conn, desired, protocol):
        """Allow lifecycle delivery on an already committed destination during
        ingress faults; never authorize a new endpoint or an envelope mismatch."""
        if not desired.profile.enabled:
            return True
        record = await one(conn, 'SELECT desired,actual,last_seen_at FROM node_resources WHERE node_id=%s', (desired.node_id,))
        if not record or not record['desired'] or not record['actual'] or not record['last_seen_at']:
            return False
        actual = NodeActualState.model_validate(record['actual'])
        planned = NodeDesiredConfig.model_validate(record['desired'])
        now = datetime.now(timezone.utc)
        if (actual.applied_revision is None or actual.applied_digest is None or
                now-record['last_seen_at'] > timedelta(seconds=self.settings.ready_max_age) or
                now-actual.observed_at > timedelta(seconds=self.settings.ready_max_age) or
                actual.observed_at > now+timedelta(seconds=5)):
            return False
        committed = await one(conn, 'SELECT digest,desired FROM node_resource_revisions WHERE node_id=%s AND revision=%s',
            (desired.node_id,actual.applied_revision))
        if committed is None or committed['digest'] != actual.applied_digest:
            return False
        canonical = NodeDesiredConfig.model_validate(committed['desired'])
        if node_digest(canonical) != actual.applied_digest or canonical.envelope_digest != planned.envelope_digest:
            return False
        role = 'awg' if protocol == 'amneziawg' else 'trusttunnel'
        required = [binding for binding in planned.bindings if binding.deployment_id == desired.deployment_id
                    and binding.backend_role == role and binding.public_port == desired.profile.endpoint.port]
        transports = {binding.transport for binding in required}
        return ({'udp'} if role == 'awg' else {'tcp','udp'}).issubset(transports) and all(
            binding in canonical.bindings and any(observed.binding == binding and observed.state == 'ACTIVE'
                and observed.error is None for observed in actual.bindings) for binding in required)

    async def publishable(self, conn, node_id, deployment_id, public_port, protocol):
        record = await one(conn, 'SELECT * FROM node_resources WHERE node_id=%s', (node_id,))
        if record is None or record['capabilities'] is None:
            return True  # explicit legacy negotiation; no changed strict DTO
        if not record['desired'] or not record['actual'] or not record['last_seen_at']:
            return False
        desired = NodeDesiredConfig.model_validate(record['desired'])
        actual = NodeActualState.model_validate(record['actual'])
        now = datetime.now(timezone.utc)
        if (actual.applied_revision is None or actual.applied_digest is None or
                now-record['last_seen_at'] > timedelta(seconds=self.settings.ready_max_age) or
                now-actual.observed_at > timedelta(seconds=self.settings.ready_max_age) or
                actual.observed_at > now+timedelta(seconds=5)):
            return False
        committed = await one(conn, 'SELECT digest,desired FROM node_resource_revisions WHERE node_id=%s AND revision=%s',
            (node_id,actual.applied_revision))
        if committed is None or committed['digest'] != actual.applied_digest:
            return False
        canonical = NodeDesiredConfig.model_validate(committed['desired'])
        if node_digest(canonical) != actual.applied_digest or canonical.envelope_digest != desired.envelope_digest:
            return False
        role = 'awg' if protocol == 'amneziawg' else 'trusttunnel'
        required = [binding for binding in desired.bindings if binding.deployment_id == deployment_id and
                    binding.backend_role == role and binding.public_port == public_port]
        # AWG remains publishable when another protocol is issuing its certificate.
        return bool(required) and all(binding in canonical.bindings and
            any(observed.binding == binding and observed.state == 'ACTIVE' and
                observed.backend_ready and observed.error is None for observed in actual.bindings) for binding in required)

    async def state(self, node_id, *, capabilities_only=False, protocols=None):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (NODE_LOCK_ID,))
            registration = await self._registration(conn, node_id)
            capabilities = await self.negotiate(conn, registration)
            if capabilities_only:
                return {'managed':capabilities is not None, 'capabilities':capabilities}
            record = await one(conn, 'SELECT * FROM node_resources WHERE node_id=%s', (node_id,))
            actual = await self._observe(conn, registration) if capabilities else None
            desired = await self._build(conn, capabilities, actual=actual, protocols=protocols) if capabilities else None
            return {'managed':capabilities is not None, 'capabilities':capabilities,
                'desired':desired, 'actual':actual, 'last_error_code':record['last_error_code']}

    async def validate_profile(self, conn, registration, desired):
        capabilities = await self.negotiate(conn, registration)
        if capabilities is None:
            return None
        await conn.execute('SELECT pg_advisory_xact_lock(%s)', (NODE_LOCK_ID,))
        actual = await self._observe(conn, registration)
        plan = await self._build(conn, capabilities, override=desired, actual=actual)
        result = await self.agents.node_validate(registration, plan)
        if result.digest != node_digest(plan) or result.revision != plan.revision:
            raise AgentError('Validation digest mismatch', 'AGENT_BAD_RESPONSE')
        return result

    async def validate(self, node_id, desired):
        async with self.db.connect() as conn:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (NODE_LOCK_ID,))
            registration = await self._registration(conn, node_id)
            capabilities = await self.negotiate(conn, registration)
            if capabilities is None:
                raise AgentError('Migration required', 'UNSUPPORTED_NODE_API')
            authoritative = await self._build(conn, capabilities, protocols=desired.enabled_protocols)
            if authoritative != desired:
                raise ValueError('RESOURCE_PLAN_MISMATCH')
            return await self.agents.node_validate(registration, desired)

    async def apply(self, node_id, request):
        from .db import LOCK_ID
        from .trusttunnel import TT_LOCK_ID
        async with self.db.connect(autocommit=True) as conn:
            acquired = []
            try:
                for lock in (LOCK_ID, TT_LOCK_ID, NODE_LOCK_ID):
                    await conn.execute('SELECT pg_advisory_lock(%s)', (lock,))
                    acquired.append(lock)
                async with conn.transaction():
                    registration = await self._registration(conn, node_id)
                    capabilities = await self.negotiate(conn, registration)
                    if capabilities is None:
                        raise AgentError('Migration required', 'UNSUPPORTED_NODE_API')
                    current = await self._observe(conn, registration)
                    desired = await self._build(conn, capabilities, protocols=request.desired.enabled_protocols, actual=current)
                    if desired != request.desired:
                        raise ValueError('RESOURCE_PLAN_MISMATCH')
                    if request.expected_applied_revision != current.applied_revision:
                        raise ValueError('STALE_NODE_REVISION')
                    await self._persist(conn, desired)
                # Durable intent survives a timeout after the remote commit.
                actual = await self.agents.node_apply(registration, desired, request.expected_applied_revision)
                await conn.execute('UPDATE node_resources SET actual=%s,last_seen_at=now(),last_error_code=NULL WHERE node_id=%s',
                    (Jsonb(actual.model_dump(mode='json')), node_id))
                return {'managed':True, 'capabilities':capabilities, 'desired':desired,
                        'actual':actual, 'last_error_code':None}
            except AgentError as failure:
                await conn.execute('UPDATE node_resources SET last_error_code=%s WHERE node_id=%s', (failure.reason,node_id))
                raise
            finally:
                for lock in reversed(acquired):
                    await conn.execute('SELECT pg_advisory_unlock(%s)', (lock,))
