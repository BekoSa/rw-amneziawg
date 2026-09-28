# Как проверить всё самому

Всё запускается в Docker; на машине нужны только Docker, Compose v2 и git. Сеть хоста проект не меняет —
каждая команда `scripts/lab.sh` сверяет маршруты/правила/интерфейсы хоста до и после и падает при расхождении.

## 1. Автотесты (≈ 20 минут, как в CI)

```sh
scripts/lab.sh build        # образы: tooling, services, agent (pinned AmneziaWG), клиент стенда
scripts/lab.sh lint         # запрет host network / privileged / опасных монтирований в compose
scripts/lab.sh test         # unit, contract, golden (форматы клиентов), БД, TUI — ожидается "N passed"
scripts/lab.sh e2e          # реальный туннель AWG 3.1 — ожидается строка "E2E: PASS"
scripts/lab.sh installer    # install.sh / install-node.sh / uninstall.sh — "INSTALLER: PASS"
```

Что именно проверяет E2E и установщик — [testing.md](testing.md). Логи и снимки сети хоста каждого прогона
лежат в `artifacts/run-*/`.

## 2. Посмотреть руками

```sh
scripts/lab.sh panel         # панель + страница подписки + расширение + нода
scripts/lab.sh credentials   # логин/пароль админа панели
scripts/lab.sh tui           # терминальное управление (в обычном терминале; q — выход)
```

| Что | Где | Что должно быть |
|---|---|---|
| Панель Remnawave | https://127.0.0.1:18443 | обычный stock-интерфейс (сертификат самоподписанный) |
| TUI → «Ноды» | `scripts/lab.sh tui` | `lab-node`, online, READY, версия ядра `amneziawg-go v3.1…` |
| Доступ | в панели: пользователь → squad «AWG E2E» | через несколько секунд он в TUI «Пользователи», «активен» |
| Страница подписки | https://127.0.0.1:18444/&lt;shortUuid&gt; (ссылка — в карточке пользователя) | ссылка «Lab \| AWG» (ключ AmneziaVPN `vpn://`), без заглушек «No hosts found» |
| Mihomo | `curl -sk -H 'user-agent: mihomo/1.19.31' https://127.0.0.1:18444/<shortUuid>` | прокси `type: wireguard`, `amnezia-wg-option.version: 3` |
| Отзыв доступа | отключить пользователя в панели | TUI: «отключён», AWG исчезает из подписки |
| Удаление расширения | `scripts/lab.sh e2e` (фазы removed/restored) | stock-подписка работает без расширения, после возврата — те же ключи |

В стенде нет настоящих Xray-нод, поэтому VLESS-ссылки — заглушки Remnawave; нода живёт во внутренней
Docker-сети, подключиться к ней с телефона нельзя — туннель проверяет клиент стенда в контейнере.

## 3. CI на GitHub

`.github/workflows/ci.yml` — на каждый push в `main` и pull request: сборка, lint, shellcheck, тесты,
E2E, проверка установщика; при push в `main` ещё публикует образы в GHCR (amd64 + arm64):

```
ghcr.io/bekosa/rw-amneziawg/extension:latest   ghcr.io/bekosa/rw-amneziawg/extension:sha-<short>
ghcr.io/bekosa/rw-amneziawg/agent:latest       ghcr.io/bekosa/rw-amneziawg/agent:sha-<short>
```

После первого пуша: Actions → CI должен стать зелёным; Packages → появятся `extension` и `agent`.
Если репозиторий приватный, пакеты тоже приватные — для установки с них нужен `docker login ghcr.io`.

Настройки репозитория (один раз): Settings → Actions → General → Workflow permissions →
**Read and write** и **Allow GitHub Actions to create and approve pull requests** (нужно для обновления ядра).

## 4. Обновление ядра AmneziaWG

Версии зафиксированы в одном месте — `ARG AWG_GO_*` / `ARG AWG_TOOLS_*` в `apps/node-agent/Dockerfile`
(тег и точный коммит). Их видно в TUI у каждой ноды.

**Автоматически:** `.github/workflows/amnezia-update.yml` каждый понедельник (или вручную: Actions →
AmneziaWG core update → Run workflow) проверяет новые релизы amneziawg-go / amneziawg-tools. Если они есть —
закрепляет их, прогоняет тесты и реальный E2E и открывает pull request с отчётом, какие параметры UAPI и
конфига появились или пропали upstream. Ничего не мёржится само.

**Вручную:**

```sh
scripts/update-amnezia.sh --check     # есть ли новые релизы (код выхода 10 — есть)
scripts/update-amnezia.sh             # закрепить последние теги и показать изменившиеся ключи
scripts/update-amnezia.sh --verify    # то же + сборка, тесты и E2E
```

Если отчёт показывает новые ключи (`> ключ`) — это новые возможности протокола: их надо добавить в
адаптер (`packages/awg-config`) и, если клиенты их поддерживают, в renderers подписок. Пропавшие ключи
(`< ключ`) — адаптер должен перестать их отправлять, иначе E2E упадёт.

**На серверах после мёржа:** ноды обновляются пересборкой/перетягиванием образа Agent —
`cd /opt/awg-node && docker compose --env-file .env pull awg-agent && docker compose --env-file .env up -d`
(или `build` вместо `pull`, если ставили из исходников после `git pull`). Ключи и журнал Agent хранятся в
томе, туннели восстанавливаются сами.

## 5. Пробная установка на реальные серверы

1. Сервер панели: `sudo ./install.sh` (или `--image-registry ghcr.io/bekosa/rw-amneziawg`, чтобы не собирать).
2. TUI → «Ноды» → `n` → на VPN-сервере `sudo ./install-node.sh` и вставить ключ.
3. TUI → «Профили» → `n` → нода, squad, AWG 3.1 → Сохранить → Проверить → Применить.
4. Пользователь в squad → в AmneziaVPN / Mihomo-клиенте / Throne обновить подписку → подключиться.
5. `/opt/awg-extension/awg peers` — handshake и трафик пользователя.
