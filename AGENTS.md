# Remnawave AWG Extension — инструкции для Codex

## Язык

Всегда отвечай пользователю и главному агенту на русском языке.
Код, имена API, идентификаторы, протоколы и общепринятые технические термины можно оставлять на английском, если так точнее.

## Читать в первую очередь

Перед существенной работой прочитай:

- `docs/MASTER_PROMPT.md`
- `docs/architecture.md`, если файл существует
- применимые вложенные `AGENTS.md`

`docs/MASTER_PROMPT.md` — главный источник продуктовых, архитектурных и safety-требований.

## Архитектурные инварианты

Stock Remnawave считается внешней зависимостью.

Никогда не патчить:

- Remnawave backend;
- базу данных Remnawave;
- Remnawave Node;
- Xray-core ради добавления AWG.

Не читать PostgreSQL Remnawave напрямую.

Использовать документированные API и webhooks.

AWG-расширение владеет только своими данными.

Удаление расширения не должно ломать stock Remnawave.

## Безопасность development-host

На development-host уже работает VPN.

Проект не должен менять сетевую конфигурацию хоста.

Все runtime-зависимости проекта должны запускаться только в Docker-контейнерах.

Это относится в том числе к:

- Remnawave Panel;
- Remnawave Node;
- PostgreSQL;
- Redis/Valkey;
- subscription-компонентам;
- AWG Controller;
- AWG Agent;
- AWG test clients;
- reverse proxy и прочим сервисам, нужным для тестов.

Не устанавливать project-specific зависимости в host OS.

Не использовать пакетный менеджер хоста для настройки проекта.

Если нужен софт — добавить его в Dockerfile или Docker Compose service.

Никогда не изменять на host:

- routes;
- ip rules;
- nftables/iptables;
- WireGuard;
- AmneziaWG;
- TUN/TAP;
- sysctl;
- kernel modules;
- systemd services;
- существующую VPN-конфигурацию.

## Development networking

Development и network tests должны использовать Docker bridge networking.

`network_mode: host` запрещён в development/test compose-файлах.

`privileged: true` запрещён.

Не монтировать `/lib/modules`, host `/proc`, host `/sys` или `/var/run/netns`.

Full-tunnel VPN tests выполнять только внутри изолированных test-client containers.

Production deployment manifests могут описывать другую топологию, но Codex никогда не должен применять production host-networking к development-host.

## Делегирование

Для существенных изменений используй project custom subagents.

Используй `architect` для:
архитектуры, контрактов, state machines и cross-component решений.

Используй `remnawave_integrator` для:
Controller, Remnawave API/webhooks, reconciliation, users, squads и IPAM.

Используй `awg_node` для:
Agent runtime, AmneziaWG, peers, revisions, health и counters.

Используй `subscription` для:
subscription gateway и format renderers.

Используй `ui` для:
standalone AWG management UI.

Используй `network_qa` для:
Docker test environment, network isolation и integration/E2E tests.

После существенной реализации используй `reviewer` для независимой финальной проверки.

Не поручай параллельным агентам редактировать одни и те же файлы.

Если работа затрагивает несколько компонентов — сначала зафиксируй или обнови контракт, затем делегируй реализацию.

## Проверка перед завершением

Перед тем как считать существенную задачу завершённой:

1. запусти релевантные unit tests;
2. запусти contract tests;
3. запусти subscription golden tests, если это относится к изменению;
4. запусти Docker-isolated integration tests;
5. запусти network safety checks, если менялся networking;
6. проверь, что stock Remnawave работает без extension;
7. для архитектурных/security-sensitive изменений запусти независимого `reviewer`.

Не утверждай, что networking работает, только потому что конфиг успешно сгенерирован.
Проверяй реальную connectivity внутри Docker network lab.
