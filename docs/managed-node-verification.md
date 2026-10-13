# Managed node — проверка 2026-10-13

Проверки выполняются в Docker; production host-network manifest на development
host не запускался. Stock Remnawave backend/Node/Xray/DB не изменялись.

Локальные результаты:

- Общий unit/contract/golden/DB/TUI suite: 515 passed, 3 skipped, 17 network
  deselected. Ownership/enrollment отдельно с минимальными необходимыми
  capabilities: 18 passed.
- Agent/contract suite: 163 passed, 3 ownership skipped; Controller: 62 passed.
- Installer/provisioning: 16 passed. Проверены rollback, PKI/identity preservation,
  immutable envelope/secret/subnet и отсутствие вытеснения чужого backend address.
  Rollback использует сохранённые immutable image IDs. Повторная миграция сначала
  освобождает Docker mappings собственного Agent для восстановления ingress journal;
  ошибка остановки возвращает прежнюю конфигурацию и образы.
- Go relay race tests: PASS. Проверены HMAC/replay, durable prepare/commit/recovery,
  UDP isolation, TCP half-close/idle и ACME443→TT443 socket retarget.
- Managed bridge lab: 1 passed, 34.91 s. Реальные HTTP-01 и TLS-ALPN-01,
  fragmented ClientHello, H2/QUIC/native TCP/UDP payload, смена2443→35443→443,
  revoke/recovery deny и два независимых UDP clients. Доказательства:
  `artifacts/managed-node-run-20261013T211503-28681`.
- Host VPN/interfaces/routes/policy rules: unchanged PASS. Firewall readback
  unavailable; его неизменность этим readback не сертифицирована.
- Independent review: исправлены ошибки legacy disabled ACME ingress и private
  bind migration; повторная проверка не выявила новых P0/P1/P2.
- Официальный Throne 1.3.2: 2 passed, 14.41 s — GUI импорт URI/Base64,
  TLS verification, реальная передача TCP/UDP. Startup ждёт connected IPC
  bundled core, а не только завершения импорта.
  `artifacts/trusttunnel-run-20261013T212410-46877`.
- Итоговый общий TrustTunnel/Throne lab: 7 passed, 161.38 s; cleanup и
  host VPN/interfaces/routes/rules comparison PASS.
  `artifacts/trusttunnel-run-20261013T212527-49138`.
- CI на `4d321fa` выявил ошибку свежего managed bootstrap: пустые revision/digest
  ошибочно считались подтверждёнными. Исправление сохраняет DRAFT до commit;
  regression и общий suite прошли. Capabilities совпадают установленному adapter.
  Обычный AWG прошёл восемь E2E-фаз и installer в этом CI run.
- TT lab был пересобран с текущим runtime; failure-injection wrapper теперь
  передаёт новые keyword arguments. Повтор на свежем образе: 7 passed, 167.82 s,
  `artifacts/trusttunnel-run-20261013T215024-69622`.
- CI на `a101212` прошёл обычный AWG/installer, TT/Throne и managed TT, но
  выявил ошибку Controller transport: unversioned NodeValidationResult проверялся
  как versioned envelope. Guard теперь применяется только к NodeCapabilities и
  NodeActualState; реальные HTTP mock regressions проверяют validation и отказ
  неизвестных версий. После исправления общий suite: 515 passed.

Реальный AWG через новый UDP relay проверяет отдельный `managed-awg` CI job с
готовым TUN; локальный development host не имеет TUN, он не создавался.
Bridge connectivity не доказывает внешний DNS/provider firewall reachability.
Отдельная runtime-проверка production host-network deployment локально не
проводилась; manifest проверен структурно и независимым reviewer.

CI блокирует публикацию extension/agent/ingress images до успешных unit,
legacy AWG/installer, TrustTunnel/Throne, managed TT и managed AWG jobs.
