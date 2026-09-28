from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

API_VERSION = 'awg.remnawave-extension.io/v1'
PROTOCOL_VERSION = '1.0'
Revision = Annotated[int, Field(strict=True, ge=1)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_digest(value: BaseModel | dict) -> str:
    data = value.model_dump(mode='json') if isinstance(value, BaseModel) else value
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class WireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)


class Endpoint(WireModel):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[A-Za-z0-9.:_-]+$')
    port: Port


class NetworkSpec(WireModel):
    ipv4_pool: str
    ipv6_pool: str | None = None
    server_ipv4: str
    server_ipv6: str | None = None
    mtu: int = Field(default=1420, ge=576, le=9000)
    client_allowed_ips: list[str] = Field(default_factory=lambda: ['0.0.0.0/0'])

    @model_validator(mode='after')
    def valid_networks(self):
        pool = ipaddress.ip_network(self.ipv4_pool, strict=True)
        server = ipaddress.ip_address(self.server_ipv4)
        if pool.version != 4 or server.version != 4 or server not in pool or pool.prefixlen > 30 or server in (pool.network_address, pool.broadcast_address):
            raise ValueError('server_ipv4 must belong to ipv4_pool')
        if (self.ipv6_pool is None) != (self.server_ipv6 is None):
            raise ValueError('ipv6_pool and server_ipv6 must be supplied together')
        if self.ipv6_pool is not None:
            pool6 = ipaddress.ip_network(self.ipv6_pool, strict=True)
            server6 = ipaddress.ip_address(self.server_ipv6)
            if pool6.version != 6 or server6.version != 6 or server6 not in pool6:
                raise ValueError('server_ipv6 must belong to ipv6_pool')
        for cidr in self.client_allowed_ips:
            ipaddress.ip_network(cidr, strict=True)
        return self


class ProtocolSpec(WireModel):
    adapter_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')
    version: str = Field(min_length=1, max_length=32, pattern=r'^[A-Za-z0-9._-]+$')
    parameters: dict[str, int | str] = Field(default_factory=dict)
    required_features: list[str] = Field(default_factory=list)

    @field_validator('parameters')
    @classmethod
    def safe_parameters(cls, parameters):
        for key, value in parameters.items():
            if not key.isascii() or not key.isalnum() or len(key) > 32:
                raise ValueError('invalid protocol parameter name')
            if isinstance(value, str) and (len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('protocol parameter contains control characters')
        return parameters


class AccessSpec(WireModel):
    squad_ids: list[UUID] = Field(default_factory=list)
    user_ids: list[UUID] = Field(default_factory=list)


class AWGProfile(WireModel):
    api_version: Literal['awg.remnawave-extension.io/v1'] = API_VERSION
    profile_id: UUID
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    endpoint: Endpoint
    network: NetworkSpec
    protocol: ProtocolSpec
    dns_servers: list[str] = Field(default_factory=list)
    access: AccessSpec = Field(default_factory=AccessSpec)
    node_ids: list[UUID] = Field(default_factory=list)

    @field_validator('dns_servers')
    @classmethod
    def valid_dns(cls, addresses):
        for address in addresses:
            ipaddress.ip_address(address)
        return addresses


class ProtocolCapability(WireModel):
    adapter_id: str
    versions: list[str]
    features: list[str] = Field(default_factory=list)


class AgentCapabilities(WireModel):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    node_id: UUID
    agent_version: str
    runtime: Literal['userspace', 'kernel', 'mock']
    protocols: list[ProtocolCapability]
    max_peers: int = Field(default=10000, ge=1)
    supports_ipv6: bool = False
    supports_atomic_apply: bool = True


class PeerSpec(WireModel):
    peer_id: UUID
    user_uuid: UUID
    public_key: str
    ipv4_address: str
    ipv6_address: str | None = None
    key_generation: int = Field(default=1, ge=1)
    persistent_keepalive: int = Field(default=25, ge=0, le=65535)

    @field_validator('public_key')
    @classmethod
    def valid_key(cls, value):
        try:
            if len(base64.b64decode(value, validate=True)) != 32:
                raise ValueError('public key must decode to 32 bytes')
        except Exception as exc:
            raise ValueError('invalid public key') from exc
        return value

    @model_validator(mode='after')
    def valid_addresses(self):
        if ipaddress.ip_address(self.ipv4_address).version != 4:
            raise ValueError('ipv4_address must be IPv4')
        if self.ipv6_address is not None and ipaddress.ip_address(self.ipv6_address).version != 6:
            raise ValueError('ipv6_address must be IPv6')
        return self


class DesiredDeployment(WireModel):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    deployment_id: UUID
    node_id: UUID
    revision: Revision
    profile: AWGProfile
    peers: list[PeerSpec] = Field(default_factory=list)
    revoked_public_keys: list[str] = Field(default_factory=list)

    @field_validator('revoked_public_keys')
    @classmethod
    def valid_revoked_keys(cls, keys):
        for key in keys:
            PeerSpec.valid_key(key)
        return keys

    @model_validator(mode='after')
    def unique_peers(self):
        for attribute in ('peer_id', 'user_uuid', 'public_key', 'ipv4_address'):
            values = [getattr(peer, attribute) for peer in self.peers]
            if len(values) != len(set(values)):
                raise ValueError(f'duplicate {attribute}')
        ipv6 = [peer.ipv6_address for peer in self.peers if peer.ipv6_address]
        if len(ipv6) != len(set(ipv6)):
            raise ValueError('duplicate ipv6_address')
        if set(self.revoked_public_keys) & {peer.public_key for peer in self.peers}:
            raise ValueError('revoked key cannot be present')
        pool = ipaddress.ip_network(self.profile.network.ipv4_pool)
        for peer in self.peers:
            address = ipaddress.ip_address(peer.ipv4_address)
            if address not in pool or address in (pool.network_address, pool.broadcast_address) or peer.ipv4_address == self.profile.network.server_ipv4:
                raise ValueError('peer address is outside pool or conflicts with server')
            if peer.ipv6_address is not None:
                if self.profile.network.ipv6_pool is None:
                    raise ValueError('IPv6 peer requires profile IPv6 pool')
                address6 = ipaddress.ip_address(peer.ipv6_address)
                pool6 = ipaddress.ip_network(self.profile.network.ipv6_pool)
                if address6 not in pool6 or address6 == ipaddress.ip_address(self.profile.network.server_ipv6):
                    raise ValueError('IPv6 peer is outside pool or conflicts with server')
        return self


class ApplyRequest(WireModel):
    desired: DesiredDeployment
    expected_applied_revision: Revision | None = None


class ValidationIssue(WireModel):
    code: str
    message: str
    field: str | None = None


class ValidationResult(WireModel):
    valid: bool
    revision: Revision
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    issues: list[ValidationIssue] = Field(default_factory=list)


class DeploymentState(str, Enum):
    DRAFT = 'DRAFT'
    VALIDATED = 'VALIDATED'
    APPLYING = 'APPLYING'
    READY = 'READY'
    ROLLING_BACK = 'ROLLING_BACK'
    DEGRADED = 'DEGRADED'
    ERROR = 'ERROR'


class ActualDeployment(WireModel):
    protocol_version: Literal['1.0'] = PROTOCOL_VERSION
    deployment_id: UUID
    desired_revision: Revision | None = None
    validated_revision: Revision | None = None
    applied_revision: Revision | None = None
    last_known_good_revision: Revision | None = None
    applied_digest: str | None = None
    state: DeploymentState = DeploymentState.DRAFT
    healthy: bool = False
    interface_name: str | None = None
    server_public_key: str | None = None
    listen_port: Port | None = None
    observed_at: AwareDatetime = Field(default_factory=utcnow)
    error: ValidationIssue | None = None

    @model_validator(mode='after')
    def coherent_ready(self):
        if self.observed_at.tzinfo is None:
            raise ValueError('observed_at must have timezone')
        if self.state == DeploymentState.READY and (not self.healthy or self.desired_revision is None or self.applied_revision != self.desired_revision):
            raise ValueError('READY requires healthy matching desired/applied revision')
        return self


class PeerTraffic(WireModel):
    peer_id: UUID
    public_key: str
    key_generation: int = Field(ge=1)
    counter_epoch: UUID | None = None
    rx_bytes: str = Field(pattern=r'^(0|[1-9][0-9]*)$')
    tx_bytes: str = Field(pattern=r'^(0|[1-9][0-9]*)$')
    latest_handshake: AwareDatetime | None = None


class TrafficSnapshot(WireModel):
    node_id: UUID
    deployment_id: UUID
    runtime_epoch: UUID
    sequence: int = Field(ge=0)
    observed_at: AwareDatetime = Field(default_factory=utcnow)
    peers: list[PeerTraffic]


class UserState(str, Enum):
    ACTIVE = 'ACTIVE'
    DISABLED = 'DISABLED'
    LIMITED = 'LIMITED'
    EXPIRED = 'EXPIRED'
    DELETED = 'DELETED'


class RemnawaveUser(WireModel):
    user_uuid: UUID
    # Opaque upstream lookup key (3.x numeric id, 2.x UUID); only the adapter interprets it.
    upstream_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9-]{1,64}$')
    username: str
    status: UserState
    expire_at: AwareDatetime | None = None
    last_traffic_reset_at: AwareDatetime | None = None
    squad_ids: list[UUID] = Field(default_factory=list)
    traffic_used_bytes: int = Field(default=0, ge=0)
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    subscription_id: str | None = Field(default=None, repr=False)


class UserSnapshot(WireModel):
    users: list[RemnawaveUser]
    authoritative: bool
    fetched_at: AwareDatetime = Field(default_factory=utcnow)
    snapshot_id: UUID


class NodeRegistration(WireModel):
    node_id: UUID
    name: str = Field(min_length=1, max_length=128)
    management_url: str = Field(pattern=r'^https://[^\s]+$')
    remnawave_node_uuid: UUID | None = None
    enabled: bool = True


class NodeView(WireModel):
    registration: NodeRegistration
    capabilities: AgentCapabilities | None = None
    online: bool = False
    last_seen_at: AwareDatetime | None = None
    deployments: list[ActualDeployment] = Field(default_factory=list)


class SubscriptionPeer(WireModel):
    profile_id: UUID
    node_id: UUID
    name: str
    endpoint: Endpoint
    server_public_key: str
    client_private_key: SecretStr
    ipv4_address: str
    ipv6_address: str | None = None
    dns_servers: list[str] = Field(default_factory=list)
    allowed_ips: list[str] = Field(default_factory=lambda: ['0.0.0.0/0'])
    mtu: int = 1420
    protocol: ProtocolSpec


class SubscriptionMaterial(WireModel):
    user_uuid: UUID
    peers: list[SubscriptionPeer]
    generated_at: AwareDatetime = Field(default_factory=utcnow)


class SubscriptionRequest(WireModel):
    subscription_token: SecretStr = Field(min_length=1, max_length=128)

    @field_validator('subscription_token')
    @classmethod
    def valid_short_uuid(cls, token):
        if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token.get_secret_value()) is None:
            raise ValueError('invalid subscription token')
        return token


class APIError(WireModel):
    code: str
    message: str
    correlation_id: str
    retryable: bool = False


ALL_MODELS = [AWGProfile, AgentCapabilities, DesiredDeployment, ApplyRequest, ValidationResult,
              ActualDeployment, TrafficSnapshot, RemnawaveUser, UserSnapshot, NodeRegistration,
              NodeView, SubscriptionMaterial, SubscriptionRequest, APIError]
