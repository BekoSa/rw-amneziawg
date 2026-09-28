# Эксплуатация, backup и удаление

Этот документ задаёт обязательные эксплуатационные процедуры. Он не заменяет
прохождение [приёмки](acceptance.md) на целевой версии Remnawave и AWG runtime.
Development lab не предназначен для публикации в Internet.

## Постоянные данные

Храните независимо:

1. AWG PostgreSQL: profiles, assignments, encrypted client keys, IPAM, revisions,
   tombstones, traffic snapshots/deltas.
2. Agent state volume: server private key, apply journal, last-known-good state,
   revocations и runtime identity metadata.
3. Controller encryption keyring: вне backup базы. Без него encrypted client keys
   восстановить нельзя. Сохраняйте все key IDs, ещё используемые строками базы.
4. Management PKI: trust roots, certificate issuance records и procedure renewal.
   Подмена сертификата не должна менять зарегистрированную identity ноды.

Remnawave имеет собственную процедуру backup. Расширение не выполняет SQL против его БД.

## Backup

Приостановите изменения profiles и key rotation. Остановите reconciliation Controller,
оставив Agent и существующие tunnels работающими. Снимите consistent dump **только AWG**
PostgreSQL с помощью `pg_dump --format=custom` в контейнере PostgreSQL. Сохраните вместе
версию migration и версию images; зашифруйте dump внешним backup-механизмом.

Для Agent сначала завершите текущий apply, затем остановите Agent-контейнер и снимите
копию его volume с сохранением прав. Копирующий контейнер получает только этот volume
read-only и отдельное место назначения. Не монтируйте Docker socket, host `/proc`, `/sys`
или каталоги чужих сервисов. Резервная копия Agent содержит секретный server key.

После backup запустите прежний Agent и Controller. Проверьте applied revision, public
server key, reconciliation и доступность stock subscription. Регулярно проверяйте
восстановление на изолированном runner; факт наличия dump не подтверждает восстановимость.

## Restore

1. Поднимите совместимую версию AWG PostgreSQL в новом extension volume.
2. Восстановите dump через `pg_restore` внутри контейнера; не направляйте его на stock БД.
3. Верните Controller encryption keyring и прежнюю конфигурацию trust.
4. Верните Agent volume и соответствующий node identity/certificate.
5. Запустите Agent, затем Controller; дождитесь reconciliation и совпадения revisions.
6. Проверьте, что tombstones удалили отозванных peers, включая ранее offline nodes.
7. Проверьте публичный server key, actual health, подписку и реальный tunnel payload.

Если AWG PostgreSQL потеряна безвозвратно, а Agent volume цел, журнал Agent продолжает
держать прежние deployments (их порты и pools), и новые deployments с теми же параметрами
Validate отклонит как конфликтующие. API удаления deployment на Agent в v1 нет: восстановите
БД из backup, а если это невозможно — остановите Agent и явно очистите его journal
(`journal.sqlite3`) в volume, сохранив файлы `*.key`, если нужна прежняя server identity
(ключ привязан к deployment id, поэтому для нового deployment будет выпущен новый ключ).

Remnawave API-токен для Controller создаётся оператором в dashboard Remnawave
(stock 3.x не принимает admin session JWT от интеграций) и передаётся как
`REMNAWAVE_API_TOKEN`. Новая, пустая установка Remnawave требует и новой AWG БД:
числовые user id могут повториться (см. [ADR 0002](decisions/0002-remnawave-3-user-identity.md)).

При потере Agent server key прежняя cryptographic identity потеряна. Не публикуйте
старый endpoint как READY. Создайте новый ключ через явную recovery-процедуру, обновите
наблюдаемый server public key, проверьте apply/health и заново выдайте клиентские configs.
IPAM и user identity при этом не должны пересоздаваться случайно.

## Upgrade и откат

Перед upgrade сохраните backup и image digests. Сначала проверьте совместимость
Controller↔Agent protocol и capabilities, затем обновляйте один компонент за раз.
Remnawave обновляется официальным image без AWG patches; его обновление само по себе
не является основанием менять AWG schema.

AWG migrations версионируются. Для несовместимой destructive migration сначала нужна
проверенная restore strategy; автоматический downgrade schema недопустим. Откат binary
разрешён только на совместимую schema. Откат runtime revision не должен возвращать
peers, отозванных после last-known-good revision.

## Отключение расширения

1. Переключите subscription routing напрямую на прежний stock endpoint, сохранив его
   HWID/response rules и TLS configuration.
2. Проверьте stock subscription и обычные Xray-подключения.
3. Остановите standalone UI, gateway, Controller и Agents.
4. Сохраните AWG PostgreSQL и Agent volumes. Не используйте `down --volumes`.

Удаление extension не требует миграции пользователей, изменения Remnawave БД или
отката stock images. Полное уничтожение AWG данных — отдельная операция с явным
решением владельца и проверенным backup.

При повторном включении сначала восстановите Agent identity и AWG database/keyring,
затем запустите reconciliation. Stock users/squads должны заново определять entitlement;
сохранённый offline peer не получает приоритет над актуальным disabled/deleted state.

## Признаки неготовности

Не публикуйте AWG материал при offline Agent, stale health, несовпадении revisions,
ошибке validation или неподдерживаемой версии клиента. При outage Controller gateway
сохраняет stock response. При outage Remnawave Controller сохраняет последний достоверный
state и не трактует timeout как удаление всех пользователей.
