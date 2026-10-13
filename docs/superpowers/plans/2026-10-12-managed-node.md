# Managed Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** После одноразовой миграции управлять AWG/TrustTunnel и любым свободным портом ноды из существующего TUI без SSH для изменения runtime configuration.

**Architecture:** Отдельный production host-network ingress передаёт TCP/UDP только на static private IP Agent. Сам Agent остаётся в bridge и координирует journaled resource revisions с локальным authenticated ingress API; Controller хранит desired отдельно от stock Remnawave.

**Tech Stack:** Python/Pydantic/FastAPI, Go stdlib relay, PostgreSQL расширения, SQLite Agent journal, Docker Compose, существующие AWG/TrustTunnel/HAProxy/lego runtimes и TUI.

**Spec:** `docs/superpowers/specs/2026-10-12-managed-node-design.md`.

**Execution status 2026-10-13:** Tasks 1–5 implemented and independently reviewed;
unit/contract/golden/DB/TUI aggregate 507 passed. Managed bridge TT/ACME network
scenario passed; managed AWG eight-phase harness is ready for the prepared CI
runner. Official Throne 1.3.2 GUI import/TLS/TCP/UDP passed both URI/Base64 scenarios.
Production host-network runtime was not executed on the development host.
Final release/pipeline gate remains pending; see `docs/managed-node-verification.md`.

## Global Constraints

- Текущий checkout, сохранение чужих изменений; без автоматического commit/push до итоговой координации root.
- Stock Remnawave/Node/Xray/DB не патчить; только documented API.
- Agent только bridge; host-network ingress только production, cap_drop ALL плюс NET_BIND_SERVICE.
- Ни Docker API/socket, ни privileged, ни host routes/firewall/sysctl mutations.
- Development/runtime dependencies только Docker; dev/tests host networking запрещён.
- Пользователь подтвердил произвольный свободный порт и node-only management; DNS-01/provider firewall вне scope.
- Старые AWG/TT v1 DTO и API не расширяются implicit extra fields; managed node — отдельный negotiated namespace.

## Review Focus

- Подмена backend IP/role через control plane: static envelope и management denial проверяются Task 1–3.
- UDP ответы двух разных клиентов: per-source backend socket, timeout и limits проверяются Task 2/6.
- Crash между bind и commit: staged plan никогда не объявляется READY; recovery проверяется Task 2/3/6.
- Port migration одновременно с revoke: старые credentials не возвращаются rollback, Task 3/4/6.
- Занятый rw-core 443/недоступный 80: конфликт не меняет stock listener и не обещает LE fallback, Task 5/6.

## Locked interfaces и dataflow

`packages/contracts/awg_contracts/managed_node.py` — единственный shared DTO source.
`NODE_API_VERSION = 'node.remnawave-extension.io/v1'`; wire protocol `'1.0'`.

| DTO | Поля |
|---|---|
| `NodeInstallEnvelope` | envelope_id:UUID, node_id:UUID, backend_ipv4:str, bind_addresses:list[str] default ['0.0.0.0','::'], reserved_ports:list[Port] default [8443], max_bindings:int=128, max_udp_flows:int=4096, max_tcp_connections:int=4096 |
| `IngressBinding` | binding_id:UUID, deployment_id:UUID, transport:'tcp'\|'udp', public_address:str='0.0.0.0', public_port:Port, backend_role:'awg'\|'trusttunnel'\|'acme-http'\|'acme-tls-alpn', backend_port:Port |
| `NodeDesiredConfig` | api_version literal, protocol_version='1.0', node_id:UUID, revision:Revision, envelope_digest:Digest, enabled_protocols:list['amneziawg'\|'trusttunnel'], bindings:list[IngressBinding] |
| `NodeApplyRequest` | desired:NodeDesiredConfig, expected_applied_revision:Revision\|None=None |
| `NodeCapabilities` | api_version/protocol_version, node_id, envelope:NodeInstallEnvelope, installed_protocols:list[ProtocolCapability], supports_dynamic_ingress:bool=True, ingress_protocol_versions:list[str]=['1.0'] |
| `NodeBindingActual` | binding:IngressBinding, state:'PREPARED'\|'ACTIVE'\|'ERROR', backend_ready:bool=False, error:ValidationIssue\|None=None |
| `NodeActualState` | api_version/protocol_version, node_id, desired_revision/accepted_revision/applied_revision/last_known_good_revision:Revision\|None, applied_digest:Digest\|None, state:DeploymentState, healthy:bool=False, bindings:list[NodeBindingActual], runtime_epoch:UUID\|None, observed_at:AwareDatetime, error:ValidationIssue\|None |
| `NodeValidationResult` | valid:bool, revision:Revision, digest:Digest, issues:list[ValidationIssue] |
| `IngressPrepareRequest` | desired:NodeDesiredConfig, expected_applied_revision:Revision\|None=None; structurally equivalent to NodeApplyRequest |
| `IngressPrepared` | ticket_id:UUID, revision:Revision, digest:Digest |
| `IngressCommitRequest` | ticket_id:UUID, revision:Revision, digest:Digest |

Functions: `node_digest(desired: NodeDesiredConfig) -> str`,
`envelope_digest(envelope: NodeInstallEnvelope) -> str`,
`validate_node_envelope(desired: NodeDesiredConfig, envelope: NodeInstallEnvelope) -> None`
(raises sanitized ValueError for mismatch/forbidden bindings).

Agent public management paths (existing mTLS): GET `/v1/node/capabilities`, POST
`/v1/node/validate`, PUT `/v1/node/apply`, GET `/v1/node/state`.
`NodeResourceService.capabilities() -> NodeCapabilities`,
`.validate(desired) -> NodeValidationResult`, `.apply(request) -> NodeActualState`,
`.state() -> NodeActualState`. Constructor specifics belong to Agent owner.

`IngressClient.prepare(request: IngressPrepareRequest) -> IngressPrepared`,
`.commit(request: IngressCommitRequest) -> NodeActualState`,
`.abort(request: IngressCommitRequest) -> None`, `.state() -> NodeActualState`.
Local paths: POST `/v1/ingress/prepare`, POST `/v1/ingress/commit`, POST
`/v1/ingress/abort`, GET `/v1/ingress/state`; transport is UDS, never published TCP.
Auth: challenge/session handshake, per-session monotonically increasing nonce,
HMAC-SHA256 over session/method/path/nonce/canonical body SHA256; separate named-volume
credential. Retries use new nonce but same desired revision/ticket. Installer emits
same envelope and secret files read by both consumers; Controller never sends secret.

Binding destinations come only from envelope backend IPv4, never a field in Controller
desired. Fixed roles: AWG UDP to allocated port20000..29999; TT TCP to HAProxy8444
and UDP to TT9443; HTTP ACME public TCP80 to Agent8080; ALPN ACME public TCP443 to
Agent8444. ACME roles cannot point directly at solver10080/10443. Agent management8443
is forbidden as backend, independently of external management port. Installer fills
envelope reserved_ports with actual external management port: public8443 is allowed
when management is on2525. Profile endpoints retain public ports; managed AWG adapter
uses internal allocated listen port without changing desired digest or publication.
Managed TT uses fixed internal frontend8444/HTTP8080, independent of public endpoint.
If TT data TCP443 already forwards to8444, Controller uses a single shared listener
binding rather than duplicating ALPN binding. One TT deployment per node remains
current constraint; AWG/TT same UDP allocation is rejected.

Controller allocates/reserves resource revision before protocol Apply. Agent prepares
backend paths and ingress; committed bindings without healthy protocol stay APPLYING.
Resource state is refreshed after protocol Apply/recovery; node READY requires ACTIVE
bindings with backend_ready. Protocol subscription material still requires existing
protocol READY plus matching confirmed node binding, never just desired node revision.

## Task 1: Contract boundaries — architect

**Files:** Create `packages/contracts/awg_contracts/managed_node.py`,
`tests/contracts/test_managed_node.py`; modify contract `__init__.py`, `export.py` and
generated `packages/contracts/v1/*.schema.json`/`openapi.json` only.

**Interfaces:** Produces all Locked interfaces; consumes existing Port, Revision,
ProtocolCapability, DeploymentState, ValidationIssue.

- [x] Write failing tests for strict unknown fields, invalid role/transport, duplicate
  bindings, disabled protocol binding, stable digest, envelope mismatch, management
  target and wildcard overlap, READY coherence, legacy schema unchanged.
- [x] Run Docker tooling contract subset; confirm missing module failure.
- [x] Implement DTOs/helpers and exported schemas/OpenAPI paths without modifying old models.
- [x] Run subset plus existing AWG/TT contract suites; regenerate schemas in Docker.
- [x] Hand off exact interfaces/results to root; no private worker commit.

Evidence Task 1: RED44 missing-contract failures; GREEN44 managed-node tests;
complete contract suite85 passed in Docker (`--network none`, schemas regenerated
as workspace UID1000). Legacy schema content preserved; no independent commit.

## Task 2: Minimal ingress — node runtime worker, separate file ownership

**Files:** Create `apps/node-ingress/{Dockerfile,go.mod,main.go,control.go,relay.go,journal.go}`
and component-local Go tests. No Agent/Controller files in this task.

**Interfaces:** Consumes envelope and ingress DTO wire shapes; produces local UDS API
and relay executable. Standard library only, no management proxy/Docker client.

- [ ] Write failing socket tests: two UDP clients receive only their own replies;
  limits and idle expiry release sockets; TCP half-close/backpressure; occupied bind
  remains owned by original listener; backend target override rejected.
- [ ] Run Go tests in Docker build/test stage; observe expected failures.
- [ ] Implement authenticated UDS control, static destination allowlist, bounded
  per-flow relay, socket reservation and durable prepare/commit/abort journal.
- [ ] Add crash/replay/mismatched ticket/restart tests; run isolated Go suite.
- [ ] Report pinned local API authentication details and image build to root.

## Task 3: Agent resource coordinator — awg_node

**Files:** Create `apps/node-agent/awg_agent/{managed_node.py,ingress_client.py}`;
modify Agent `api.py`, `__main__.py`, TT runtime/certificate integration as needed;
own Agent unit tests in `tests/unit/agent/`.

**Interfaces:** Produces NodeResourceService and IngressClient Locked signatures;
consumes Task 1 DTO and Task 2 API. Keep existing AgentService protocol journals.

- [ ] Write failing tests: unsupported legacy capability, stale revision, envelope
  mismatch, unavailable ingress, same-revision digest conflict, backend failure.
- [ ] Run Agent subset in Docker; confirm failures.
- [ ] Always initialize installed supported runtimes in managed mode, independent of
  old TT_ENABLED; retain legacy startup path if envelope absent. Add negotiated node API.
- [ ] Implement serialized node journal/prepare/commit/recovery and dynamic role ports;
  protocol READY refreshes node backend health. Protect revoke during rollback.
- [ ] Run tests including crash after accepted/prepare/commit and rotated TT sessions;
  report results and backward-compatible startup evidence.

## Task 4: Controller resource reconciliation — remnawave_integrator

**Files:** Controller managed-node module, migration for own node resource tables,
Agent API adapter, existing AWG/TT Apply integration; Controller-only tests.

**Interfaces:** Consumes negotiated node API; produces TUI resource diagnostics and
node state attached through new Controller endpoints, not extra legacy wire fields.
Endpoints under `/api/v1/nodes/{node_id}/resources`: GET state/capabilities, POST
validate, PUT apply. Body uses Locked desired/apply DTOs; Controller auth unchanged.

- [ ] Write failing tests for modern/legacy negotiation, unknown version, concurrent
  AWG/TT UDP conflict, desired endpoint withheld on failed node Apply, revoke race.
- [ ] Run Controller subset in Docker PostgreSQL tooling environment.
- [ ] Persist immutable resource revisions in extension DB; allocate full bindings
  deterministically with locks; prepare node resources before existing protocol Apply
  and refresh actual after Apply. Keep legacy profiles functional without new capability.
- [ ] Add resource constraints to subscription publication using current verified
  protocol material logic; do not add identity or read stock DB.
- [ ] Run Controller/contract material tests and report migration/recovery evidence.

## Task 5: TUI, installer and operational packaging — root/ui ownership split

**Files:** ui owns `apps/tui` and its unit tests. Root owns installer, production
ingress Docker manifests/build/CI packaging, shared root docs; no mutual file edits.

**Interfaces:** UI consumes Controller resource endpoints. Installer supplies identical
envelope/static Agent IP and UDS credential to Agent/ingress; current identity retained.

- [ ] UI adds failing tests for visible resource revisions/bindings/ACME availability,
  arbitrary port edit and specific PORT_IN_USE/legacy migration messages.
- [ ] Root adds shell tests for legacy AWG migration, existing TT migration, subnet
  conflict, interrupted migration rollback and original PKI/state preservation.
- [ ] Implement UI and managed installation mode; production ingress host networking
  is confined to dedicated manifest, Agent remains bridge. Recreate service uses stable IP.
- [ ] Document one-time interruption, image pull+recreate and no provider/firewall API;
  run shellcheck/installer tests and scoped UI tests in Docker.
- [ ] Update image publishing jobs for ingress only after build/tests pass.

## Task 6: Real network and removal verification — network_qa

**Files:** Dedicated managed-node bridge lab Compose/scripts, integration/network
tests, evidence docs; do not change production manifest owned by root.

**Interfaces:** Consumes built Agent/ingress images and strict contracts; lab ingress
uses bridge namespace, same control/runtime implementation as production.

- [ ] Write failing real scenarios: AWG payload TCP/UDP; TT HTTP2/QUIC/native payload;
  port change, conflict, normal TLS plus ALPN solver, UDP client isolation, direct
  bypass absent, backend-management denial, revoke and crash recovery.
- [ ] Run structural safety lint and read-only host baseline before Docker networks.
- [ ] Execute isolated tests; no host firewall/routes/TUN modifications. Missing TUN
  means AWG networking runs on prepared CI runner, never fabricated success locally.
- [ ] Execute stock before/removed/restored HTTP and supported full AWG removal tests;
  cleanup compare host baseline and preserve evidence for all failures/retries.
- [ ] Dedicated disposable production runner verifies host-network bind without
  NET_ADMIN/host firewall mutations; report separately if unavailable.

## Task 7: Final independent review and release — reviewer/root

- [ ] Reviewer checks negotiated boundary, destination allowlist, UDP isolation,
  durable revisions, migration rollback, revocation, Compose privilege scope and
  truthful distinction between local bind and public reachability.
- [ ] Root closes material findings, runs unit/contract/golden/integration/network
  suites appropriate to final code and validates stock-without-extension behavior.
- [ ] Root records passed checks and explicit unverified production-runner limitations.
- [ ] Root commits/pushes only after coherent implementation verification, then checks
  pipeline results; workers do not commit independently.
