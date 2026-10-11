# Контракты v1

Источник wire schema — `awg_contracts.models`, JSON использует snake_case.
Импорт: `PYTHONPATH=/workspace/packages/contracts`, `from awg_contracts import ...`.
Зависимость: Pydantic v2. Запускать export/tests только в tooling container.

`python -m awg_contracts.export` создаёт самостоятельные JSON Schema 2020-12
и OpenAPI 3.1 в `v1/`. Generated schemas должны проверяться на drift в CI.
Изменения схем проходят architect review до изменений consumers.

Agent management endpoints:

| HTTP | Path | Request | Response |
|---|---|---|---|
| GET | `/v1/capabilities` | — | AgentCapabilities |
| POST | `/v1/deployments/{deployment_id}/validate` | DesiredDeployment | ValidationResult |
| PUT | `/v1/deployments/{deployment_id}/apply` | ApplyRequest | ActualDeployment |
| GET | `/v1/deployments/{deployment_id}/state` | — | ActualDeployment |
| GET | `/v1/deployments/{deployment_id}/traffic` | — | TrafficSnapshot |
| POST | `/v1/internal/subscriptions/material` | SubscriptionRequest | SubscriptionMaterial |

Все Agent endpoints требуют mutual authentication. Internal subscription endpoint
требует отдельный service credential и недоступен из public ingress.
Path deployment ID должен совпасть с payload ID. Node ID должен совпасть с
локальной identity Agent. `expected_applied_revision` — optimistic concurrency
guard; None означает отсутствие applied state. Idempotent replay идентичного
revision+digest проверяется прежде concurrency guard. Reuse revision с другим
digest отклоняется. Digest — SHA256 canonical JSON полной DesiredDeployment,
включая значения defaults: `content_digest(desired)`.

`revoked_public_keys` — durable deny overlay: rollback не может вернуть доступ
отозванным или отсутствующим в последнем авторитетном desired state peers.
Controller передаёт полный peer set, никогда delta. Пустой set авторитетен только
после successful API snapshot или explicit validated admin operation.

Traffic counters — десятичные строки (uint64 и выше без потери JS precision).
`runtime_epoch` меняется при process restart runtime; `sequence` монотонен в epoch.
`PeerTraffic.counter_epoch` меняется только для peer при recreation/reset/decrease.
Production Agent заполняет его всегда; None допускается для legacy fixtures.
Controller дедуплицирует `(node_id, deployment_id, runtime_epoch, sequence)`.
`key_generation` отделяет counter history после rotation. Peer accounting identity
включает runtime_epoch, counter_epoch и key_generation. Смена epoch одного peer
не меняет baseline других peers и не удваивает их lifetime counters.

`SecretStr` исключает случайную печать client key и subscription token. Только
authenticated material response serializer раскрывает client key намеренно;
renderer вызывает `.get_secret_value()`. Model dump для logs не раскрывает ключ.
Private server key отсутствует во всех схемах и запрещён extra-field validation.

ProtocolSpec не подтверждает поддержку произвольной версии. Он позволяет
version adapters; Agent обязательно проверяет allowlist полей и значений pinned
runtime. Capabilities с runtime=mock запрещены для production readiness.

Minor releases сохраняют payload v1; новые обязательные поля/иная семантика
требуют v2. Неизвестные поля отвергаются: rollout additive fields выполняется
через negotiated protocol version, а не отправкой их старому Agent.

## TrustTunnel v1 (additive)

Источник — `awg_contracts.trusttunnel`, все публичные имена доступны также из
`awg_contracts`. Namespace `trusttunnel.remnawave-extension.io/v1`, management
`protocol_version='1.0'`. AWG schemas, defaults и `content_digest` не меняются.
TrustTunnel не имеет WireGuard keys, addresses, IPAM или protocol parameter map.
Существующий `ProtocolCapability` объявляет `adapter_id='trusttunnel'`,
`versions=['1.0']`, features `http2`, `http3`, `acme-http01`, `acme-tls-alpn01`
только после проверки установленного pinned runtime. Сам контракт не доказывает
установку runtime, connectivity или поддержку конкретного клиента.

| HTTP | Path | Request | Response |
|---|---|---|---|
| POST | `/v1/trusttunnel/deployments/{deployment_id}/validate` | TrustTunnelDesired | TrustTunnelValidationResult |
| PUT | `/v1/trusttunnel/deployments/{deployment_id}/apply` | TrustTunnelApplyRequest | TrustTunnelActual |
| GET | `/v1/trusttunnel/deployments/{deployment_id}/state` | — | TrustTunnelActual |
| POST | `/v1/trusttunnel/deployments/{deployment_id}/export` | TrustTunnelExportRequest | TrustTunnelExportResponse |
| POST | `/v1/internal/subscriptions/trusttunnel/material` | SubscriptionRequest | TrustTunnelMaterial |

Agent endpoints используют existing controller mTLS identity; обязательны path
deployment ID и локальный node ID checks. Internal material использует отдельный
gateway service credential, stock token resolution и не принимает user UUID.
Exports также требуют current authorization и production READY, ответы с
credentials получают `Cache-Control: no-store`; content/body/URL не логируются.

`TrustTunnelTLS(domain,email,environment='production',challenge='auto')`
нормализует DNS domain в lowercase IDNA. Wildcard, IP literal, URL/path,
control characters, malformed DNS labels и single-label domain запрещены.
Email проходит conservative operational contact validation. Custom ACME directory
и CA не передаются этим production contract: Pebble override принадлежит только
explicit isolated runtime lab configuration. `TrustTunnelEndpoint(host,port=443)`
допускает DNS/IP endpoint; certificate domain остаётся отдельным TLS name.
Data port не переопределяет ACME public HTTP80/TLS443.

`TrustTunnelProfile` содержит `api_version,profile_id,name,enabled,endpoint,access,
node_ids,tls`. Draft допускает пустой `node_ids`, длина не больше одного.
`TrustTunnelDesired` содержит `api_version,protocol_version,deployment_id,node_id,
revision,profile,clients`: profile должен выбрать exactly `[node_id]`, disabled
profile допускает только пустой clients. Credential — `peer_id,user_uuid,username,
password:SecretStr,credential_generation=1`; identities уникальны по каждому из
peer_id/user_uuid/username. Username — безопасный opaque ASCII identifier,
password минимум32 символа, без controls. Controller обязан генерировать password
из криптографической случайности >=256 bits: длина в контракте не доказывает entropy.
Revision/generation/port — strict integers; bool/string coercion запрещён.

`TrustTunnelApplyRequest(desired,expected_applied_revision=None)` имеет те же CAS,
idempotent retry и durable accepted watermark требования, что AWG, но отдельный
journal. `TrustTunnelValidationResult(valid,revision,digest,issues=[])` ничего не
получает у ACME. Digest: `trusttunnel_digest(desired)` возвращает SHA256 UTF-8
canonical JSON `wire_payload(desired)` с sorted keys, separators `(',', ':')`,
`ensure_ascii=False`, `allow_nan=False`, всеми defaults и **реальными passwords**.
Обычный `content_digest` использовать для TT запрещено: SecretStr mask скрывает
rotation. Пустой full clients set означает revoke согласно authoritative policy.

`TrustTunnelActual` имеет state/healthy/runtime_epoch/observed_at/error,
desired/validated/applied/last_known_good revisions и applied_digest. Сертификат
передаётся плоскими полями: `certificate_state` (ABSENT/ISSUING/VALID/RENEWING/ERROR/
EXPIRED), `certificate_generation` (0 до issuance), `certificate_environment`,
`not_after`, `fingerprint`, `served_fingerprint`, `next_attempt_at`,
`certificate_error_code` (safe identifier). Fingerprints — lowercase SHA256 hex.
READY требует healthy matching desired/applied revision, applied_digest,
production certificate generation>=1, expiry>observed_at и matching non-null
fingerprints. VALID/RENEWING/ERROR допускаются с valid served cert: renewal warning
не отзывает ещё рабочий сертификат. Controller дополнительно проверяет freshness,
node enabled и runtime health. Staging никогда не production READY. Filtered
rollback — DEGRADED/applied_revision=None и material не публикуется. Renewal меняет
certificate_generation, а не desired revision.

`TrustTunnelMaterial(user_uuid,entries=[],generated_at)` выдаёт только authorized
entries. `TrustTunnelSubscriptionEntry` содержит profile_id/node_id/peer_id/name,
endpoint/tls_server_name, username/password/credential_generation и optional
`native_deeplink`/`native_toml`. Native values — SecretStr, поскольку credentials
закодированы в export. `TrustTunnelExportRequest(peer_id,format)` использует только
`deeplink|toml`; `TrustTunnelExportResponse(peer_id,format,content:SecretStr)`
содержит secret-free metadata и закрытый content. Agent использует pinned upstream
exporter; схема не определяет самодельный native encoding.

`repr`, обычные `model_dump`/`model_dump_json` не раскрывают SecretStr.
`wire_payload(model)->dict` — **единственный explicit transport helper** для TT,
раскрывает password и credential-bearing native/content только после проверки
authentication/authorization. Его результат запрещено логировать или сохранять
plaintext в Controller DB/history; sensitive snapshots должны быть encrypted.
Helper намеренно отвергает AWG модели. Контрактные ValidationError скрывают inputs
в `str(error)`, но HTTP error handler всё равно удаляет `input`/`ctx` из
`ValidationError.errors()`; sanitized errors никогда не включают subprocess output
с raw runtime config или credentials. Private TLS/ACME keys отсутствуют в schemas
и отвергаются extra-field validation.
