"""Negotiated node resources; no implicit changes to legacy AWG/TT v1 payloads.

Controller desired may select listeners but never a forwarding destination.
The immutable install envelope supplies the only permitted backend address.
"""
from __future__ import annotations

import ipaddress
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictStr,
    field_validator, model_validator,
)

from .models import (
    DeploymentState, Port, ProtocolCapability, Revision, ValidationIssue,
    content_digest, utcnow,
)

NODE_API_VERSION = 'node.remnawave-extension.io/v1'
NODE_PROTOCOL_VERSION = '1.0'
NodeAPIVersion = Literal['node.remnawave-extension.io/v1']
Digest = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
BackendRole = Literal['awg', 'trusttunnel', 'acme-http', 'acme-tls-alpn']
NodeProtocol = Literal['amneziawg', 'trusttunnel']


def _bind_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if (address.is_loopback or address.is_link_local or address.is_multicast or
            '%' in value or getattr(address, 'ipv4_mapped', None) is not None):
        raise ValueError('public bind address must be an unscoped unicast or wildcard IP')
    return str(address)


class NodeWireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True, hide_input_in_errors=True)


class NodeInstallEnvelope(NodeWireModel):
    envelope_id: UUID
    node_id: UUID
    backend_ipv4: StrictStr
    bind_addresses: list[StrictStr] = Field(default_factory=lambda: ['0.0.0.0', '::'], min_length=1, max_length=32)
    reserved_ports: list[Port] = Field(default_factory=lambda: [8443], max_length=128)
    max_bindings: Annotated[int, Field(strict=True, ge=1, le=1024)] = 128
    max_udp_flows: Annotated[int, Field(strict=True, ge=1, le=1000000)] = 4096
    max_tcp_connections: Annotated[int, Field(strict=True, ge=1, le=1000000)] = 4096

    @field_validator('backend_ipv4')
    @classmethod
    def private_bridge_backend(cls, value):
        address = ipaddress.ip_address(value)
        networks = [ipaddress.ip_network(prefix) for prefix in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
        if address.version != 4 or not any(address in network for network in networks):
            raise ValueError('backend must be an RFC1918 bridge IPv4 address')
        return str(address)

    @field_validator('bind_addresses')
    @classmethod
    def permitted_addresses(cls, values):
        addresses = [_bind_address(value) for value in values]
        if len(addresses) != len(set(addresses)):
            raise ValueError('duplicate install bind address')
        return addresses

    @field_validator('reserved_ports')
    @classmethod
    def distinct_reserved_ports(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate reserved port')
        return values


class IngressBinding(NodeWireModel):
    binding_id: UUID
    deployment_id: UUID
    transport: Literal['tcp', 'udp']
    public_address: StrictStr = '0.0.0.0'
    public_port: Port
    backend_role: BackendRole
    backend_port: Port

    @field_validator('public_address')
    @classmethod
    def safe_public_address(cls, value):
        return _bind_address(value)

    @model_validator(mode='after')
    def permitted_role_destination(self):
        if self.backend_role == 'awg':
            valid = self.transport == 'udp' and 20000 <= self.backend_port <= 29999
        elif self.backend_role == 'trusttunnel':
            valid = self.backend_port == (8444 if self.transport == 'tcp' else 9443)
        elif self.backend_role == 'acme-http':
            valid = self.transport == 'tcp' and self.public_port == 80 and self.backend_port == 8080
        else:
            valid = self.transport == 'tcp' and self.public_port == 443 and self.backend_port == 8444
        if not valid:
            raise ValueError('backend role, transport and ports are incompatible')
        return self


class NodeDesiredConfig(NodeWireModel):
    api_version: NodeAPIVersion = NODE_API_VERSION
    protocol_version: Literal['1.0'] = NODE_PROTOCOL_VERSION
    node_id: UUID
    revision: Revision
    envelope_digest: Digest
    enabled_protocols: list[NodeProtocol] = Field(default_factory=list, max_length=2)
    bindings: list[IngressBinding] = Field(default_factory=list, max_length=1024)

    @model_validator(mode='after')
    def coherent_allocations(self):
        if len(self.enabled_protocols) != len(set(self.enabled_protocols)):
            raise ValueError('duplicate enabled protocol')
        if len(self.bindings) != len({binding.binding_id for binding in self.bindings}):
            raise ValueError('duplicate binding identity')
        awg_destinations = {}
        for index, binding in enumerate(self.bindings):
            protocol = 'amneziawg' if binding.backend_role == 'awg' else 'trusttunnel'
            if protocol not in self.enabled_protocols:
                raise ValueError('binding requires an enabled protocol')
            if binding.backend_role == 'awg':
                owner = awg_destinations.setdefault(binding.backend_port, binding.deployment_id)
                if owner != binding.deployment_id:
                    raise ValueError('AWG backend port is allocated to another deployment')
            for earlier in self.bindings[:index]:
                if (earlier.transport != binding.transport or earlier.public_port != binding.public_port):
                    continue
                left = ipaddress.ip_address(earlier.public_address)
                right = ipaddress.ip_address(binding.public_address)
                if left.version == right.version and (left == right or left.is_unspecified or right.is_unspecified):
                    raise ValueError('overlapping public listener allocation')
        tt_deployments = {binding.deployment_id for binding in self.bindings if binding.backend_role != 'awg'}
        if len(tt_deployments) > 1:
            raise ValueError('one TrustTunnel deployment per node is supported')
        return self


class NodeApplyRequest(NodeWireModel):
    desired: NodeDesiredConfig
    expected_applied_revision: Revision | None = None


class NodeCapabilities(NodeWireModel):
    api_version: NodeAPIVersion = NODE_API_VERSION
    protocol_version: Literal['1.0'] = NODE_PROTOCOL_VERSION
    node_id: UUID
    envelope: NodeInstallEnvelope
    installed_protocols: list[ProtocolCapability] = Field(default_factory=list)
    supports_dynamic_ingress: StrictBool = True
    ingress_protocol_versions: list[StrictStr] = Field(default_factory=lambda: ['1.0'], max_length=16)

    @model_validator(mode='after')
    def envelope_identity(self):
        if self.node_id != self.envelope.node_id:
            raise ValueError('capability identity differs from install envelope')
        return self


class NodeBindingActual(NodeWireModel):
    binding: IngressBinding
    state: Literal['PREPARED', 'ACTIVE', 'ERROR']
    backend_ready: StrictBool = False
    error: ValidationIssue | None = None


class NodeActualState(NodeWireModel):
    api_version: NodeAPIVersion = NODE_API_VERSION
    protocol_version: Literal['1.0'] = NODE_PROTOCOL_VERSION
    node_id: UUID
    desired_revision: Revision | None = None
    accepted_revision: Revision | None = None
    applied_revision: Revision | None = None
    last_known_good_revision: Revision | None = None
    applied_digest: Digest | None = None
    state: DeploymentState = DeploymentState.DRAFT
    healthy: StrictBool = False
    bindings: list[NodeBindingActual] = Field(default_factory=list, max_length=1024)
    runtime_epoch: UUID | None = None
    observed_at: AwareDatetime = Field(default_factory=utcnow)
    error: ValidationIssue | None = None

    @model_validator(mode='after')
    def ready_is_committed_and_healthy(self):
        if self.state == DeploymentState.READY:
            if (not self.healthy or self.desired_revision is None or
                    self.applied_revision != self.desired_revision or
                    self.accepted_revision != self.desired_revision or self.applied_digest is None or
                    self.error is not None or any(binding.state != 'ACTIVE' or not binding.backend_ready or
                                                 binding.error is not None for binding in self.bindings)):
                raise ValueError('READY requires matching committed revisions and healthy active bindings')
        return self


class NodeValidationResult(NodeWireModel):
    valid: StrictBool
    revision: Revision
    digest: Digest
    issues: list[ValidationIssue] = Field(default_factory=list)


class IngressPrepareRequest(NodeApplyRequest):
    """Local ingress prepare shares the node Apply optimistic concurrency contract."""


class IngressPrepared(NodeWireModel):
    ticket_id: UUID
    revision: Revision
    digest: Digest


class IngressCommitRequest(IngressPrepared):
    """Commit and abort refer only to an authenticated reserved ticket."""


def node_digest(desired: NodeDesiredConfig) -> str:
    if not isinstance(desired, NodeDesiredConfig):
        raise TypeError('node_digest requires NodeDesiredConfig')
    return content_digest(desired)


def envelope_digest(envelope: NodeInstallEnvelope) -> str:
    if not isinstance(envelope, NodeInstallEnvelope):
        raise TypeError('envelope_digest requires NodeInstallEnvelope')
    return content_digest(envelope)


def validate_node_envelope(desired: NodeDesiredConfig, envelope: NodeInstallEnvelope) -> None:
    """Validate authority against installed limits; no host inspection or mutation."""
    if desired.node_id != envelope.node_id or desired.envelope_digest != envelope_digest(envelope):
        raise ValueError('ENVELOPE_MISMATCH')
    if len(desired.bindings) > envelope.max_bindings:
        raise ValueError('BINDING_LIMIT_EXCEEDED')
    for binding in desired.bindings:
        if binding.public_port in envelope.reserved_ports:
            raise ValueError('RESERVED_CONTROL_PORT')
        if binding.public_address not in envelope.bind_addresses:
            raise ValueError('UNAPPROVED_BIND_ADDRESS')


MANAGED_NODE_MODELS = [NodeInstallEnvelope, IngressBinding, NodeDesiredConfig,
                       NodeApplyRequest, NodeCapabilities, NodeBindingActual,
                       NodeActualState, NodeValidationResult, IngressPrepareRequest,
                       IngressPrepared, IngressCommitRequest]

__all__ = [model.__name__ for model in MANAGED_NODE_MODELS] + [
    'NODE_API_VERSION', 'NODE_PROTOCOL_VERSION', 'MANAGED_NODE_MODELS',
    'node_digest', 'envelope_digest', 'validate_node_envelope',
]
