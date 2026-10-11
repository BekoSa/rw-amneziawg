# TrustTunnel и node-local ACME: проект решения

Статус: draft для review, 2026-10-09. Продуктовый код не изменён; baseline
проверки ещё идут. MASTER_PROMPT.md пуст; действуют AGENTS.md и architecture.md.
Пользователь запросил второй протокол и автоматическое получение/продление
Let's Encrypt; дополнительно предложил поддержать HTTP-01 и TLS-ALPN-01.

## Границы и выбранный подход

TrustTunnel — отдельный дочерний runtime существующего Agent, управляемый
Controller через прежнюю mTLS identity boundary. Remnawave users/status/expiry/
squads читаются через публичные API; stock runtime и DB не меняются.
Нет Docker socket, host networking, privileged, host packages/systemd/iptables.
Restart TrustTunnel не перезапускает AWG. Все services и tests работают в Docker.

Выбран additive подход: отдельные TT модели/endpoints/таблицы. Общая union-модель
для всех протоколов потребовала бы изменения strict AWG v1 payload и migrations
WG peers/IPAM. Фиктивные WG keys/IP addresses для TT отвергнуты. Один TT profile
выбирает одну node; первый релиз допускает один TT deployment на node. Общая
eligibility и security boundary переиспользуются, протокольное состояние отдельно.

## Upstream и evidence

Предлагаемые pin: endpoint **v1.1.0**, lab client **v1.1.5**, без latest/master.
Stable endpoint release связывает эти версии. Release artifacts обеих архитектур
закрепить SHA256 и проверить подпись по pinned verification key; конкретные
SHA256 обязан добавить implementer до merge. Сейчас это незавершённый пункт.
[Endpoint release](https://github.com/TrustTunnel/TrustTunnel/releases/tag/v1.1.0).

Pinned endpoint использует username/password и TOML credentials/TLS hosts.
Документированный SIGHUP reload TLS всё равно проверяется реальным runtime.
[Configuration](https://github.com/TrustTunnel/TrustTunnel/blob/v1.1.0/CONFIGURATION.md).

Native exports: `tt://?` deep-link и CLI TOML. Получать exporter pinned binary:
`trusttunnel_endpoint vpn.toml hosts.toml -c USER -a ENDPOINT --format deeplink|toml`.
Encoding не изобретать; fixture round-trip pinned client обязателен.
Встроенный subscription endpoint из master/beta не предполагается в v1.1.0.
[README](https://github.com/TrustTunnel/TrustTunnel/blob/v1.1.0/README.md).

ACME binary выбран **lego**, поддерживающий HTTP-01 и TLS-ALPN-01; точный tag и
checksums закрепляет implementer после проверки flags в Docker. TCP ingress
выбран HAProxy в TCP mode с raw ClientHello ALPN inspection; точный image digest
закрепить. Оба pin — обязательные acceptance deliverables, не плавающие latest.
[Lego challenges](https://go-acme.github.io/lego/obtain/index.print.html).
ALPN ACL должен сверяться с pinned HAProxy manual, включая точное имя fetch и
list matching, не копироваться без config/runtime проверки.
[HAProxy manual](https://www.haproxy.com/documentation/haproxy-configuration-manual/).

## Контракты и API

Новый `packages/contracts/awg_contracts/trusttunnel.py`, namespace
`trusttunnel.remnawave-extension.io/v1`, protocol_version `1.0`.
AWG models/serialization/defaults/digests не меняются.

* TrustTunnelProfile: profile_id,name,enabled,endpoint,access,node_ids (одна node),
  tls(domain,email,environment production/staging,challenge http-01/tls-alpn-01/auto).
  Domain DNS-only normalized IDNA, без wildcard, URL/path/control chars.
* TrustTunnelCredential: peer_id,user_uuid,username,password:SecretStr,
  credential_generation. Stable opaque username; random password >=256 bit.
* TrustTunnelDesired: deployment_id,node_id,revision,profile,clients full set;
  unique peer_id/user_uuid/username. ApplyRequest содержит expected_applied_revision.
* TrustTunnelActual: lifecycle/revisions/applied_digest/healthy/runtime_epoch/
  observed_at; certificate state ABSENT/ISSUING/VALID/RENEWING/ERROR/EXPIRED,
  not_after,fingerprint,served_fingerprint,next_attempt_at,sanitized error code.
* TrustTunnelMaterial: user_uuid и authorized entries/native exports без TLS
  private key или ACME account key.

Existing AgentCapabilities.protocols допускает adapter_id=trusttunnel,
versions=['1.0'], features=['http2','http3','acme-http01','acme-tls-alpn01'].
Capabilities публикуются только для установленного pinned binary. Controller
не отправляет TT schemas старому Agent. Default AWG payload прежний.

Agent `/v1/trusttunnel/deployments/{id}/validate|apply|state|export` использует
existing controller mTLS identity, path/node checks. Public management отсутствует.
Controller `/api/v1/trusttunnel/profiles`, validate/apply/state/credential rotation.
Internal `/v1/internal/subscriptions/trusttunnel/material` — отдельный gateway
credential и authoritative stock token resolution, без caller-selected user UUID.

SecretStr требует explicit wire serializers: раскрывать password только для
authenticated desired/material. Digest canonical JSON считается по реальному
wire password, иначе rotation не изменяет digest. Logs/repr/error dumps redacted;
outbound HTTP debug выключен. Не пересылать passwords в argv, URLs или labels.

## Ownership, revisions и rollback

Controller собственные tt_profiles,tt_deployments,tt_credentials. Password AEAD
через KeyVault с отдельным context `trusttunnel:<user_uuid>:<generation>` и key ID.
Desired/revision history тоже не сохраняет раскрытые passwords в JSONB: encrypt
sensitive payload at rest. Допустим FK только к extension user cache.
Нет TT IPAM и WG peer addresses. Rotation отдельная от AWG key rotation.

Reconcile использует общую eligibility/expiry/squad/node enabled политику,
authoritative snapshot и tombstones; неполная pagination не удаляет users.
Credential stable между reconcile; secrets не генерируются повторно при restart.
Validate выполняет compile/schema/resource checks без ACME issuance.
Apply persistent accepted watermark до mutation, revision+digest retry и CAS.
Initial issuance может вернуть APPLYING; Controller polls, не ждёт внутри timeout.

Agent durable SQLite journal с private filesystem permissions. Full set является
последней authorization truth. Rollback LKG credentials пересекаются с новым
desired по peer_id/username/generation/credential fingerprint. Absent, disabled,
rotated user не возвращается. Filtered rollback = DEGRADED, applied_revision=None,
нет subscription публикации. Если безопасный rollback невозможен, stop только TT.
Credential update/revocation controlled restart TT и terminate старых sessions;
SIGHUP нельзя считать credential reload без pinned runtime evidence.

Node volume содержит private runtime credentials, TLS key и ACME account key:
directories 0700/files 0600/umask 077, atomic rename+fsync; snapshots/backups private.
Challenge responder не имеет пути к секретам. Single-flight persistent lock
сериализует apply/recover/certificate publish. Renewal не меняет desired revision;
это local certificate_generation и actual status.

## HTTP-01, TLS-ALPN-01 и ingress

Production public ports: HTTP-01 TCP80, TLS-ALPN-01 TCP443; обычный TT HTTPS TCP
и HTTP3 UDP на настраиваемом endpoint.port (default 443). Изменение internal ports
не меняет фиксированные CA public ports. Если TCP443 уже занимает stock Xray,
HTTP-01 позволяет отдельный TT data port; TLS-ALPN-01 отклоняется до mutation,
если ingress443 не принадлежит расширению. Auto не пытается захватить чужой порт.
Domain A/AAAA должен указывать на ноду. Management mTLS сертификат отдельный от LE.
Default challenge auto: HTTP-01 сначала, fallback TLS-ALPN-01 при challenge/network
failure с bounded retry; explicit modes запускают только выбранный solver.
Не обещать fallback после любого order error: account/rate-limit ошибки требуют
backoff, а не нового order. Сохранять последний успешный method для renewal.

Node-local supervisor запускает HAProxy и lego без shell и Docker daemon.
HAProxy TCP443 frontend читает ClientHello с bounded inspect-delay;
`req.ssl_alpn` содержащий `acme-tls/1` направляется только к loopback lego TLS
solver (например 10443); остальные TLS bytes passthrough в TT backend
127.0.0.1:9443. Сам TT слушает 0.0.0.0:9443: его единый listen_address обслуживает
TCP и UDP. UDP endpoint.port публикуется как endpoint.port:9443/udp; TCP9443 наружу
не публикуется. HAProxy также принимает TCP endpoint.port, если он отличается от443.
Нет TLS termination и HTTP proxy перед TT.
HTTP80 frontend направляет только safe challenge path к lego HTTP solver
(например 10080); прочие paths получают 404. Нельзя проксировать management API.
Не добавлять PROXY header к TT без documented support. Solver high ports
loopback-only и не публикуются Docker. Port mappings проверяются installer.

Без активного challenge lego listeners могут отсутствовать: acme-tls/1 соединение
отклоняется и никогда не уходит в regular TT backend. Normal TLS/no ALPN не
направляется solver. Нельзя продолжить после partial/unparsed ClientHello в
неправильный backend: timeout/oversize получает bounded failure. HAProxy config
проверяется pinned `haproxy -c`; ALPN routing — реальными fragmented ClientHello tests.

Остановка TT для temporary bind443 рассмотрена как fallback topology, но не
выбрана: renewal не должен каждый раз прерывать пользователей. Certbot выбранный
для одного HTTP-01 заменён lego ради обоих методов. DNS-01/wildcard отложены:
требуют provider credentials/plugins и отдельной permission model.

## Renewal, health и failures

Node сам выдаёт initial cert и renew при offline Controller. Lego account/cert
state durable; restart не создаёт новый account/order каждый раз. Check каждые
12 h с 0..1 h jitter; учитывать ACME Renewal Information (ARI) window и renewal
policy pinned lego, proportional fallback remaining <= lifetime/3 при отсутствии
ARI; не предполагать всегда 90 дней. Retry 5 min..6 h exponential jitter,
persist next_attempt. Production CA allowlist LE production/staging; custom
directory/CA допускаются только explicit isolated test config для Pebble.

После issuance PEM/SAN/key-match/validity проверка и versioned atomic bundle.
SIGHUP pinned TT, затем реальный TLS probe с SNI проверяет presented fingerprint,
expiry/chain. Если reload не сработал, controlled restart только TT и повторный
probe. Ошибка renewal сохраняет рабочий valid cert с warning. Expired served cert
исключает READY/material. Staging/Pebble cert никогда production-ready.
Rollback certificate bundle допустим только на ещё valid старый bundle; config
revision rollback не откатывает автоматически успешно renewed cert.
Upstream initial wizard сам по себе не обеспечивает жизненный цикл.
[Renewal guidance](https://github.com/TrustTunnel/TrustTunnel/blob/v1.1.0/CERT_RENEWAL.md).

## TUI, native gateway и compatibility

TUI отдельная TT форма: domain,email,public endpoint,challenge/environment;
Save → Validate → Apply сохранён. Node показывает expiry/renewal warning/actual
health. Existing AWG network fields не навязываются TT. Explicit export user
показывает credential-bearing native link; passwords скрыты в обычных таблицах.

Gateway enrich только после successful stock auth/HWID/rules и fresh READY
desired==applied. /info native TT action и explicit native format route используют
проверенную stock identity. Stock fallback при exporter/controller error.
Mihomo TT можно включить для **подтверждённых core >=1.19.32**: upstream этого
tag содержит TrustTunnel outbound, documented fields username/password/sni/quic/
udp/health-check. Это conservative supported floor, не утверждение о первой
upstream версии с поддержкой. Нужны exact golden и real pinned client proxy E2E;
неизвестный app-embedded core не считается verified. Throne/INCY не заявлять.
[Mihomo v1.19.32 implementation](https://github.com/MetaCubeX/mihomo/blob/v1.19.32/adapter/outbound/trusttunnel.go),
[configuration evidence](https://wiki.metacubex.one/en/config/proxies/trusttunnel/).
Native tt:// fixture должен дополнительно сверить pinned DEEP_LINK.md TLV schema.
Existing AWG
output остаётся прежним; неизвестному клиенту TT не вставлять автоматически.
Встроенная subscription выдача TT endpoint выключена: иначе обход Remnawave
identity/status/expiry. Native body no-store; не публиковать секретные URLs в logs.

## Реализация и acceptance

Architect: TT models/schema/API docs и согласованные contracts; Integrator:
Controller service/migrations; AWG node: отдельные TT runtime/certificate/lifecycle
modules и Dockerfile; Subscription: native fixtures/gateway; UI: TT screen;
Root: pinned artifacts/production Compose/installer; QA: isolated Pebble lab.
Файловые ownership между параллельными implementers не пересекаются.

До feature merge узкие baseline fixes: убрать direct stock SQL из lab.sh,
проверять removal stock через публичный API; TSV uninstall сохраняет empty fields
и имеет regression; enrollment валидирует всё до mutation. Не превращать это
в несвязанный extensive refactor. Baseline full network E2E сейчас блокируется
отсутствием host /dev/net/tun; не создавать device/модули. Prepared runner нужен
для container full-tunnel. TT server proxy smoke не заменяет full-tunnel evidence.

* Unit/contract: redaction, plaintext-sensitive persistence absence, real digest,
  domain/argv/path negatives, CAS/retry/crash/rollback/revocation/rotation.
* DB reconcile: stable encrypted credentials, expiry/status/node disable,
  incomplete snapshots, no public arbitrary-user material access.
* Native golden: exporter exact fixtures + pinned client parse/import,
  Unicode/special chars, stock compatibility и outage passthrough.
* Pebble: реальные HTTP-01 и TLS-ALPN-01 через frontend, initial/restart/renew,
  failed challenges/fallback/backoff, fingerprint served after reload,
  expiry exclusion, non-challenge TLS untouched, fragmented ClientHello,
  simultaneous normal TT sessions during renewal и no acme regular backend leak.
* Network: isolated client → endpoint → separate target network real TCP/UDP;
  wrong/rotated/revoked auth denial и old session termination; AWG работает
  после TT restart. Private target allowance только lab; production deny private.
* Structural safety и read-only host baseline before/after cleanup; stock removal
  API smoke, restart/migrations, independent final reviewer.

Production issuance не утверждать проверенным только по Pebble. Для этого нужны
operator domain/email, external DNS/ports и реальный Let's Encrypt evidence.
Все new dependency pins/checksums, HAProxy ACL compatibility и runner evidence
должны войти в окончательный verification report; это ещё не реализовано.
