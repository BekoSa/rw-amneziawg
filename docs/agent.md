# AWG Agent

Agent — отдельный management service с локальным SQLite journal и userspace
AmneziaWG. Stock Remnawave и его БД не используются.

## Запуск и identity

Образ собирается `apps/node-agent/Dockerfile`. В исходнике закреплены:

- [amneziawg-go v3.1.20260828](https://github.com/amnezia-vpn/amneziawg-go/tree/b5928efb6ca19f0153958460c3d141f04abc5c2e);
- [amneziawg-tools v3.1.20260812](https://github.com/amnezia-vpn/amneziawg-tools/tree/ee0f0a9aa34ff0a0da4b3433b9512781cfe02843).

Версии закреплены в `ARG AWG_GO_*` / `ARG AWG_TOOLS_*` Dockerfile (тег + коммит) и попадают в
capabilities Agent (видны в TUI). Обновление — `scripts/update-amnezia.sh` или еженедельный workflow,
см. [verification](verification.md#4-обновление-ядра-amneziawg).

Entrypoint `python -m awg_agent`, один процесс/worker. Env:

| Переменная | Назначение |
| --- | --- |
| `AWG_NODE_ID` | UUID зарегистрированной ноды |
| `AWG_CONTROLLER_ID` | UUID единственного разрешённого Controller |
| `AWG_STATE_DIR` | durable volume, default `/var/lib/awg-agent` |
| `AWG_ISOLATED_RUNTIME=1` | явное разрешение работы в Docker namespace |
| `AWG_TLS_CERT`, `AWG_TLS_KEY`, `AWG_TLS_CA` | пути Docker secrets mTLS |
| `AWG_MANAGEMENT_HOST`, `AWG_MANAGEMENT_PORT` | default `0.0.0.0:8443` |

Controller certificate должен содержать URI SAN
`spiffe://awg/controller/<AWG_CONTROLLER_ID>`. Проверяется trust chain и exact SAN
до принятия HTTP, `X-Client-*` заголовки не используются. Node certificate должен
содержать DNS SAN `<AWG_NODE_ID>.agents.awg.internal`; Controller проверяет этот
TLS server name и совпадение `capabilities.node_id`.

Development разрешён только с Docker bridge, `cap_drop: ALL`, `cap_add: NET_ADMIN`,
`/dev/net/tun` и отдельными tmpfs `/run`, `/tmp`. Нет host network, privileged,
modules, host `/proc`/`sys`/netns mounts. Runtime откажется работать без Docker marker
и явного env. Это defense in depth; manifest safety lint обязателен, env сам по себе
не доказывает namespace isolation. Если host TUN отсутствует, необходим другой
isolated runner. Создавать device на development host нельзя.

## Контракт и ревизии

Роуты: `/v1/capabilities`, `/v1/deployments/{uuid}/validate`, `/apply`, `/state`,
`/traffic`; wire models находятся в `packages/contracts/awg_contracts`.

Сервис блокирует параллельные mutations и второй процесс над одним state volume.
Revision строго возрастает, тот же revision+digest можно повторять; тот же revision
с другим содержимым отклоняется. Новый revision требует CAS `expected_applied_revision`.
Полный desired snapshot сохраняется с `synchronous=FULL` до runtime mutation.

Validate отклоняет deployment, если на той же ноде другой включённый deployment уже использует
тот же UDP port или пересекающийся IPv4/IPv6 pool: все deployments живут в одном namespace.

Egress: после применения Agent атомарно (`nft -f`) пересоздаёт свою таблицу `inet awg<id>` в
namespace контейнера до `link up`: masquerade пула наружу; `drop` для сетей из
`AWG_EGRESS_DENY` (CSV CIDR: management, metadata, соседние служебные сети, bridge-адрес хоста);
запрет client↔client, чужих source-адресов и новых входящих соединений к клиентам; input-цепочка
закрывает от туннеля все локальные сокеты Agent (включая management API), кроме ICMP и ответов.
Без `AWG_EGRESS_DENY` клиенты могут достичь любых сетей, к которым подключён Agent, — в production
его нужно задавать. Невалидное значение останавливает старт Agent. Чужие таблицы и
`flush ruleset` не используются; при остановке deployment таблица удаляется.

После рестарта контейнера в `/run/amneziawg` остаётся socket умершего процесса. Если на нём
никто не слушает, Agent удаляет его и поднимает runtime заново; живой неизвестный процесс
по-прежнему не «усыновляется». Идентичный повторный Apply восстанавливает runtime при drift.

Runtime использует прямой UAPI socket, удаляет отсутствующие peers и сохраняет
счётчики существующих. `awg-quick`/shell hooks не используются. IPv4/IPv6 адреса и
MTU назначаются только внутри контейнера через `ip` argv, без shell.

Нативный UAPI не обеспечивает транзакционный switch, поэтому capability
`supports_atomic_apply=false`. Сервис предоставляет journaled apply с проверкой
actual public key, listen port, parameters, addresses, MTU, peers и process health.
Неудачное применение откатывает LKG, отфильтрованный по последнему разрешённому
множеству `(public_key, key_generation, IPs)`. При ошибке rollback runtime
останавливается. Applied revision после любого rollback неизвестен (`null`),
state `DEGRADED`, подписка такую ноду исключает. Restart использует тот же журнал
и не восстанавливает отозванных peers из старого LKG.

Бинарная версия runtime 3.1 не означает поддержку всех wire versions. Adapter
`amneziawg-go-v3` принимает только явно заявленные capabilities. AWG 3/3.1 пока
отклоняются. Strict validators отвергают неизвестные поля, control characters,
overlapping headers и непроверенную signature grammar.

## Данные и эксплуатация

Каждый deployment получает локальный X25519 server key в файле mode 0600;
Controller получает только public key. SQLite содержит конфигурацию с client
public keys, но не server private keys. Volume mode 0700, backup требует защиты
как секретных данных. Backup делается после остановки Agent либо согласованным
SQLite backup вместе с `.key` файлами. Volume сохраняют при uninstall. При потере
ключа server identity изменится, поэтому восстановить старые подписки без backup
нельзя.

Management и `/health/live`, `/health/ready` используют mTLS. Liveness отделён от
готовности deployments. Health подтверждает actual state, но не заменяет handshake
и payload E2E. Счётчики читаются из UAPI, sequence сохраняется перед возвратом.
Перезапуск runtime создаёт новый epoch; сброс счётчиков не должен интерпретироваться
как отрицательный трафик. Между последним опросом и неожиданным падением runtime
возможна потеря несчитанных байтов: UAPI не имеет durable packet ledger.

## Проверка

Unit tests `tests/unit/agent` используют stateful test runtime только внутри Docker.
Production не имеет env-переключателя mock driver. Настоящий handshake и payload
проверяются отдельным Docker network lab; до его успешного запуска connectivity
не считается подтверждённой.

Full-tunnel egress требует `net.ipv4.ip_forward=1` (и IPv6 forwarding при IPv6)
в sysctls **контейнера**. Agent управляет отдельной nft table `awg<deployment-prefix>`
в этом namespace: masquerade только источников profile pool, выходящих за пределы
его AWG interface. Existing tables не очищаются. UDP listen port должен быть
опубликован на bridge container согласно profile. Management порт доступен только
доверенному Controller. Для нескольких deployments нужны непересекающиеся pools
и различные UDP ports; каждый публикуется отдельно. Docker bridge egress и IPv6
upstream route должны быть настроены оператором на целевой production-машине.
