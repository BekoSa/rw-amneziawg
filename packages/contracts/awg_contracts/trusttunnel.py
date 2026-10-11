"""Additive TrustTunnel v1 contracts; AWG v1 serialization remains independent.

Only authenticated wire/export callers may use ``wire_payload``. Ordinary
Pydantic serialization intentionally masks all credential-bearing strings.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    StrictBool,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from .models import AccessSpec, DeploymentState, Port, Revision, ValidationIssue, utcnow

TRUSTTUNNEL_API_VERSION = 'trusttunnel.remnawave-extension.io/v1'
TRUSTTUNNEL_PROTOCOL_VERSION = '1.0'
TTAPIVersion = Literal['trusttunnel.remnawave-extension.io/v1']
Digest = Annotated[str, Field(strict=True, pattern=r'^[a-f0-9]{64}$')]
Generation = Annotated[int, Field(strict=True, ge=1)]
CertificateState = Literal['ABSENT', 'ISSUING', 'VALID', 'RENEWING', 'ERROR', 'EXPIRED']
CertificateEnvironment = Literal['production', 'staging']
ExportFormat = Literal['deeplink', 'toml']


def _safe_text(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('control characters are forbidden')
    return value


def _domain(value: str) -> str:
    _safe_text(value)
    try:
        normalized = value.encode('idna').decode('ascii').lower()
    except UnicodeError as exc:
        raise ValueError('invalid DNS domain') from exc
    if len(normalized) > 253 or '.' not in normalized:
        raise ValueError('domain must be a fully qualified DNS name')
    labels = normalized.split('.')
    if any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None for label in labels):
        raise ValueError('invalid DNS domain')
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise ValueError('IP literals are not certificate domains')


class TrustTunnelWireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True, hide_input_in_errors=True)


class TrustTunnelEndpoint(TrustTunnelWireModel):
    host: StrictStr = Field(min_length=1, max_length=253)
    port: Port = 443

    @field_validator('host')
    @classmethod
    def safe_host(cls, value):
        _safe_text(value)
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return _domain(value)


class TrustTunnelTLS(TrustTunnelWireModel):
    domain: StrictStr = Field(min_length=1, max_length=253)
    email: StrictStr = Field(min_length=3, max_length=254)
    environment: CertificateEnvironment = 'production'
    challenge: Literal['http-01', 'tls-alpn-01', 'auto'] = 'auto'

    @field_validator('domain')
    @classmethod
    def normalize_domain(cls, value):
        return _domain(value)

    @field_validator('email')
    @classmethod
    def safe_email(cls, value):
        _safe_text(value)
        # Intentionally conservative operational contact syntax, never an argv flag.
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]*@[A-Za-z0-9.-]+', value) is None:
            raise ValueError('invalid ACME contact email')
        local, domain = value.rsplit('@', 1)
        if len(local) > 64:
            raise ValueError('invalid ACME contact email')
        return local + '@' + _domain(domain)


class TrustTunnelProfile(TrustTunnelWireModel):
    api_version: TTAPIVersion = TRUSTTUNNEL_API_VERSION
    profile_id: UUID
    name: StrictStr = Field(min_length=1, max_length=128)
    enabled: StrictBool = True
    endpoint: TrustTunnelEndpoint
    access: AccessSpec = Field(default_factory=AccessSpec)
    node_ids: list[UUID] = Field(default_factory=list, max_length=1)
    tls: TrustTunnelTLS

    @field_validator('name')
    @classmethod
    def safe_name(cls, value):
        return _safe_text(value)


class TrustTunnelCredential(TrustTunnelWireModel):
    peer_id: UUID
    user_uuid: UUID
    username: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
    password: SecretStr = Field(min_length=32, max_length=1024)
    credential_generation: Generation = 1

    @field_validator('password')
    @classmethod
    def safe_password(cls, value):
        _safe_text(value.get_secret_value())
        return value

    @field_serializer('password', when_used='json')
    def serialize_password(self, value, info: SerializationInfo):
        return value.get_secret_value() if (info.context or {}).get('authenticated_wire') else str(value)


class TrustTunnelDesired(TrustTunnelWireModel):
    api_version: TTAPIVersion = TRUSTTUNNEL_API_VERSION
    protocol_version: Literal['1.0'] = TRUSTTUNNEL_PROTOCOL_VERSION
    deployment_id: UUID
    node_id: UUID
    revision: Revision
    profile: TrustTunnelProfile
    clients: list[TrustTunnelCredential] = Field(default_factory=list)

    @model_validator(mode='after')
    def coherent_clients(self):
        if self.profile.node_ids != [self.node_id]:
            raise ValueError('desired profile must select exactly the target node')
        for attribute in ('peer_id', 'user_uuid', 'username'):
            values = [getattr(client, attribute) for client in self.clients]
            if len(values) != len(set(values)):
                raise ValueError(f'duplicate {attribute}')
        if not self.profile.enabled and self.clients:
            raise ValueError('disabled profile cannot authorize clients')
        return self


class TrustTunnelApplyRequest(TrustTunnelWireModel):
    desired: TrustTunnelDesired
    expected_applied_revision: Revision | None = None


class TrustTunnelValidationResult(TrustTunnelWireModel):
    valid: StrictBool
    revision: Revision
    digest: Digest
    issues: list[ValidationIssue] = Field(default_factory=list)


class TrustTunnelActual(TrustTunnelWireModel):
    api_version: TTAPIVersion = TRUSTTUNNEL_API_VERSION
    protocol_version: Literal['1.0'] = TRUSTTUNNEL_PROTOCOL_VERSION
    deployment_id: UUID
    desired_revision: Revision | None = None
    validated_revision: Revision | None = None
    applied_revision: Revision | None = None
    last_known_good_revision: Revision | None = None
    applied_digest: Digest | None = None
    state: DeploymentState = DeploymentState.DRAFT
    healthy: StrictBool = False
    runtime_epoch: UUID | None = None
    observed_at: AwareDatetime = Field(default_factory=utcnow)
    error: ValidationIssue | None = None
    certificate_state: CertificateState = 'ABSENT'
    certificate_generation: Annotated[int, Field(strict=True, ge=0)] = 0
    certificate_environment: CertificateEnvironment | None = None
    not_after: AwareDatetime | None = None
    fingerprint: Digest | None = None
    served_fingerprint: Digest | None = None
    next_attempt_at: AwareDatetime | None = None
    certificate_error_code: StrictStr | None = Field(default=None, pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')

    @model_validator(mode='after')
    def coherent_ready(self):
        if self.state == DeploymentState.READY:
            if (not self.healthy or self.desired_revision is None or
                    self.applied_revision != self.desired_revision or self.applied_digest is None):
                raise ValueError('READY requires healthy matching applied revision and digest')
            if (self.certificate_environment != 'production' or self.certificate_generation < 1 or
                    self.certificate_state not in ('VALID', 'RENEWING', 'ERROR') or
                    self.not_after is None or self.not_after <= self.observed_at or
                    self.fingerprint is None or self.served_fingerprint != self.fingerprint):
                raise ValueError('READY requires a valid production certificate verified by TLS probe')
        return self


class TrustTunnelSubscriptionEntry(TrustTunnelWireModel):
    profile_id: UUID
    node_id: UUID
    peer_id: UUID
    name: StrictStr = Field(min_length=1, max_length=128)
    endpoint: TrustTunnelEndpoint
    tls_server_name: StrictStr = Field(min_length=1, max_length=253)
    username: StrictStr = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
    password: SecretStr = Field(min_length=32, max_length=1024)
    credential_generation: Generation = 1
    native_deeplink: SecretStr | None = None
    native_toml: SecretStr | None = None

    @field_validator('tls_server_name')
    @classmethod
    def normalize_server_name(cls, value):
        return _domain(value)

    @field_validator('password')
    @classmethod
    def safe_password(cls, value):
        _safe_text(value.get_secret_value())
        return value

    @field_serializer('password', 'native_deeplink', 'native_toml', when_used='json')
    def serialize_secrets(self, value, info: SerializationInfo):
        if value is None:
            return None
        return value.get_secret_value() if (info.context or {}).get('authenticated_wire') else str(value)


class TrustTunnelMaterial(TrustTunnelWireModel):
    api_version: TTAPIVersion = TRUSTTUNNEL_API_VERSION
    user_uuid: UUID
    entries: list[TrustTunnelSubscriptionEntry] = Field(default_factory=list)
    generated_at: AwareDatetime = Field(default_factory=utcnow)


class TrustTunnelExportRequest(TrustTunnelWireModel):
    peer_id: UUID
    format: ExportFormat


class TrustTunnelExportResponse(TrustTunnelWireModel):
    """Export metadata is public-safe; content is revealed only on protected wire."""
    peer_id: UUID
    format: ExportFormat
    content: SecretStr = Field(min_length=1, max_length=1048576)

    @field_serializer('content', when_used='json')
    def serialize_content(self, value, info: SerializationInfo):
        return value.get_secret_value() if (info.context or {}).get('authenticated_wire') else str(value)


def wire_payload(model: BaseModel) -> dict:
    """Reveal secrets for an already authenticated TT transport; never use in logs."""
    if not isinstance(model, TrustTunnelWireModel):
        raise TypeError('wire_payload requires a TrustTunnel contract')
    return model.model_dump(mode='json', context={'authenticated_wire': True})


def trusttunnel_digest(desired: TrustTunnelDesired) -> str:
    """SHA256 of full canonical wire JSON, including real password and defaults."""
    if not isinstance(desired, TrustTunnelDesired):
        raise TypeError('trusttunnel_digest requires TrustTunnelDesired')
    canonical = json.dumps(wire_payload(desired), sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


TRUSTTUNNEL_MODELS = [TrustTunnelTLS, TrustTunnelProfile, TrustTunnelCredential, TrustTunnelDesired,
                     TrustTunnelApplyRequest, TrustTunnelActual, TrustTunnelValidationResult,
                     TrustTunnelMaterial, TrustTunnelSubscriptionEntry, TrustTunnelExportRequest,
                     TrustTunnelExportResponse]

__all__ = [model.__name__ for model in TRUSTTUNNEL_MODELS] + [
    'TrustTunnelEndpoint', 'TRUSTTUNNEL_API_VERSION', 'TRUSTTUNNEL_PROTOCOL_VERSION',
    'TRUSTTUNNEL_MODELS', 'wire_payload', 'trusttunnel_digest',
]
