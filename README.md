# rw-amneziawg

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

Независимое расширение AmneziaWG (до версии 3.1) для stock Remnawave: Controller, Node Agent,
Subscription Gateway и терминальное управление (TUI). Пользователи и доступ модерируются в Remnawave
через Internal Squads; AWG автоматически появляется в подписках (AmneziaVPN, Mihomo-клиенты, Throne,
INCY). Все приложения Remnawave остаются stock.

**Установка на работающий Remnawave — [INSTALL.md](INSTALL.md):** на сервере панели и на VPN-сервере
скачивается по одному скрипту, клонировать проект не нужно.

Проект находится в разработке. Полная готовность определяется проверками из
[карты приёмки](docs/acceptance.md), включая реальный AWG payload в изолированном Docker lab,
removal/restore и независимый review. Наличие исходников не означает прохождения этих проверок.

**Как проверить всё самому, CI и обновление ядра AmneziaWG:** [docs/verification.md](docs/verification.md).

## Документация

- [Требования](docs/MASTER_PROMPT.md)
- [Архитектура и контракты](docs/architecture.md)
- [План реализации](docs/implementation-plan.md)
- [Docker lab и проверки](docs/testing.md)
- [Controller](docs/controller.md), [Agent](docs/agent.md)
- [Подписки и форматы клиентов](docs/subscriptions.md), [TUI](docs/tui.md)
- [Backup, restore и удаление](docs/operations.md), [установка](INSTALL.md)

## Разработка

На хосте нужны Docker Engine, Docker Compose и Git. Зависимости Python, PostgreSQL,
Remnawave, AWG и инструменты тестирования устанавливаются и запускаются только в контейнерах.

```sh
scripts/lab.sh build          # все образы
scripts/lab.sh lint           # safety lint compose-манифестов
scripts/lab.sh test           # unit / contract / golden / DB
scripts/lab.sh e2e            # реальный AWG-туннель против stock Remnawave 3.4.4
scripts/lab.sh installer      # install.sh / install-node.sh / uninstall.sh против stock-стенда
scripts/lab.sh tui            # терминальное управление
```

Фазы E2E, топология, переменные и непокрытые сценарии — в [инструкции lab](docs/testing.md).
Network-тесты требуют уже доступного `/dev/net/tun`; проект не создаёт его на development
host и не загружает модули ядра.

## Границы безопасности

Расширение использует документированные API/webhooks Remnawave. Оно не читает напрямую
PostgreSQL Remnawave, не меняет stock backend/Node/Xray и не добавляет туда migrations.

Development manifests используют Docker bridge. VPN, маршруты, policy rules, firewall,
TUN-интерфейсы и systemd services хоста не настраиваются проектом. Full-tunnel возможен
только внутри изолированного test-client container.

Client private keys зашифрованы в отдельной AWG PostgreSQL. Server private key остаётся
в persistent volume Agent. Management API требует взаимной аутентификации. Неизвестный
клиент либо ошибка enrichment оставляют stock subscription без AWG.

Отключение gateway возвращает subscription traffic непосредственно stock Remnawave.
Остановка extension не должна удалять AWG database и Agent keys: удаление volumes —
отдельное явное действие, не часть обычного uninstall.
