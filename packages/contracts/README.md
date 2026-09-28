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
