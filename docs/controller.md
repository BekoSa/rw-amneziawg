# AWG Controller

Реализация владеет только собственной PostgreSQL. Remnawave читается через публичный HTTP API
(`REMNAWAVE_API_URL` указывает на `.../api`): `GET /api/users`, `GET /api/users/{id}`,
`GET /api/users/by-short-uuid/{shortUuid}`. Нужен API-токен Remnawave (создаётся в dashboard;
admin session JWT stock 3.x интеграциям не принимает). Код backend и БД Remnawave не используются.
Идентичность пользователей 3.x (числовой `id` → UUIDv5) описана в
[ADR 0002](decisions/0002-remnawave-3-user-identity.md).

Документированные источники: [официальный Users SDK](https://github.com/remnawave/python-sdk/blob/production/remnawave/controllers/users.py), [официальные DTO](https://github.com/remnawave/python-sdk/blob/production/remnawave/models/users.py), [webhooks](https://docs.rw/features/webhooks/).

## Политики

Один client keypair на пользователя, приватный ключ AES-256-GCM; blob `v1:<key_id>:…`, associated
data — key id и UUID пользователя. Ключ шифрования задаётся извне (`AWG_KEY_ENCRYPTION_KEY`,
`AWG_KEY_ENCRYPTION_KEY_ID`); для ротации старые ключи перечисляются в
`AWG_KEY_ENCRYPTION_PREVIOUS_KEYS=id:base64,...`, и reconcile перешифровывает строки текущим ключом.
Потеря ключа исключает восстановление client keys из backup БД.

Ротация client key: `POST /api/v1/users/{user_uuid}/rotate-key` (admin). Новый keypair,
`key_generation+1`, тот же IP; старый public key попадает в `revoked_public_keys` всех deployments
пользователя, поэтому ни rollback Agent, ни offline-нода не вернут ему доступ. Подписка отдаёт
новый ключ только после применения ревизии на ноде.

Webhook только пробуждает reconciliation. Fetch failure не меняет entitlement. Отсутствие UUID в paginated list требует подтверждающего GET конкретного пользователя с HTTP 404. Это защищает от удаления peers из-за движения страниц при concurrent upstream updates.

IP allocation выполняется внутри транзакции, сериализованной по pool; PostgreSQL constraints защищают адрес и assignment. Адрес удалённого peer остаётся занят до подтверждения удаления Agent, затем находится в quarantine. Тот же peer (disable → enable) забирает свой адрес из quarantine обратно (`released_by`), только если свободны все его семейства адресов; иначе получает новый набор. Чужим peers адрес до истечения quarantine не выдаётся.

Traffic snapshots имеют epoch и sequence. Повторный и устаревший sequence игнорируются, смена epoch и уменьшение counter трактуются как reset. AWG usage хранится отдельно. Policy `combined` учитывает upstream Xray + AWG и отключает только AWG. Usage никогда не записывается в stock Remnawave. Сброс quota определяется authoritative `lastTrafficResetAt`, webhook сам usage не сбрасывает.

## Проверки

Unit/DB-тесты — `scripts/lab.sh test` (отдельная одноразовая БД `awg_test`). Поведение против реального stock Remnawave 3.4.4 и Agent — `scripts/lab.sh e2e`, см. [testing](testing.md).
