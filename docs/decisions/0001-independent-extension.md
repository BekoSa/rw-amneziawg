# ADR 0001: независимый extension и versioned control plane

Статус: принято для v1.

Используем отдельные Controller/PostgreSQL/Agent/Gateway/UI, Python/Pydantic
wire schemas и documented Remnawave API. Встраивание в backend/Node/Xray или
прямые SQL-запросы к stock DB запрещены требованиями. Отдельный gateway позволяет
вернуть stock subscriptions при AWG failure и удалить extension без миграции
Remnawave. Цена — eventual consistency и собственные reconciliation/traffic state.

Full immutable desired snapshots с revision/digest упрощают reconciliation после
offline/retry. Durable watermark и revocation overlay предотвращают stale apply
и восстановление отозванного доступа при rollback. Extra fields запрещены;
изменения wire semantics проходят явную negotiation/version bump.

Client key принадлежит Controller и шифруется; server private key принадлежит
Agent. Это сохраняет server identity независимо от Controller DB compromise.

Development использует Docker bridge и userspace AWG. Нет fallback на host
networking/kernel modules. Отсутствие TUN переносит real network acceptance на
isolated runner, а не снижает критерий доказательства до config generation.
