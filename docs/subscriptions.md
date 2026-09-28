# Подписки и форматы клиентов

Gateway стоит между пользователем (или stock subscription-page) и stock Remnawave. Сначала он
получает обычный ответ Remnawave и только потом добавляет AWG в формате конкретного клиента.
Логика подписок Remnawave (аутентификация, HWID, лимиты, response rules) не дублируется и не меняется.

## Где стоит Gateway

| Установка Remnawave | Как подключается Gateway (делает `install.sh`) |
|---|---|
| Stock `remnawave/subscription-page` | В её `.env` `REMNAWAVE_PANEL_URL` меняется на `http://awg-gateway:8081` (бэкап `.env.awg-backup`). Gateway пропускает к панели только её read-only маршруты (`AWG_GATEWAY_SUBPAGE_PASSTHROUGH=1`). |
| Подписки напрямую с панели | Reverse proxy отправляет `/api/sub/*` на `awg-gateway:8081` (пример выводит `install.sh`). |

Gateway доверяет `X-Forwarded-*` только в этой топологии (`AWG_GATEWAY_TRUST_FORWARDED=1`): он
доступен лишь из Docker-сети панели. Удаление (`uninstall.sh`) возвращает прежний `.env`, и подписки
снова идут прямо в Remnawave.

## Что получает каждый клиент

| Клиент (User-Agent) | Формат AWG | AWG 2 | AWG 3.1 | Источник |
|---|---|---|---|---|
| AmneziaVPN ≥ 5.0.1.5 | `vpn://` ключ на странице подписки | да | да | `amnezia-client` 5.0.3.0 `importController.cpp`, `awgProtocolConfig.cpp` |
| Mihomo и приложения на нём (`mihomo`, Clash Verge, FlClash, ClashMeta for Android, ClashX Meta, Nyanpasu, Koala Clash) | `type: wireguard` + `amnezia-wg-option`, для 3.1 `version: 3` | да | да | mihomo v1.19.31 `adapter/outbound/wireguard.go` |
| Throne (`Throne/…`, последняя 1.3.1) | `wg://…&enable_amnezia=true&jc=…&header_protection_key=…` | да | да | Throne 1.3.1 `src/configs/outbounds/wireguard.cpp` |
| INCY (`INCY/…`) | `amneziawg://<base64url .conf>#Имя` | да | нет* | incy.gitbook.io `subscription-format` |
| Прочие клиенты со списком ссылок (v2rayNG, Hiddify …) | та же строка `amneziawg://` (незнакомые схемы клиенты пропускают) | да | нет* | — |
| Happ | ничего: по документации Happ поддерживает только VLESS/VMess/SS/Socks5/Trojan/Hysteria2 | — | — | happ.su dev-docs |
| sing-box JSON, legacy Clash/Stash YAML | ответ не меняется, приватные данные даже не запрашиваются | — | — | — |
| Страница подписки (`/api/sub/<id>/info`) | в `links` добавляются `amneziawg://` (для AWG 2) и `vpn://`. Stock-страница показывает ссылки только при включённой настройке **Show connection keys** (Remnawave → конфиг страницы подписки → baseSettings) | да | да | — |
| Браузер без страницы подписки (`/api/sub/<id>`, `Accept: text/html`) | stock отвечает тем же JSON, что и `/info`; в `links` добавляются те же ссылки | да | да | — |
| Приложение AmneziaWG (конфиг-файл) | `/api/sub/<id>/awg` отдаёт `.conf` после проверки доступа stock-подпиской | да | да | — |

\* INCY документирует только поля AWG 2 (Jc…I5). Для профиля 3.1 INCY и «прочие» клиенты AWG-строку
не получают, пока поддержка не подтверждена; пользователям остаются AmneziaVPN, Mihomo и Throne.

Имя записи во всех форматах — название профиля (например «🇩🇪 Германия · AWG»).

На странице подписки ключ `vpn://` получает фрагмент `#Имя профиля`: stock-страница берёт название
ссылки из фрагмента (без него пишет «Unknown»). AmneziaVPN при импорте фрагмент безвредно пропускает —
проверено на Qt 6.11 (`QByteArray::fromBase64` + `qUncompress`, как в `ImportController`).

**Заглушки Remnawave.** Если у пользователя нет Xray-хостов, stock отдаёт записи-заглушки
(`→ No hosts found`, `→ Check Hosts tab` …; нулевой UUID, `0.0.0.0:1`). Когда Gateway добавляет AWG, он
их убирает — из списков ссылок, из Mihomo (proxies и группы; правила stock не трогаются) и со страницы
подписки. Если AWG этому клиенту не добавлен (клиент без поддержки, пользователь отключён, лимит HWID),
ответ остаётся stock вместе с заглушками — в них Remnawave объясняет причину.

## AmneziaWG 3.1

Профиль версии `3.1` добавляет к AWG 2 (Jc/Jmin/Jmax, S1–S4, H1–H4 диапазонами, I1–I5):

| Параметр | Сторона | Значение по умолчанию (генератор TUI) |
|---|---|---|
| `HeaderProtectionKey` | сервер и клиент (обязан совпадать) | случайные 32 байта (base64); требует S1–S4 ≥ 12 |
| `RandomTrailers` | сервер и клиент (без него приёмник отбрасывает удлинённые handshake) | `true` |
| `ContentPaddingAddition` | клиент (amneziawg-go ограничивает окном UDP) | случайный диапазон, например `14-120` |
| `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts` | клиент | `100-120`, `3-7`, `150-180`, `5-15`, `15-20` |
| `DisableCookies` | локально (не отвечать cookie под нагрузкой) | `true` |
| `I1` (есть и в AWG 2) | отправитель handshake | сигнатурный пакет: DNS-ответ на A-запрос популярного домена со случайным ID, TTL и адресом |

Таймеры, `RandomTrailers`, `DisableCookies` и форма `I1` совпадают с тем, что AmneziaVPN 5.0.3.0
выставляет своим серверам 3.1 (`protocolConstants.h`, `awgInstaller.cpp`). Для профиля, созданного до
этих дефолтов, в TUI есть кнопка **«Дополнить»**: добавляет недостающие поля и не трогает ключи,
заголовки и паддинги. Клиенты получат новые поля при обновлении подписки; до этого клиенты без
`RandomTrailers` не пройдут handshake.

Булевы значения записываются по-разному, и Gateway это учитывает: UAPI amneziawg-go принимает
`true/false`, `awg setconf` и AmneziaVPN — `on/off` (AmneziaVPN считает включённым всё, кроме `off`),
Throne — `true/false`, Mihomo — YAML-булево. Сервер с `HeaderProtectionKey` не принимает клиентов AWG 2,
поэтому для смешанного парка создаются два профиля на разных UDP-портах.

## Безопасность и fail-open

- Любая ошибка Controller, БД, renderer или неподходящий формат — пользователь получает исходный ответ
  Remnawave байт в байт (status, заголовки, `subscription-userinfo`).
- Material (приватный ключ клиента) запрашивается только после HTTP 200 от stock и только если формат
  ответа вообще можно дополнить; кэш `private, no-store`, ETag stock удаляется при изменении тела.
- Наружу доступны только `/api/sub/<token>[/…]`; admin/auth API Remnawave через Gateway недоступен.
  Пути проверяются по сырым байтам (encoded traversal не проходит).
- `/metrics` Gateway закрыт токеном `AWG_GATEWAY_METRICS_TOKEN`.
- Тексты ошибок, токены и ключи не логируются и не возвращаются клиенту. Для диагностики Gateway пишет
  строку на каждую подписку: `subscription client=mihomo awg=added ready_peers=1`, либо
  `awg=unchanged ready_peers=0` (пользователь не в squad профиля или нода профиля не READY/online), либо
  `awg=skipped reason=…` (`stock-denied`, `no-awg-for-client`, `stock-format-not-extendable` …):
  `docker logs awg-gateway`.

Проверки: golden-тесты `tests/golden/test_client_formats.py`, `tests/golden/test_subscription.py`,
Gateway — `tests/unit/subscription/test_gateway.py`, реальный туннель AWG 3.1 — `scripts/lab.sh e2e`,
stock subscription-page через Gateway — `scripts/lab.sh installer`.
