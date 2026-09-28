# План исполнения полного MASTER_PROMPT

Статус этого файла — план acceptance, не отчёт о завершении. Каждый пункт закрывают
проверяемые artifacts/tests. Отсутствующий TUN не уменьшает scope реализации;
network acceptance переносится на isolated runner и остаётся непроверенным до
получения результатов. Production-ready нельзя заявлять по unit tests.

## Последовательность и зависимости

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
