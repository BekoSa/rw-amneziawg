"""Textual terminal UI: nodes, profiles (Save → Validate → Apply), users/peers, errors."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, Select, SelectionList, Static,
                             TabbedContent, TabPane, TextArea)

from awg_config import random_parameters

from .api import APIError, ControllerAPI
from .pki import node_bundle

NUMERIC = {'Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4'}
STATES = {'DISABLED': 'отключён', 'LIMITED': 'лимит трафика', 'EXPIRED': 'истёк срок'}


def peer_state(peer: dict) -> str:
    if peer.get('user_deleted'):
        return 'удаляется с ноды'
    if peer.get('present'):
        return 'активен'
    return STATES.get(peer.get('user_status'), 'нет доступа к профилю')


def human_bytes(value) -> str:
    size = float(value or 0)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return '0 B'


def when(value) -> str:
    if not value:
        return '—'
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime('%d.%m %H:%M:%S')
    except ValueError:
        return str(value)


def field(label: str, widget) -> Vertical:
    """A labelled form field: filled inputs stay recognisable (placeholders vanish once typed into)."""
    return Vertical(Label(label), widget, classes='field')


def parse_parameters(text: str) -> dict:
    """`Key = value` lines → protocol parameters (integers for junk/padding sizes)."""
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'строка {number}: ожидается «Параметр = значение»')
        key, value = (part.strip() for part in line.split('=', 1))
        if key in result:
            raise ValueError(f'параметр {key} указан дважды')
        result[key] = int(value) if key in NUMERIC else value
    return result


def format_parameters(parameters: dict) -> str:
    return '\n'.join(f'{key} = {value}' for key, value in parameters.items())


class ProfileScreen(ModalScreen[bool]):
    """Draft editor. Any edit invalidates validation; Apply needs a fresh Save + Validate."""

    BINDINGS = [Binding('escape', 'dismiss(False)', 'Закрыть')]

    def __init__(self, api: ControllerAPI, nodes: list[dict], squads: list[dict], profile: dict | None):
        super().__init__()
        self.api, self.nodes, self.squads, self.initial = api, nodes, squads, profile
        self.profile_id = (profile or {}).get('profile_id') or str(uuid4())
        self.saved = profile is not None
        self.digest = None

    def compose(self) -> ComposeResult:
        p = self.initial or {}
        net = p.get('network', {})
        proto = p.get('protocol', {'version': '3.1', 'parameters': random_parameters('3.1')})
        chosen = set(p.get('access', {}).get('squad_ids', []))
        squads = [(f'{s["name"]}  ·  {s["members"]} польз.', s['uuid'], s['uuid'] in chosen) for s in self.squads]
        with Vertical(classes='dialog'):
            yield Label('AWG-профиль · ' + (p['name'] if p else 'новый'), classes='title')
            with VerticalScroll(classes='form'):
                yield field('Название в подписке (видят пользователи)',
                            Input(p.get('name', ''), placeholder='например: Germany · AWG', id='name'))
                yield field('Нода', Select([(n['registration']['name'], n['registration']['node_id']) for n in self.nodes],
                                           prompt='выберите ноду', id='node', value=(p.get('node_ids') or [Select.NULL])[0]))
                with Horizontal(classes='row endpoint'):
                    yield field('Endpoint — адрес ноды для клиентов', Input(p.get('endpoint', {}).get('host', ''),
                                                                          placeholder='de.example.com', id='host'))
                    yield field('UDP-порт', Input(str(p.get('endpoint', {}).get('port', 51820)), id='port'))
                with Horizontal(classes='row'):
                    yield field('Пул адресов IPv4', Input(net.get('ipv4_pool', '10.8.0.0/24'), id='pool'))
                    yield field('IP сервера в пуле', Input(net.get('server_ipv4', '10.8.0.1'), id='server'))
                    yield field('MTU', Input(str(net.get('mtu', 1380)), id='mtu'))
                with Horizontal(classes='row'):
                    yield field('DNS для клиентов', Input(', '.join(p.get('dns_servers', ['1.1.1.1', '1.0.0.1'])), id='dns'))
                    yield field('AllowedIPs клиентов', Input(', '.join(net.get('client_allowed_ips', ['0.0.0.0/0'])), id='allowed'))
                yield field('Кто получает AWG — Internal Squads Remnawave (пробел — отметить)',
                            SelectionList(*squads, id='squads') if squads else
                            Static('В Remnawave нет Internal Squads — создайте группу в панели.', classes='hint'))
                with Horizontal(classes='version-row'):
                    yield field('Версия протокола', Select([('AmneziaWG 3.1', '3.1'), ('AmneziaWG 2', '2')],
                                                           value=proto.get('version', '3.1'), allow_blank=False, id='version'))
                    yield Button('Сгенерировать параметры', id='generate')
                yield field('Параметры протокола (Параметр = значение)',
                            TextArea(format_parameters(proto.get('parameters', {})), id='parameters'))
            yield Static('Сохранение не меняет работающий туннель: Сохранить → Проверить → Применить.', id='status')
            with Horizontal(classes='buttons'):
                yield Button('Закрыть', id='close')
                yield Button('Сохранить', id='save', variant='primary')
                yield Button('Проверить', id='validate', disabled=not self.saved)
                yield Button('Применить', id='apply', variant='success', disabled=True)

    def draft(self) -> dict:
        value = lambda widget_id: self.query_one(f'#{widget_id}', Input).value.strip()
        split = lambda text: [item.strip() for item in text.replace(';', ',').split(',') if item.strip()]
        node = self.query_one('#node', Select).value
        return {'profile_id': self.profile_id, 'name': value('name'), 'enabled': True,
                'endpoint': {'host': value('host'), 'port': int(value('port'))},
                'network': {'ipv4_pool': value('pool'), 'server_ipv4': value('server'), 'mtu': int(value('mtu')),
                            'client_allowed_ips': split(value('allowed'))},
                'protocol': {'adapter_id': 'amneziawg-go-v3', 'version': self.query_one('#version', Select).value,
                             'parameters': parse_parameters(self.query_one('#parameters', TextArea).text)},
                'dns_servers': split(value('dns')),
                'access': {'squad_ids': list(self.query_one('#squads', SelectionList).selected), 'user_ids': []},
                'node_ids': [] if node is Select.NULL else [node]}

    def status(self, text: str):
        self.query_one('#status', Static).update(text)

    def changed(self):
        self.saved, self.digest = False, None
        self.query_one('#validate', Button).disabled = True
        self.query_one('#apply', Button).disabled = True

    @on(Input.Changed)
    @on(TextArea.Changed)
    @on(SelectionList.SelectedChanged)
    def edited(self, event):
        if self.is_mounted and getattr(event, 'control', None) is not None and event.control.has_focus:
            self.changed()
            self.status('Черновик изменён: сохраните и проверьте заново.')

    @on(Select.Changed, '#version')
    @on(Select.Changed, '#node')
    def select_changed(self, event: Select.Changed):
        if event.select.has_focus:  # a user choice, not the initial/programmatic value
            self.changed()
            self.status('Черновик изменён: сохраните и проверьте заново.')

    @on(Button.Pressed, '#generate')
    def generate(self):
        version = self.query_one('#version', Select).value
        self.query_one('#parameters', TextArea).text = format_parameters(random_parameters(version))
        self.changed()
        self.status(f'Сгенерированы параметры AmneziaWG {version}.')

    @on(Button.Pressed, '#close')
    def close_screen(self):
        self.dismiss(False)

    @on(Button.Pressed, '#save')
    async def save(self):
        try:
            draft = self.draft()
            if len(draft['node_ids']) != 1:
                raise ValueError('выберите ноду')
            await self.api.save_profile(draft)
        except (ValueError, APIError) as error:
            self.status(f'[red]Не сохранено:[/] {error}')
            return
        self.saved = True
        self.query_one('#validate', Button).disabled = False
        self.status('Черновик сохранён. Работающий туннель не изменён — выполните проверку.')

    @on(Button.Pressed, '#validate')
    async def validate(self):
        try:
            result = await self.api.validate_profile(self.profile_id)
        except APIError as error:
            self.status(f'[red]Проверка не выполнена:[/] {error}')
            return
        issues = [f'{i["code"]}: {i["message"]}' for r in result.get('results', []) for i in r.get('issues', [])]
        if result.get('valid'):
            self.digest = result['draft_digest']
            self.query_one('#apply', Button).disabled = False
            self.status('[green]Проверка пройдена Agent.[/] Можно применять.')
        else:
            self.status('[red]Проверка не пройдена:[/] ' + ('; '.join(issues) or 'нода недоступна'))

    @on(Button.Pressed, '#apply')
    async def apply(self):
        try:
            await self.api.apply_profile(self.profile_id, self.digest)
        except APIError as error:
            self.status(f'[red]Не применено:[/] {error}')
            return
        self.dismiss(True)


class NodeKeyScreen(ModalScreen[None]):
    """Issue a node certificate and show the one-line installer command for the VPN server."""

    BINDINGS = [Binding('escape', 'dismiss(None)', 'Закрыть')]

    def __init__(self, api: ControllerAPI):
        super().__init__()
        self.api = api
        self.node_id = None

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog narrow'):
            yield Label('Новая AWG-нода', classes='title')
            with VerticalScroll(classes='form'):
                yield field('Название', Input(placeholder='например: de-1', id='name'))
                with Horizontal(classes='row endpoint'):
                    yield field('Адрес ноды для Controller (домен или IP)', Input(placeholder='de.example.com', id='address'))
                    yield field('Порт управления (TCP)', Input('8443', id='port'))
                yield field('Команда и ключ для VPN-сервера (выделите мышью, чтобы скопировать)',
                            TextArea('Нажмите «Выпустить ключ ноды».', read_only=True, id='command'))
            yield Static('Ключ показывается один раз и нигде не хранится.', id='status')
            with Horizontal(classes='buttons'):
                yield Button('Закрыть', id='close')
                yield Button('Выпустить ключ ноды', id='issue', variant='primary')
                yield Button('Нода установлена — подключить', id='register', variant='success', disabled=True)

    def status(self, text: str):
        self.query_one('#status', Static).update(text)

    @on(Button.Pressed, '#close')
    def close_screen(self):
        self.dismiss(None)

    @on(Button.Pressed, '#issue')
    def issue(self):
        ca_dir = Path(os.environ.get('AWG_PKI_CA_DIR', '/pki-ca'))
        controller_id = os.environ.get('AWG_CONTROLLER_ID', '')
        try:
            self.node_id = uuid4()
            bundle = node_bundle(ca_dir, controller_id, self.node_id)
        except (OSError, ValueError) as error:
            self.status(f'[red]Нет доступа к CA расширения:[/] {type(error).__name__}')
            return
        # The key is pasted at the installer prompt, never put on a command line (ps, shell history, sudo logs).
        self.query_one('#command', TextArea).text = (
            '# На VPN-сервере, из каталога проекта, выполните и вставьте ключ, когда установщик спросит:\n'
            'sudo ./install-node.sh --udp-ports 51820\n\n# Ключ ноды:\n' + bundle + '\n')
        self.query_one('#register', Button).disabled = False
        self.status('Скопируйте команду (выделите мышью). Ключ ноды показывается один раз и нигде не хранится.')

    @on(Button.Pressed, '#register')
    async def register(self):
        name = self.query_one('#name', Input).value.strip() or 'awg-node'
        address = self.query_one('#address', Input).value.strip()
        port = self.query_one('#port', Input).value.strip()
        if ':' in address and not address.startswith('['):
            address = f'[{address}]'  # IPv6 literal in a URL
        try:
            await self.api.register_node({'node_id': str(self.node_id), 'name': name,
                                          'management_url': f'https://{address}:{int(port)}', 'enabled': True})
        except (ValueError, APIError) as error:
            self.status(f'[red]Нода не подключена:[/] {error}. Проверьте, что Agent запущен и порт доступен.')
            return
        self.dismiss(None)


class AWGApp(App):
    TITLE = 'AmneziaWG · Remnawave extension'
    CSS = """
    ProfileScreen, NodeKeyScreen { align: center middle; background: $background 60%; }
    .dialog { width: 110; max-width: 96%; height: 94%; border: round $accent; background: $panel; }
    .dialog.narrow { width: 96; height: 32; max-height: 96%; }
    .dialog > .title { width: 1fr; padding: 1 2 0 2; text-style: bold; color: $accent; }
    .form { height: 1fr; padding: 1 2 0 2; }
    .field { height: auto; margin-bottom: 1; }
    .field > Label { color: $text-muted; padding-left: 1; }
    .row { height: auto; margin-bottom: 1; }
    .row > .field { width: 1fr; margin: 0 2 0 0; }
    .row > .field:last-of-type { margin-right: 0; }
    .row.endpoint > .field { width: 3fr; }
    .row.endpoint > .field:last-of-type { width: 1fr; }
    .version-row { height: auto; }
    .version-row > .field { width: 1fr; }
    #generate { margin: 1 0 0 2; min-width: 28; }
    #squads { height: auto; max-height: 8; }
    #parameters { height: 12; }
    #command { height: 8; }
    .hint { color: $warning; padding-left: 1; }
    #status { height: auto; min-height: 1; padding: 0 3; color: $text-muted; }
    .buttons { height: auto; align-horizontal: right; padding: 1 2; border-top: solid $primary 30%; }
    .buttons > Button { margin-left: 1; min-width: 14; height: 3; }
    #summary { padding: 1 2; }
    .tab-hint { color: $text-muted; padding: 0 1 1 1; }
    """
    BINDINGS = [Binding('r', 'refresh', 'Обновить'), Binding('n', 'new', 'Создать'), Binding('e', 'edit', 'Изменить'),
                Binding('k', 'rotate', 'Сменить ключ'), Binding('s', 'reconcile', 'Синхронизировать'),
                Binding('q', 'quit', 'Выход')]

    def __init__(self, api: ControllerAPI, refresh_seconds: float = 15):
        super().__init__()
        self.api, self.refresh_seconds = api, refresh_seconds
        self.data = {'nodes': [], 'profiles': [], 'peers': [], 'errors': []}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id='tabs'):
            with TabPane('Обзор', id='overview'):
                yield Static('Загрузка…', id='summary')
            with TabPane('Ноды', id='nodes'):
                yield Static('n — новая нода (выпустить ключ и команду установки)', classes='tab-hint')
                yield DataTable(id='nodes-table', cursor_type='row')
            with TabPane('Профили', id='profiles'):
                yield Static('n — создать профиль, e — изменить выбранный', classes='tab-hint')
                yield DataTable(id='profiles-table', cursor_type='row')
            with TabPane('Пользователи', id='peers'):
                yield Static('Доступ выдаётся в Remnawave через Internal Squads. k — сменить ключ выбранного пользователя.',
                             classes='tab-hint')
                yield Input(placeholder='Фильтр: имя, IP, состояние', id='peer-filter')
                yield DataTable(id='peers-table', cursor_type='row')
            with TabPane('Ошибки', id='errors'):
                yield DataTable(id='errors-table', cursor_type='row')
        yield Footer()

    def on_mount(self):
        self.query_one('#nodes-table', DataTable).add_columns('Нода', 'Agent', 'Состояние', 'Ревизия', 'Порт', 'Связь', 'Версия Agent / ядро AmneziaWG')
        self.query_one('#profiles-table', DataTable).add_columns('Профиль', 'AWG', 'Endpoint', 'Pool', 'Squads', 'Статус')
        self.query_one('#peers-table', DataTable).add_columns('Пользователь', 'Состояние', 'IP', 'Handshake', '↑ upload', '↓ download')
        self.query_one('#errors-table', DataTable).add_columns('Время', 'Код', 'Объект')
        self.load()
        self.set_interval(self.refresh_seconds, self.load)

    async def on_unmount(self):
        await self.api.close()  # same event loop that used the client

    @work(exclusive=True)
    async def load(self):
        try:
            for name in self.data:
                self.data[name] = await self.api.items(name)
        except APIError as error:
            self.notify(str(error), title='Нет данных', severity='error')
            return
        self.render_all()

    def render_all(self):
        nodes, profiles, peers = self.data['nodes'], self.data['profiles'], self.data['peers']
        ready = sum(1 for n in nodes if n['online'] and n['deployments'] and all(d['state'] == 'READY' for d in n['deployments']))
        active = sum(1 for p in peers if p['present'])
        traffic = sum(int(p['rx_total']) + int(p['tx_total']) for p in peers)
        self.query_one('#summary', Static).update(
            f'[b]Ноды READY:[/] {ready} / {len(nodes)}     [b]Профили:[/] {len(profiles)}     '
            f'[b]Активные пользователи AWG:[/] {active}     [b]Трафик AWG:[/] {human_bytes(traffic)}     '
            f'[b]Ошибки:[/] {len(self.data["errors"])}\n\n'
            'Доступ выдаётся в Remnawave: добавьте пользователя в Internal Squad, выбранный в профиле.\n'
            'Клавиши: r — обновить, n — создать, e — изменить, k — сменить ключ пользователя, s — синхронизировать.')
        table = self.query_one('#nodes-table', DataTable)
        table.clear()
        for node in nodes:
            deployments = node['deployments'] or [{}]
            for d in deployments:
                table.add_row(node['registration']['name'], 'online' if node['online'] else 'offline', d.get('state', '—'),
                              f'{d.get("applied_revision") or "—"} / {d.get("desired_revision") or "—"}',
                              str(d.get('listen_port') or '—'), when(node.get('last_seen_at')),
                              (node.get('capabilities') or {}).get('agent_version', '—'),
                              key=f'{node["registration"]["node_id"]}:{d.get("deployment_id", "")}')
        table = self.query_one('#profiles-table', DataTable)
        table.clear()
        for item in profiles:
            p = item['draft']
            status = 'применён' if item.get('active') == item['draft'] else ('проверен' if item.get('validated_digest') else 'черновик')
            table.add_row(p['name'], p['protocol']['version'], f'{p["endpoint"]["host"]}:{p["endpoint"]["port"]}',
                          p['network']['ipv4_pool'], str(len(p['access']['squad_ids'])), status, key=item['id'])
        self.render_peers()
        table = self.query_one('#errors-table', DataTable)
        table.clear()
        for error in self.data['errors']:
            table.add_row(when(error['created_at']), error['code'], str(error.get('entity_id') or '—'))

    @on(Input.Changed, '#peer-filter')
    def render_peers(self, _event=None):
        needle = self.query_one('#peer-filter', Input).value.lower()
        table = self.query_one('#peers-table', DataTable)
        table.clear()
        for peer in self.data['peers']:
            row = (peer.get('username') or peer['user_id'], peer_state(peer), peer['ipv4'], when(peer.get('latest_handshake')),
                   human_bytes(peer['rx_total']), human_bytes(peer['tx_total']))
            if needle in ' '.join(row).lower():
                table.add_row(*row, key=peer['id'])

    def active_tab(self) -> str:
        return self.query_one('#tabs', TabbedContent).active

    async def open_profile(self, profile: dict | None):
        try:
            squads = await self.api.squads()
        except APIError as error:
            self.notify(f'Squads Remnawave недоступны: {error}', severity='error')
            return
        def done(applied):
            if applied:
                self.notify('Apply принят. Нода получит ревизию; READY появится на вкладке «Ноды».', title='Профиль')
            self.load()
        await self.push_screen(ProfileScreen(self.api, self.data['nodes'], squads, profile), done)

    async def action_new(self):
        if self.active_tab() == 'nodes':
            await self.push_screen(NodeKeyScreen(self.api), lambda _: self.load())
        else:
            self.query_one('#tabs', TabbedContent).active = 'profiles'
            await self.open_profile(None)

    async def action_edit(self):
        table = self.query_one('#profiles-table', DataTable)
        if self.active_tab() != 'profiles' or not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        item = next(i for i in self.data['profiles'] if i['id'] == key)
        await self.open_profile(item['draft'])

    async def action_rotate(self):
        table = self.query_one('#peers-table', DataTable)
        if self.active_tab() != 'peers' or not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        peer = next(p for p in self.data['peers'] if p['id'] == key)
        try:
            await self.api.rotate_key(peer['user_id'])
        except APIError as error:
            self.notify(str(error), severity='error')
            return
        self.notify(f'Новый ключ для {peer.get("username")}: пользователю нужно обновить подписку.', title='Ключ')
        self.load()

    async def action_reconcile(self):
        try:
            await self.api.reconcile()
        except APIError as error:
            self.notify(str(error), severity='error')
            return
        self.notify('Синхронизация с Remnawave запущена.')

    def action_refresh(self):
        self.load()
