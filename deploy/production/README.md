# Production-манифесты

Пошаговая установка — [INSTALL.md](../../INSTALL.md). Установщики скачивают эти файлы сами.

| Файл | Где используется |
|---|---|
| `compose.yaml` | сервер панели (`install.sh`): `awg-db`, `controller`, `gateway`, `tui`, вспомогательные `pki-init`, `check-remnawave`. Подключается к Docker-сети стоковой панели как external. |
| `node.compose.yaml` | VPN-сервер (`install-node.sh`): Agent с AmneziaWG в собственной bridge-сети, только `NET_ADMIN` и `/dev/net/tun`. |

Что установщик меняет вне своего каталога: только `REMNAWAVE_PANEL_URL` стоковой subscription-page и,
по согласию, `WEBHOOK_*` панели — с записью прежних значений в `/opt/awg-extension/stock-changes`;
`uninstall.sh` возвращает именно эти ключи. Admin API Controller доступен только из внутренней сети
расширения. Резервное копирование — [operations](../../docs/operations.md): БД AWG, `/opt/awg-extension/.env`
(ключ шифрования), том `pki-ca`; на нодах — том `agent-state` и `/opt/awg-node/pki`.

Проверка всего сценария на изолированном стенде: `scripts/lab.sh installer`.
