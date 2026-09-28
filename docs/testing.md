# Docker lab и проверки

Все зависимости и проверки выполняются в контейнерах. На хосте нужны только Docker Engine,
Docker Compose и `/bin/sh`. Проект не меняет маршруты, policy rules, firewall, sysctl, модули
ядра и VPN хоста; каждая команда `scripts/lab.sh`, запускающая контейнеры, делает read-only
снимок маршрутов, policy rules, интерфейсов и адресов хоста до и после и падает при расхождении
(Docker `br-*`/`veth*` исключены). Firewall хоста **не сравнивается**: снимок `nft` сохраняется,
если доступен без root, но Docker сам меняет правила при создании сетей.

## Команды

```sh
scripts/lab.sh build          # все образы: tooling, services, agent (pinned amneziawg-go/tools), live client, UI
scripts/lab.sh lint           # structural safety lint объединённых compose.yaml + compose.live.yaml
scripts/lab.sh test           # unit, contract, golden, security, DB-тесты (реальный PostgreSQL в Docker)
scripts/lab.sh setup          # stock Remnawave + API-токен + lab PKI (named volumes)
scripts/lab.sh up-extension   # Controller и Gateway
scripts/lab.sh tui            # терминальное управление (TUI), `tui status`, `tui peers`
scripts/lab.sh installer      # установщик против stock-панели и stock subscription-page 8.0.0 (см. ниже)
scripts/lab.sh panel          # всё вместе: панель https://127.0.0.1:18443, stock-страница подписки https://127.0.0.1:18444/<shortUuid> (self-signed)
scripts/lab.sh credentials    # логин/пароль lab-админа Remnawave (из named volume)
scripts/lab.sh e2e            # реальный AWG-туннель и сценарии жизненного цикла (ниже)
scripts/lab.sh down           # остановить контейнеры (volumes сохраняются)
```

TUI-тесты (Textual pilot, headless) входят в `scripts/lab.sh test` (`tests/unit/tui`).

Артефакты каждого прогона — `artifacts/run-*/` (host-before/host-after, `e2e.log`).

## Топология live lab

```text
lab-control 10.240.6.0/24 ── tooling (E2E runner) ── awg-client:9000 (управление клиентом)
underlay    10.240.4.0/24 ── awg-client .3  ⇄ UDP 51820 ⇄  awg-agent .2
target      10.240.5.0/24 ──                              awg-agent .2 ── target .3 (HTTP 8080, UDP echo 8081, DNS 5353)
extension   (internal)    ── controller, gateway, awg-db, awg-agent:8443 (mTLS)
stock-api   (internal)    ── remnawave, stock-proxy, controller, gateway
```

Все сети стенда — `internal` bridge (единственное исключение — `ui-publish`: к ней подключены
только read-only nginx UI и dev-proxy панели `panel` ради портов на 127.0.0.1; safety lint это проверяет). Клиент не подключён к `target`: payload может попасть к target
только через туннель, что проверяется отрицательной пробой до поднятия интерфейса. Agent
ставит nftables-таблицу `inet awg<id>` в **своём** namespace: masquerade пула, запрет
client↔client и новых входящих соединений к клиентам, запрет выхода в служебные сети
(`AWG_EGRESS_DENY`) и закрытие локальных сокетов Agent (management API) со стороны туннеля. Full tunnel меняет default route
только в namespace `awg-client`. Требуется уже существующий `/dev/net/tun`
(`scripts/lab.sh live-preflight`); проект его не создаёт.

## Фазы `scripts/lab.sh e2e`

Профиль стенда — **AmneziaWG 3.1** (Header Protection, Content Padding, Random Trailers) на pinned
amneziawg-go v3.1.20260828; клиент стенда настраивается из Mihomo-записи подписки через `awg setconf`.

| Фаза | Что проверяется на реальном stock Remnawave 3.4.4 и amneziawg-go |
|---|---|
| `connect` | Mihomo получает `version: 3`, Throne — `wg://` с `header_protection_key`, страница подписки — `vpn://`, `/awg` — `.conf`; Happ — stock байт в байт. Squad и пользователи через stock API; регистрация ноды (mTLS); Save → Validate → Apply; READY; peer только у участника squad; AWG-запись в Mihomo-подписке через Gateway; до туннеля target недоступен; handshake, TCP, UDP, DNS через full tunnel; через туннель недоступны Controller, AWG PostgreSQL, management API Agent и bridge-адрес хоста; неподдерживаемый клиент получает байт-в-байт stock-ответ; RX/TX собраны Controller |
| `lifecycle` | split tunnel; disable → доступ пропал и AWG исчез из подписки; enable → доступ вернулся с тем же конфигом и IP; ротация client key → старый ключ отвергнут нодой, новый из подписки работает, IP тот же |
| `controller_down` | Controller остановлен: туннель работает, Gateway отдаёт stock-ответ (fail-open) |
| `agent_restart` | рестарт Agent: восстановление из журнала, server key не изменился (старый клиентский конфиг работает) |
| `controller_restart` | READY после рестарта; повторный reconcile не создаёт ревизий |
| `removed` | Gateway, Controller, Agent остановлены: stock-подписка напрямую работает; в БД Remnawave нет таблиц расширения (единственное lab-only read-only обращение к stock PostgreSQL — `psql` из `lab.sh`; сам extension эту БД не читает) |
| `restored` | повторный запуск: данные AWG DB и ключ Agent сохранены, тот же клиентский конфиг снова работает |
| `delete` | удаление пользователя в Remnawave → peer удалён с ноды, доступ пропал |

## `scripts/lab.sh installer`

В project-owned `artifacts/installer-lab` поднимается stock subscription-page 8.0.0 так, как её ставят по
инструкции Remnawave (свой compose-проект и `.env`). Затем без интерактива: `install.sh` (поиск панели,
проверка токена, PKI, Controller/Gateway, переключение subscription-page на Gateway), выпуск ключа ноды
в TUI-контейнере, `install-node.sh` (порты только на 127.0.0.1), регистрация ноды и профиль AWG 3.1 для
squad «AWG E2E», проверка что **ответ stock subscription-page** содержит AWG для Mihomo и не тронут для
Happ, `uninstall.sh` и проверка, что subscription-page работает без расширения.

## Что не покрыто E2E

- Rollback после плохой ревизии на реальном runtime: покрыт unit-тестами Agent с инъекцией
  сбоя (`tests/unit/agent`), в live lab не воспроизводится — нет валидной ревизии, на которой
  amneziawg-go стабильно падает.
- IPv6, несколько нод, quota enforcement, ротация ключа шифрования БД и webhook-only доставка —
  unit/DB тесты, без live-проверки.
- Клиенты INCY/Throne/Mihomo как реальные приложения: проверяется формат (golden), а туннель
  поднимается эталонным `amneziawg-go` + `awg setconf` из параметров Mihomo-записи.
- Upgrade matrix: проверен только stock 3.4.4; предыдущая версия — контрактными фикстурами.
- Firewall хоста не сертифицируется (см. выше; в логе помечено `UNAVAILABLE`).
