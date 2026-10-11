# TrustTunnel

TrustTunnel управляется расширением через тот же Controller и mTLS Agent, что и
AmneziaWG. У него отдельные профили, ревизии и username/password; WireGuard keys
и IPAM к нему не относятся. Remnawave остаётся источником users/status/expiry/squads.

## Установка ноды

На production VPS с существующим Docker и TUN установщик может включить оба runtime:

```sh
./install-node.sh --trusttunnel --secret-file /path/to/node-key
```

По умолчанию TrustTunnel использует TCP/UDP443, HTTP-01 TCP80 и TLS-ALPN-01 TCP443.
Установщик проверяет занятые TCP-порты и не останавливает чужой сервис.
Если443 занимает Xray, оставьте его и используйте HTTP-01 с отдельным data port:

```sh
./install-node.sh --trusttunnel --trusttunnel-port 2443 --acme-tls-port 0 \
  --secret-file /path/to/node-key
```

Если80 недоступен, оставьте TLS-ALPN-01:

```sh
./install-node.sh --trusttunnel --acme-http-port 0 --secret-file /path/to/node-key
```

Параметры включения и портов сохраняются при обновлении ноды. В каталоге установки
создаётся `trusttunnel.compose.yaml`: overlay содержит только валидированные mappings
доступных портов. Обычный AWG install без opt-in не публикует новые порты.
В production node overlay допустим минимальный NET_BIND_SERVICE для низких портов.
Development использует отдельный isolated Docker lab; production manifest на нём
не запускается.

## Профиль и сертификат

В TUI откройте TrustTunnel, создайте профиль и задайте:

- публичный endpoint и data port, совпадающий с установленным;
- DNS domain сертификата и ACME contact email;
- одну ноду, пользователей или Internal Squads;
- `http-01`, `tls-alpn-01` или `auto`;
- environment `production` для публичного Let's Encrypt.

DNS A/AAAA должны указывать на ноду, выбранные ACME-порты должны быть доступны
из Интернета. Ошибочная AAAA-запись также может помешать validation. DNS-01 и
wildcard в этой версии не реализованы.

Save сохраняет draft; Validate проверяет контракт, capabilities и ingress без
выпуска сертификата. Apply создаёт desired revision. Нода сама выполняет ACME
issuance; на время ожидания статус APPLYING. Публикация возможна только после
READY, когда применена нужная ревизия и TLS probe проверила реально предъявленный
сертификат. ACME-ключи и TLS private key остаются на ноде; management PKI отдельная.

HAProxy читает ClientHello без TLS termination: ALPN `acme-tls/1` идёт в lego
solver, обычный TLS — в TrustTunnel. UDP направляется в его отдельный container
port9443. HTTP ingress обслуживает только ACME challenge path. Management API не
публикуется через transport ingress.

Renewal выполняется внутри Agent без Controller: durable ACME account, ARI/срок
действия сертификата, backoff и jitter. После успешного обновления нода проверяет
PEM/SAN/key match, публикует bundle и перезагружает TrustTunnel; затем проверяет
served fingerprint. Отдельный durable last-served pointer позволяет вернуть
прежний валидный bundle при ошибке загрузки нового сертификата. Ошибка renewal сохраняет ещё действующий сертификат с warning;
истёкший сертификат исключает профиль из подписок. Restart TrustTunnel не меняет AWG
ключи. Staging и Pebble сертификаты не становятся production READY.

## Доставка пользователю

Нативные `tt://?` и CLI TOML получают через exporter закреплённого TrustTunnel.
Для официального клиента нода дополняет экспортную цепочку проверенным root из
доверенного CA store: custom certificate клиента использует отдельный trust store.
Формат создаёт upstream exporter; сертификат, обслуживаемый endpoint, не меняется.
Gateway предоставляет отдельную ссылку скачивания:

```text
/api/sub/<subscription-token>/trusttunnel?format=deeplink
/api/sub/<subscription-token>/trusttunnel?format=toml
```

Этот маршрут сначала проверяет stock subscription auth/HWID/response rules; при
ошибке расширения возвращает исходный stock response. Подписки и экспорт содержат
password, поэтому ответы имеют `no-store`, их нельзя помещать в публичный cache.
Ссылку подписки храните как credential. `/info` предоставляет native links и
download metadata; отображение дополнительных кнопок stock subscription page
нужно проверять для её конкретной версии.

Mihomo TT output допускается для подтверждённого core>=1.19.32. Unknown embedded
core и неподдерживаемые клиенты сохраняют stock/AWG поведение. Для Throne и INCY
поддержка TrustTunnel не заявлена. Отзыв/expiry/выход из squad и отдельная rotation
TT credentials должны удалять доступ и завершать старые sessions.

## Данные, восстановление и ограничения

Controller хранит `tt_*` таблицы в своей БД; password и sensitive desired/revision
payloads шифруются AEAD. Backup требует также encryption keys Controller.
Agent state volume хранит journal, credentials, ACME account и сертификатные
bundles. Сохраняйте этот volume при update/removal и включайте его в защищённый
backup. Потеря ACME account может привести к повторному выпуску и rate limits.

TrustTunnel traffic пока не включается в AWG ledger/combined traffic accounting;
status/expiry/limits, уже известные из Remnawave, применяются как entitlement.
Не считайте отсутствие TT counters нулевым расходом трафика.
Один TT deployment на ноду; редактируйте существующий профиль для смены его настроек.

## Проверка

```sh
scripts/lab.sh test -q -m 'not network'
scripts/trusttunnel-lab.sh build
scripts/trusttunnel-lab.sh lint
scripts/trusttunnel-lab.sh test
scripts/stock-http-smoke.sh
```

TT lab использует Pebble и реальные TrustTunnel/Mihomo binaries, bridge networks и
раздельные client/target сети. Target bridge полностью internal; адреса192.0.0.9/.10
выбраны из исключений upstream для тестового назначения, чтобы production запрет
доступа к private targets оставался включённым. Проверка пересечения subnet
выполняется до создания сети. Это проверяет ACME и proxy connectivity без TUN;
full-tunnel и существующий AWG E2E требуют prepared isolated runner с TUN. Pebble
не доказывает успешный выпуск production Let's Encrypt: для этого нужны реальный
domain, DNS, ingress и операторский smoke. Конкретные результаты запуска и
непокрытые случаи фиксируются в verification report, а не выводятся из config.

## Закреплённые версии

| Компонент | Версия | Где закреплён |
|---|---|---|
| TrustTunnel endpoint | 1.1.0 | Agent Dockerfile, SHA256 для amd64/arm64 |
| lego | 5.5.2 | Agent Dockerfile, SHA256 для amd64/arm64 |
| HAProxy | 3.2.5 | Agent Dockerfile, SHA256 исходников, сборка на bookworm |
| Mihomo lab client | 1.19.32 | Dockerfile.trusttunnel-client, SHA256 |
| TrustTunnel lab client | 1.1.5 | Dockerfile.trusttunnel-client, SHA256 |
| Pebble | 2.8.0 | Dockerfile.pebble, image digest |

Обновление версии требует проверки контракта, exporter fixtures и реального lab.

Результаты завершённой проверки и границы evidence:
[TrustTunnel verification](trusttunnel-verification.md).
