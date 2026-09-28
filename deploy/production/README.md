# Установка рядом с существующим Remnawave

Remnawave остаётся полностью stock: панель, нода, subscription-page и их образы не меняются.
Расширение ставится рядом и подключается только через документированный API, webhooks и
переменные окружения stock-приложений.

## 1. Сервер с панелью Remnawave

Нужны Docker и Compose v2, запущенная панель (`remnawave/backend`) и API-токен
(Remnawave → Settings → API tokens).

```sh
git clone https://github.com/BekoSa/rw-amneziawg.git /opt/awg-src && cd /opt/awg-src
sudo ./install.sh
```

Установщик:
1. находит контейнер панели и её Docker-сеть, проверяет токен и показывает Internal Squads;
2. создаёт `/opt/awg-extension` (`.env` с секретами, права 600), собирает образ, генерирует PKI
   управления нодами, поднимает БД AWG, Controller и Gateway в сети панели;
3. **по вашему согласию** направляет stock subscription-page через Gateway — одна строка
   `REMNAWAVE_PANEL_URL=http://awg-gateway:8081` в её `.env`. Прежнее значение каждого изменённого ключа
   записывается в `/opt/awg-extension/stock-changes`; `uninstall.sh` возвращает именно эти ключи и не
   трогает ваши остальные правки. Перезапускается только этот сервис. Если subscription-page нет,
   установщик выводит правило reverse proxy для `/api/sub/*`;
4. **по вашему согласию** включает webhooks панели (добавляет URL расширения, существующие webhooks
   сохраняются). Без них изменения подхватываются опросом раз в 15 секунд;
5. создаёт команду `/opt/awg-extension/awg` — терминальное управление ([TUI](../../docs/tui.md)).
   Admin API Controller отвечает только из внутренней сети расширения, не из сети панели.

Неинтерактивно: `--api-token-file FILE --subpage yes|no --webhook yes|no --yes`.

## 2. VPN-сервер (AWG-нода)

В TUI: `Ноды → n` — получите команду. На VPN-сервере (Docker, `/dev/net/tun`):

```sh
git clone https://github.com/BekoSa/rw-amneziawg.git /opt/awg-src && cd /opt/awg-src
sudo ./install-node.sh --udp-ports 51820      # вставьте ключ awgnode1:… по запросу (или --secret-file FILE)
```

Agent работает рядом с Remnawave Node, в своём контейнере (bridge-сеть, только `NET_ADMIN` и
`/dev/net/tun`); AmneziaWG, маршруты и nftables живут только в namespace контейнера. Откройте в
firewall TCP 8443 (управление, mutual TLS — принимается только сертификат вашего Controller) и
UDP-порты профилей. Порты, опубликованные Docker, обходят UFW: ограничьте управление адресом панели
(`iptables -I DOCKER-USER -p tcp --dport 8443 ! -s <IP панели> -j DROP`) или `--management-bind <частный IP>`.
Затем в TUI — «Нода установлена — подключить».

## 3. Профиль и пользователи

TUI → `Профили → n`: нода, endpoint (адрес ноды для клиентов), Internal Squads, AmneziaWG 3.1 →
Сохранить → Проверить → Применить. Чтобы ключ AmneziaVPN (`vpn://`) был виден пользователям на странице
подписки, включите в Remnawave у конфига страницы подписки **Show connection keys** (по умолчанию stock
скрывает все ссылки). Дальше всё в Remnawave: пользователь в выбранном squad получает
AWG в подписке; отключение, истечение срока, лимит или удаление в Remnawave снимают доступ.

## Удаление и восстановление

```sh
sudo ./uninstall.sh            # вернуть .env subscription-page и панели, остановить расширение; данные сохраняются
sudo ./uninstall.sh --purge    # то же и удалить БД AWG и PKI
```

После `uninstall.sh` без `--purge` повторный `install.sh` поднимает те же ключи и peers. Резервное
копирование — [operations](../../docs/operations.md): БД AWG, `/opt/awg-extension/.env` (ключ
шифрования) и том `pki-ca`; на нодах — том `agent-state` и `/opt/awg-node/pki`.

## Файлы

| Файл | Назначение |
|---|---|
| `compose.yaml` | сторона панели: `awg-db`, `controller`, `gateway`, `tui`, вспомогательные `pki-init`, `check-remnawave` |
| `node.compose.yaml` | Agent на VPN-сервере |

Проверка всего сценария на изолированном стенде: `scripts/lab.sh installer`.
