# TrustTunnel: результаты проверки 2026-10-12

Реализованы отдельные TrustTunnel contracts, encrypted Controller state, Agent
runtime, HTTP-01/TLS-ALPN-01/auto, node-local renewal, TUI и subscription delivery.
Extension использует публичные API/webhooks stock Remnawave; её данные хранятся
отдельно. Существующие изменения checkout
сохранены; коммиты и внешняя публикация не выполнялись.

Baseline до реализации: 202 passed / 8 network deselected; stock HTTP без extension
работал. Финальный aggregate включает новые TT проверки. Один warning — deprecation
Starlette TestClient/httpx; тесты прошли, runtime не затрагивается.

## Реальное исполнение

| Проверка | Результат | Evidence |
|---|---|---|
| Полные unit/contract/golden/DB/security/TUI tests | 371 passed, 14 network deselected, 30.84s | `artifacts/run-20261012T012017-75494/tests.log` |
| Agent + contracts после всех node fixes | 99 passed | `.superpowers/sdd/2026-10-09-trusttunnel/node-report.md` |
| Полная TrustTunnel network matrix | 5 passed, 141.66s, exit0 | `artifacts/trusttunnel-run-20261012T011711-73189/` |
| Stock HTTP до остановки extension / после остановки / после восстановления | 1 passed на каждой фазе, exit0 | `artifacts/stock-http-run-20261012T010840-62917/` |
| Shellcheck изменённых scripts и разбор CI YAML | PASS | Docker `network none`; CI publish зависит от TT tests |
| Независимый reviewer | Native/renewal/durability/provider findings устранены | `.superpowers/sdd/2026-10-09-trusttunnel/final-review.md` |

Network matrix использует настоящий Pebble, TrustTunnel endpoint1.1.0, официальный
TrustTunnel client1.1.5 и Mihomo1.19.32. Проверены issuance HTTP-01/TLS-ALPN-01,
auto fallback после реального HTTP503 и сохранение выбранного метода, fragmented
ClientHello, обычный traffic при активном ALPN solver, HTTP2 и QUIC, TCP/UDP payload,
wrong password, private-target denial, renewal served fingerprint, сохранение
сессии при renewal, прекращение старой сессии при rotation, durable revoke,
restart recovery и backoff после failed challenge.

Официальный клиент получает raw native export без добавления root на клиенте,
без native SSL_CERT_FILE override и с `skip_verification=false`. Test adaptation
заменяет только TUN listener на SOCKS. Нода перед upstream export дополняет цепочку
корневым CA из доверенного store после проверки issuer signatures, CA constraints
и validity; served certificate не меняется, временные export files приватны.

Renewal lab использует test-only `lego run --renew-force`, чтобы не ждать ARI window
свежего сертификата. Production CLI и ARI policy не изменены. Тестовый Pebble CA
доверен явно только в isolated test runtime; staging не становится production READY.

## Safety и границы доказательства

В final TT, aggregate и stock HTTP runs сравнение host VPN/interfaces/routes/policy
rules прошло. Firewall comparison недоступен из-за read permissions/tool: firewall
не сертифицирован этим запуском. Все lab runtime выполнялись в project-owned
internal Docker bridges; client не подключён к target network. Host networking,
VPN, TUN, modules, sysctl, systemd и project dependencies в host OS не настраивались.

На development-host отсутствует `/dev/net/tun`. Полный production entrypoint с AWG,
реальный AWG full-tunnel и полный installer/removal E2E требуют prepared isolated
runner. Здесь доказаны TT proxy connectivity и stock HTTP removal/restoration;
они не подменяют AWG tunnel/installer tests.

Production Let's Encrypt issuance на публичном domain не выполнялась: нужны
операторские DNS/ingress и smoke. Pebble подтверждает реальный ACME flow и runtime
connectivity, но не доступность конкретной production-ноды извне.

Один TT deployment на ноду. TT traffic пока не включён в AWG ledger/combined quota.
Поддержка TT не заявлена для неизвестного embedded core, Throne и INCY; отображение
native download buttons stock subscription page зависит от её версии.

## История исправлений

Помимо реализации исправлены uninstall TSV с пустыми fields, atomic enrollment
publish, Controller stale-cache revocation race, provider no-transform/conditional/
encoded gate, renewal fallback на last-served certificate, fsync directory ancestry,
lego stdout diagnostic capture и native chain trust-store incompatibility.

Предыдущий full raw run `011156` содержал отказ TLS-ALPN forced renewal; его причина
не была установлена по старому assertion. Narrow TLS run `011533` и итоговый общий
run `011711` прошли. Production retry policy ради этого не ослаблялась; failed logs
сохранены. Ранние harness ошибки и root-append native tests не выдаются за итоговое
доказательство raw-client compatibility.

## Воспроизведение

```sh
scripts/lab.sh build
scripts/lab.sh test -q -m 'not network'
scripts/trusttunnel-lab.sh build
scripts/trusttunnel-lab.sh lint
scripts/trusttunnel-lab.sh test
scripts/stock-http-smoke.sh
```

Docker lab операции сериализуются lock; cleanup сохраняет state volumes и evidence.
Инструкция установки и эксплуатации: [TrustTunnel](trusttunnel.md).
