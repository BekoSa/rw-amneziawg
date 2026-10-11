# TrustTunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить управляемый TrustTunnel с автоматическим Let's Encrypt HTTP-01/TLS-ALPN-01 и сохранить работающий AWG и stock Remnawave.

**Architecture:** Отдельные TrustTunnel contracts/API/таблицы расширения; существующая mTLS management boundary. Node-local supervisor владеет endpoint, lego, HAProxy и durable ACME lifecycle. Controller владеет entitlement и encrypted user credentials.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, PostgreSQL/SQLite, cryptography, Textual; pinned TrustTunnel endpoint/client, lego, HAProxy; Docker/Pebble.

**Spec:** [Принятый дизайн](../specs/2026-10-09-trusttunnel-design.md).

## Global Constraints

- Все зависимости и tests только Docker; bridge networking, без privileged, host network, Docker socket и host network mutation.
- Stock Remnawave только documented API/webhooks, без прямого SQL даже в lab.
- AWG v1 wire payload и digest сохраняются. TrustTunnel namespace `trusttunnel.remnawave-extension.io/v1`, protocol_version `1.0`.
- Один TT deployment на node; endpoint.port настраивается отдельно от ACME public80/443.
- HTTP-01/TLS-ALPN-01/auto, отдельная management PKI; renewal работает без Controller.
- Endpoint v1.1.0/client v1.1.5; SHA256 release artifacts и точные lego/HAProxy pins обязательны до готовности.
- SecretStr раскрывается только authenticated wire/export; encrypted credentials и revision snapshots в Controller DB, secrets не в logs/argv/URLs.
- Независимый reviewer и реальные connectivity/challenge tests обязательны; full tunnel требует подготовленного runner с существующим TUN.

## Review Focus

- Существующий Xray занимает443: HTTP-01 работает с отдельным data port, TLS-ALPN отвергает конфликт без остановки Xray.
- TT использует единый listen_address TCP/UDP: bind0.0.0.0:9443 и UDP mapping публичного data port, не loopback-only UDP.
- Revoke одновременно с renewal/restart: старые sessions и credentials не возвращаются rollback.
- Crash между certificate publish и reload: recover проверяет предъявленный fingerprint до READY.
- Короткий lifetime сертификата/ARI: renewal не зависит от фиксированных90 дней, restart не создаёт order storm.

## Task 1: Baseline repairs — root + awg_node

**Files:** `scripts/lab.sh`, `uninstall.sh`, `apps/node-agent/awg_agent/enroll.py`; новые `tests/unit/test_uninstall.py`, `tests/unit/agent/test_enrollment.py`, `tests/network/test_stock_boundaries.py`.

**Interfaces:** существующие CLI без изменения аргументов; E2E removal проверяет stock HTTP/API.

- [ ] Regression: absent/empty original TSV сохраняет container и вызывает restart; invalid management_port оставляет прежние PKI bytes; lab orchestration не вызывает stock psql.
- [ ] RED в Docker: `scripts/lab.sh test -q tests/unit/test_uninstall.py tests/unit/agent/test_enrollment.py tests/network/test_stock_boundaries.py`.
- [ ] Исправить TSV parsing, полную prevalidation и безопасную запись enrollment; удалить direct stock SQL.
- [ ] GREEN той же командой; shellcheck в Docker для scripts.

## Task 2: Contracts — architect (до consumers)

**Files:** новый `packages/contracts/awg_contracts/trusttunnel.py`, export/init/README, generated `packages/contracts/v1/TrustTunnel*.schema.json` и OpenAPI; `tests/contracts/test_trusttunnel.py`.

**Interfaces:** `TrustTunnelTLS`, `TrustTunnelProfile`, `TrustTunnelCredential`, `TrustTunnelDesired`, `TrustTunnelApplyRequest`, `TrustTunnelActual`, `TrustTunnelValidationResult`, `TrustTunnelMaterial`, `TrustTunnelSubscriptionEntry`. Имена полей из spec; `wire_payload(model)` раскрывает password для защищённого wire, `trusttunnel_digest(desired)` считает реальный wire JSON. Actual имеет state/revision/applied_digest/certificate metadata; secret-free export response отдельно.

- [ ] RED: domain/injection/duplicate credentials, digest отличается при rotation, repr/обычный model_dump не раскрывают secret, canonical wire round-trip, schema snapshot.
- [ ] Определить strict модели и endpoint request/response schemas; capability adapter_id='trusttunnel', versions=['1.0'].
- [ ] Экспорт schemas только Docker; GREEN: `scripts/lab.sh test -q tests/contracts`.
- [ ] Зафиксировать для consumers точные методы serialization и import names; peer IPAM/AWG schemas не переделывать.

## Task 3: Node runtime и ACME — awg_node

**Files:** новые `apps/node-agent/awg_agent/trusttunnel.py`, `trusttunnel_runtime.py`, `certificates.py`; изменения `api.py`, `__main__.py`, `service.py`, `Dockerfile`; `tests/unit/agent/test_trusttunnel.py`, `test_certificates.py`.

**Interfaces:** `TrustTunnelService(directory,node_id,runtime,certificates)` с `validate(desired)`, `apply(request)`, `state(id)`, `export(id,peer_id,format)`, `recover()`, `close()`; `TrustTunnelRuntime` владеет child process groups/config/HAProxy/exporter и служебной TLS probe; `CertificateManager` запускает lego argv и хранит generation/next_attempt/validated bundle. Общий service lock защищает запись журналов, apply и certificate publish.

- [ ] RED: stale/reused revision, retry/CAS, crash accepted watermark, filtered rollback без revoke resurrection, key rotation завершает старые sessions, offline Controller не прекращает renewal.
- [ ] Реализовать separate journal/runtime с bounded subprocess timeouts и sanitized failures, read-only validate без ACME mutation.
- [ ] Реализовать HAProxy TCP ClientHello ALPN passthrough, HTTP safe path; TT bind0.0.0.0:9443 и loopback solver ports. Installer port policy передаётся immutable env.
- [ ] Реализовать renewal single flight, persistent backoff, ARI/proportional fallback, atomic bundle fsync, served fingerprint/chain probe, expiry material exclusion; не обещать atomic apply при restart.
- [ ] Проверить export pinned binary/native format, reload SIGHUP и restart fallback; worker сообщает точные artifacts/pins root для review.
- [ ] GREEN: `scripts/lab.sh test -q tests/unit/agent tests/security`.

## Task 4: Controller — remnawave_integrator

**Files:** новые `apps/controller/awg_controller/trusttunnel.py`, `migrations/004_trusttunnel.sql`; изменения `service.py`, `app.py`, `agent_client.py`, `domain.py` только integration hooks; новые Controller TT unit/DB/API tests.

**Interfaces:** `TrustTunnelController(db,upstream,agents,settings)` с `save_profile(profile)`, `validate_profile(id)`, `apply_profile(id,digest)`, `reconcile(snapshot)`, `material(token)`, `rotate_credentials(user_uuid)`; authenticated AgentClient TT validate/apply/state/export. Cache/eligibility берётся из существующего Controller authoritative snapshot.

- [ ] RED: stable credentials/revisions, AEAD DB/revision plaintext absence, status/expiry/squad/node revoke, rotation, incomplete snapshot preservation, older agent rejected, arbitrary UUID material denied.
- [ ] Создать additive extension-only migration и AEAD sensitive snapshots с domain-specific context.
- [ ] Реализовать TT desired/delivery/polling; issuance APPLYING не блокирует общий reconcile timeout и AWG delivery.
- [ ] Добавить admin и service API, bounded fresh READY material gate и authoritative subscription identity.
- [ ] GREEN: `scripts/lab.sh test -q tests/unit/controller tests/contracts tests/security` с реальной extension PostgreSQL.

## Task 5: Delivery — subscription и ui, без пересечения файлов

**Files (subscription):** `packages/subscription-renderers/subscription_renderers/trusttunnel.py`, capabilities/init, `apps/subscription-gateway/awg_gateway/app.py`; TT golden fixtures/tests.
**Files (ui):** `apps/tui/awg_tui/api.py`, `app.py`, новая TT form module; `tests/unit/tui/test_trusttunnel.py`.

**Interfaces:** typed material из Task2/Controller APIs Task4. Native `tt://?`/CLI TOML получает pinned exporter Agent через Controller; Mihomo renderer использует verified core >=1.19.32, unknown embedded core TT не получает.

- [ ] RED golden: native import/export, exact Mihomo fields, Unicode/escaping/no-secret logs, stock unchanged on unsupported/outage, no unpublished TT.
- [ ] Реализовать отдельную native route и `/info` action только после successful stock identity/auth/HWID; обновление body headers/cache без stock regressions.
- [ ] RED TUI pilot: отдельная TT форма без IPAM/WG полей, Save/Validate/Apply, expiry/warning/rotation, скрытые passwords.
- [ ] GREEN: `scripts/lab.sh test -q tests/golden tests/unit/subscription tests/unit/tui tests/security`.

## Task 6: Deployment и изолированный lab — root + network_qa

**Files (root):** `install-node.sh`, `deploy/production/node.compose.yaml`, docs/INSTALL/README/operations, dependency/pin manifests/CI.
**Files (QA):** `deploy/dev/compose.trusttunnel.yaml`, lab Dockerfiles, Pebble configs, `scripts/trusttunnel-lab.sh`, `tests/integration/test_trusttunnel.py`, network safety tests.

**Interfaces:** one TT deployment/node; static public ports config, 443 conflict refuse before mutation; no Docker socket. Lab directory/CA override only explicit test config.

- [ ] Pin/hash/verify endpoint/client/lego artifacts and HAProxy digest, build containers; no host installs.
- [ ] Добавить opt-in TT install ports/email/domain/method, preserve existing AWG defaults/data and update/removal safety.
- [ ] Safety lint и host baseline перед network creation; isolated underlay/server-target/management networks, client без прямого target доступа.
- [ ] Pebble реальные HTTP-01/TLS-ALPN-01 issuance+renewal+failure+recovery и ALPN fragmentation/normal traffic. Никаких обращений к production CA в автоматическом lab.
- [ ] Реальная TT TCP/UDP/auth/revoke/rotation/restart connectivity; concurrent renewal сохраняет обычные sessions, revoke завершает их.
- [ ] Проверить stock HTTP работает без extension и restoration; AWG реальный full-tunnel только prepared TUN runner, отсутствующий device не создавать.

## Task 7: Verification и independent review — root/reviewer

- [ ] Полный `scripts/lab.sh test -q -m 'not network'`, contract/golden/security/DB/TUI; сравнить baseline202 passed.
- [ ] Новый TT lab и существующий AWG E2E на безопасном prepared runner; сохранить exact commands/logs/host comparison, limitations явно.
- [ ] Reviewer читает итоговые diff/contracts/secret boundaries/renewal/revocation/upgrades/removal и исправления проходят targeted RED/GREEN.
- [ ] Обновить architecture/testing/operations с реальными результатами, описать backup node ACME/account/cert и encrypted Controller credentials.
- [ ] Проверить source diff сохраняет пользовательские изменения; не коммитить unrelated baseline edits. Итоговый report отделяет реализацию, local evidence и prepared-runner gaps.

## Execution handoff

По AGENTS.md выполняем custom subagents: сначала architect contracts, затем независимые consumers с ownership выше. Docker lab владеет один QA одновременно; workers unit-тесты координируют с ним. Root интегрирует и проверяет, reviewer выполняет независимый итоговый аудит. Предпочтителен текущий checkout с сохранением уже существующих изменений; worktree требует переноса текущего diff, а не старого HEAD.
