# План исполнения полного MASTER_PROMPT

Статус этого файла — план acceptance, не отчёт о завершении. Каждый пункт закрывают
проверяемые artifacts/tests. Отсутствующий TUN не уменьшает scope реализации;
network acceptance переносится на isolated runner и остаётся непроверенным до
получения результатов. Production-ready нельзя заявлять по unit tests.

## Последовательность и зависимости

### Запрошенные доработки — 2026-10-12

Это план будущей работы, не перечень реализованных возможностей.

1. **TrustTunnel в обычной подписке Throne.** Исправить исключение Throne из
   capability matching; добавить проверенный renderer для `tt://` в URI/Base64
   подписках. Целевой сценарий: Throne 1.3.2, TT deployment READY, существующие
   stock/AWG записи сохраняются. Нужны upstream evidence, golden tests и
   фактический импорт/подключение клиента в Docker lab.
2. **Однократная установка универсальной ноды.** После установки управлять
   включением AWG/TrustTunnel, любыми свободными TCP/UDP-портами и ACME из TUI.
   Отдельный production ingress принимает публичные соединения; Agent с AWG
   остаётся в Docker bridge. Ни Docker socket, ни управление host routes/NAT
   не передаются ingress. Старым нодам нужна одна миграция с сохранением identity,
   ключей и state. Firewall провайдера вне согласованного scope. Дизайн и
   контракты должны пройти review до реализации.
3. **Правила выдачи по приложениям и форматам, аналогично response rules
   Remnawave.** В TUI разрешать/запрещать AWG и TrustTunnel для выбранных
   приложений, версий и форматов: URI/Base64, Mihomo, native export и другие
   подтверждённые renderer capabilities. Отдельный доступ пользователей/squads
   этим пунктом не изменяется.

   Правила имеют явный порядок, default policy и preview результата для
   заданного User-Agent/формата. Неизвестная версия не получает неподтверждённый
   протокол; правило allow не обходит техническую capability-проверку.
   Одна политика применяется к обычной подписке, provider-запросам, `/info` и
   native download: запрещённые entries и ссылки не появляются через другой
   маршрут. Явный deny не должен превращаться в выдачу секрета при outage.
   Stock auth/HWID/response rules исполняются до enrichment; stock Remnawave
   сохраняет собственную логику. Блокирование всей stock-подписки, если требуется,
   нужно отдельно согласовать с поддерживаемыми публичными response rules API,
   без patches и прямого доступа к stock DB.

   Acceptance: allow/deny/default/order matrix, версии и malformed User-Agent,
   все поддержанные форматы и маршруты, запрет обхода через provider/native/info,
   outage behavior, сохранность stock/AWG entries, sanitized diagnostics и
   независимый security review. Владельцы: architect (контракт), subscription
   (Gateway/renderers), remnawave_integrator (policy storage/API), ui (TUI),
   network_qa и reviewer (проверки).

1. Architect фиксирует архитектуру, strict Pydantic models, JSON Schema/OpenAPI,
   contract tests и семантику state/auth/idempotency.
2. Network QA строит Docker-only tooling и stock lab; проверяет Compose safety,
   host baseline, доступность Docker/TUN и pinned image/build reproducibility.
3. После contracts независимо работают Controller, Agent, gateway; UI начинает
   после фиксации admin API. Новые shared payloads согласуются architect.
4. Root связывает consumers, environment, TLS/secrets/bootstrap, UI и production
   examples. Все tests исполняются tooling containers.
5. После lab safety gate выполняются real userspace tunnel tests на доступном
   isolated runner. Если gate не пройден, tests помечаются blocked, не passed.
6. Независимый reviewer проверяет итоговые файлы, security boundaries и evidence;
   исполнители исправляют findings, релевантные проверки повторяются.

## Матрица требований и acceptance

| Master §§ | Реализация / владелец | Обязательное доказательство |
|---|---|---|
| 1–6, 37–43 | архитектура, UI, reverse proxy, root | stock containers unchanged; independent UI; stock route/removal/restore |
| 7–10, 42–43 | Controller/Remnawave adapter | public API contracts current+previous, pagination/error distinction, lifecycle/squads/missed webhook |
| 11–14 | Controller DB/keys/IPAM | PostgreSQL concurrency, exhaustion/quarantine, AEAD and key persistence; no stock DB credentials |
| 15–22 | Agent/runtime/contracts | capabilities, validation, replay/stale/conflict rejection, crash journal, rollback, mTLS rejection |
| 23–32 | gateway/renderers/registry | exact Base64/Mihomo goldens, INCY/Throne verified mappings, incompatible exclusion, outage stock equality |
| 33–36 | Agent/Controller traffic | delta/reset/epoch/rotation/replay tests, quota policy, no direct DB sink |
| 44–67 | network QA | Docker-only safety lint, host before/after, no bypass, handshake, TCP, UDP, DNS, full/split tunnel |
| 68–71 | все компоненты | secret redaction, no private server wire fields, metrics, separate live/ready/dependencies |
| 72–76 | Controller/Agent/contracts | offline conservative state, tombstones/reconnect, concurrent events, mixed version/retry |
| 77–79 | Controller/root | versioned AWG-only migrations, backup/restore instructions/test, preserved volumes on uninstall |
| 80–90 | QA/root/reviewer | complete pyramid, actual payload, independent review, accurate limitations/evidence |

## Наборы проверок

- Unit: IPAM policy, desired state, capability matching, validators, traffic deltas,
  crypto handling, renderer primitives, rollback authorization overlay.
- Contract: schema negative cases; generated-schema drift; Controller↔Agent HTTP;
  documented Remnawave DTO fixtures и supported-version checks.
- Golden: exact client configs, Unicode, quoting/escaping, line endings,
  malformed input, readiness exclusions и fail-open.
- Integration: реальные AWG PostgreSQL, Controller, Agent, stock Remnawave;
  lifecycle/quota/tombstones/restarts, duplicate allocations и concurrent apply.
- Network E2E: реальный pinned userspace AWG в Docker namespace; handshake и
  payload, peer disable/enable/delete, reconnect/key rotation, server/Agent/
  Controller restart, rollback после плохой revision; client не имеет bypass.
- Removal/upgrade: stock доступен без extension, данные extension сохранены,
  повторный reconcile восстановил peers; current/previous supported upstream.

## Условия честного завершения

В итоговом отчёте отдельно указать: реализовано, проверено контейнерными tests,
проверено real network E2E, blocked environmental verification и upstream/client
capabilities без evidence. Не заменять real stock Remnawave mock-сервисом в removal
acceptance. Не выдавать runtime=mock за реальный AWG Agent. Не утверждать
поддержку клиента только по наличию renderer class.

Первоначальное окружение не имело /dev/net/tun. Пока prepared isolated runner
не предоставил handshake/payload и host-integrity результаты, network часть
Definition of Done остаётся открытой. Ручное создание host TUN запрещено.
