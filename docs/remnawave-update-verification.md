# Проверка обновления Remnawave — 2026-10-12

Проверено обновление backend с 3.4.4 до **3.4.5**. Stock Node 3.4.2
проверен по upstream diff; его runtime handshake в этом окружении не проверялся.
Subscription Page остаётся на актуальном теге 8.0.0. Default pins не изменены.

## Совместимость

- [Backend 3.4.4 → 3.4.5](https://github.com/remnawave/backend/compare/3.4.4...3.4.5):
  используемые публичные API пользователей, user DTO, Internal Squads и подписанные
  user webhooks не изменились. Prisma migrations в diff отсутствуют.
- `SERVICE_SNI_VERIFICATION=true` сохраняет прежнее поведение. Исправление exact
  numeric user-id filter не затрагивает адаптер: он не использует этот фильтр.
- [Node 3.4.1 → 3.4.2](https://github.com/remnawave/node/compare/3.4.1...3.4.2):
  удалён `CipherType.NONE`. Перед обновлением stock Shadowsocks необходимо проверить
  используемые ciphers. AWG и TrustTunnel этот enum не используют.
- Новый optional post-start webhook `service.core_started` не имеет HMAC и не
  предназначен для endpoint расширения `/webhooks/remnawave`, принимающего
  подписанные пользовательские события.

## Реальный Docker smoke

Запуск `REMNAWAVE_IMAGE=remnawave/backend:3.4.5 scripts/stock-http-smoke.sh`
на сохранённой lab database от 3.4.4 завершился с exit 0:

| Фаза | Результат |
|---|---|
| Расширение работает | 1 passed |
| Controller/Gateway остановлены | 1 passed |
| Расширение восстановлено | 1 passed |

Проверены реальные HTTP API, direct stock subscriptions, Gateway passthrough,
reconcile и сохранение TrustTunnel draft. Evidence:
`artifacts/stock-http-run-20261012T013707-80108/`.
Image ID: `sha256:b16d724b90fd7c9fec2df04bd28938a671cafc62894105068e11550ee3449c56`.
Host VPN, interfaces, routes и policy rules не изменились. Firewall certification
недоступна из-за read permissions/tool.

Дополнительно прошёл **201 тест**: адаптер Remnawave, contracts, subscription
golden и subscription unit tests. Лог:
`artifacts/remnawave-3.4.5-focused-tests.log`. Этот набор использует fixtures/mocks;
реальная совместимость с backend подтверждается отдельным HTTP smoke выше.
Cleanup завершился с exit 0, работающих контейнеров не осталось; evidence:
`artifacts/remnawave-3.4.5-cleanup.log`.

## Границы результата

Это проверка lab upgrade, а не обновление production. Полные stock Node handshake,
AWG full-tunnel, пользовательские Xray/Shadowsocks конфигурации требуют отдельного
prepared runner с TUN и проверки конфигураций конкретной установки.

## Доступ к GitHub

Remote: `git@github.com:BekoSa/rw-amneziawg.git`. Первоначальный SSH dry-run отказал с
`Permission denied (publickey)`. После добавления оператором SSH ключа повторный
`git push --dry-run` прошёл с exit 0, используя официальный GitHub host key
во временном known_hosts; системная SSH-конфигурация не изменялась.
GitHub connector подтвердил `permissions.push=true` и `admin=true` для репозитория:
публикация через GitHub API доступна. В этой проверке commit/push не выполнялись.
