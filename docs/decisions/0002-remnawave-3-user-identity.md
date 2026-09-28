# ADR 0002: идентичность пользователей Remnawave 3.x

Статус: принято.

## Контекст

Требования (MASTER §4, §11) связывают AWG-данные с Remnawave через `remnawave_user_uuid`.
Stock Remnawave 3.4.4 (проверено по `openapi.json` официального образа
`remnawave/backend:3.4.4`) такого поля не отдаёт: пользователь идентифицируется
числовым `id`, а `GET/DELETE /api/users/{userId}` и `actions/*` принимают именно число.
Remnawave 2.x отдавал `uuid`.

Кроме того, stock 3.x принимает admin session JWT только от собственного dashboard
(`x-remnawave-client-type: browser`); интеграции обязаны использовать API-токены
(`POST /api/tokens`, создаются оператором в dashboard).

## Решение

- Вся зависимость от DTO остаётся в `packages/remnawave-client`.
- Внутренний ключ пользователя расширения — UUID:
  - 2.x: upstream `uuid` как есть;
  - 3.x: `uuid5(6f1c0a52-8a4e-5d0f-9d57-3a1f3c1c7e21, "remnawave-user:<id>")`.
- `RemnawaveUser.upstream_id` — непрозрачный ключ для точечных запросов к upstream
  (3.x — число, 2.x — UUID). Controller не интерпретирует его, только передаёт адаптеру.
  Адаптер пропускает в URL лишь положительное целое или валидный UUID.
- Контроллер использует только API-токен. В lab его один раз создаёт
  `scripts/bootstrap_stock.py`, эмулируя действие оператора в dashboard;
  в production токен создаёт оператор.

## Последствия

- Числовой `id` — primary key stock БД и не переиспользуется при штатной работе;
  UUIDv5 стабилен между рестартами и restore stock из backup.
- Если оператор пересоздаст stock БД с нуля, `id` могут совпасть с прежними и
  унаследовать AWG-ключи. Процедура «новая установка Remnawave» должна начинаться с
  отдельной AWG БД (см. [operations](../operations.md)).
- Апгрейд Remnawave 2.x → 3.x меняет ключ идентичности (UUID → UUIDv5 от `id`). Автоматической
  миграции нет: если точечный GET вернёт пользователя с другой идентичностью или ошибку,
  reconcile останавливается (fail closed), ничего не удаляя. Такой апгрейд требует явной
  миграции AWG БД, которая пока не реализована.
- Контрактные тесты фиксируют форму DTO 3.4.4 (`tests/unit/controller/test_remnawave.py`),
  E2E — реальное поведение stock образа.
