# Проверка требований

Источник: [MASTER_PROMPT.md](MASTER_PROMPT.md). Это карта приёмки, а не заявление о production readiness.
Успешная генерация конфигурации и unit tests не подтверждают tunnel connectivity.

| Разделы master | Проверяемое свойство | Владелец | Необходимое доказательство |
|---|---|---|---|
| 1–6, 42–43, 76 | Внешний stock Remnawave, versioned contracts | architect, integrator | Contract tests, upgrade matrix |
| 7–11, 72–75 | Reconciliation, lifecycle, squads, tombstones | integrator | Реальный PostgreSQL, authoritative snapshot failure/empty, offline deletion |
| 12–14, 68–69 | Race-safe IPAM, quarantine, encrypted client keys, local server keys | integrator, node | Concurrency tests, restart, secrets redaction, rotation |
| 15–20, 22, 75–76 | Agent, mTLS, capabilities, revisions и rollback | node | Contract + runtime tests, auth rejection, recovery, revoke-safe rollback |
| 21, 23–32 | Ready-only, identity binding, transparent fail-open, renderers | subscription | Golden outputs и proxy tests, реальные client-version fixtures |
| 33–36 | Traffic epochs/deltas, dedupe и quotas | integrator, node | Reset/restart/replay/key rotation accounting tests |
| 37–39 | Standalone UI, explicit validation/apply | ui | UI state/security tests и Controller integration |
| 40–43, 77–79 | Removal, restore, upgrade, persistent volumes | network_qa | Stock доступен без extension; восстановление peers; upgrade tests |
| 44–67 | Docker-only lab и host safety | network_qa | Resolved Compose lint, preflight, before/after host snapshots |
| 61–62, 80–81 | Настоящий AWG tunnel | network_qa, node | Handshake и TCP/UDP/DNS payload, full/split tunnel, lifecycle, reconnect |
| 68–71 | Secrets, logs, metrics и health | все компоненты | Redaction/auth tests, dependency failures, metrics endpoint |
| 82–86 | Contracts-first, ownership, документация и review | root, reviewer | Архитектура, схема API, независимый отчёт |
| 87–90 | Полный Definition of Done | root | Все обязательные доказательства выше, без critical findings |

## Ограничения среды, установленные перед реализацией

- Исходный каталог не содержит `.git`; diff/commit history до разработки отсутствует.
- Docker Engine и Compose установлены; доступ к daemon из sandbox требует разрешения.
- `/dev/net/tun` отсутствует. Проект не создаёт его и не загружает kernel modules.
- Live network E2E допустим только на отдельном пригодном runner либо при уже доступном TUN.
- До фактического выполнения removal/upgrade/live E2E эти требования остаются непроверенными.

## Правило учёта результата

В итоговом отчёте приводятся команды, exit codes, количество прошедших/упавших/пропущенных тестов.
`SKIP`, недоступный Docker registry, отсутствующий TUN или неподдерживаемый клиент не считаются `PASS`.
Отказ от неподтверждённого renderer сохраняет stock subscription, но не считается подтверждением поддержки этого клиента.
