"""TrustTunnel draft editor. Only the authenticated Controller API is used."""
from uuid import uuid4

from awg_contracts.trusttunnel import TrustTunnelExportResponse, TrustTunnelProfile
from pydantic import ValidationError
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, SelectionList, Static, TextArea

from .api import APIError, ControllerAPI


def field(label, control):
    return Vertical(Label(label), control, classes='field')


class TrustTunnelProfileScreen(ModalScreen[bool]):
    BINDINGS = [Binding('escape', 'dismiss(False)', 'Закрыть')]

    def __init__(self, api: ControllerAPI, nodes, squads, profile=None):
        super().__init__()
        self.api, self.nodes, self.squads = api, nodes, squads
        self.initial = profile or {}
        self.profile_id = self.initial.get('profile_id') or str(uuid4())
        self.saved = profile is not None
        self.digest = None
        self.edit_revision = 0

    def compose(self) -> ComposeResult:
        p = self.initial
        tls, endpoint = p.get('tls', {}), p.get('endpoint', {})
        node_ids = p.get('node_ids') or []
        selected = node_ids[0] if node_ids else (self.nodes[0]['registration']['node_id'] if len(self.nodes) == 1 else Select.NULL)
        chosen = p.get('access', {}).get('squad_ids', [])
        squads = [(f'{s["name"]} · {s["members"]} польз.', s['uuid'], s['uuid'] in chosen) for s in self.squads]
        with Vertical(classes='dialog'):
            yield Label('TrustTunnel · ' + p.get('name', 'новый профиль'), classes='title')
            with VerticalScroll(classes='form'):
                yield field('Название в подписке', Input(p.get('name', ''), id='name'))
                yield field('Одна нода (требуется capability trusttunnel 1.0)',
                            Select([(n['registration']['name'], n['registration']['node_id']) for n in self.nodes],
                                   value=selected, prompt='выберите ноду', id='node'))
                with Horizontal(classes='row endpoint'):
                    yield field('Public endpoint — домен или IP для клиентов',
                                Input(endpoint.get('host', ''), placeholder='vpn.example.com', id='host'))
                    yield field('TCP/UDP data port', Input(str(endpoint.get('port', 443)), id='port'))
                yield field('Домен TLS сертификата (DNS)', Input(tls.get('domain', ''), placeholder='vpn.example.com', id='domain'))
                yield field("Email для Let's Encrypt", Input(tls.get('email', ''), placeholder='vpn@example.com', id='email'))
                with Horizontal(classes='row'):
                    yield field('ACME challenge', Select([('Auto (HTTP-01 → TLS-ALPN-01)', 'auto'),
                        ('HTTP-01 · public TCP 80', 'http-01'), ('TLS-ALPN-01 · public TCP 443', 'tls-alpn-01')],
                        value=tls.get('challenge', 'auto'), allow_blank=False, id='challenge'))
                    yield field('ACME environment', Select([('Production', 'production'), ('Staging (тестовый сертификат)', 'staging')],
                        value=tls.get('environment', 'production'), allow_blank=False, id='environment'))
                yield Static('Challenge использует public 80/443 независимо от data port. Staging не даёт READY.', classes='hint')
                yield field('Internal Squads Remnawave — кому выдаётся TrustTunnel',
                            SelectionList(*squads, id='squads'))
            yield Static('Сохранить → Проверить → Применить. Сохранение не меняет runtime.', id='status', markup=False)
            with Horizontal(classes='buttons'):
                yield Button('Закрыть', id='close')
                yield Button('Сохранить', id='save', variant='primary')
                yield Button('Проверить', id='validate', disabled=not self.saved)
                yield Button('Применить', id='apply', variant='success', disabled=True)

    def draft(self):
        value = lambda key: self.query_one('#' + key, Input).value.strip()
        node = self.query_one('#node', Select).value
        if node is Select.NULL:
            raise ValueError('выберите одну ноду')
        return TrustTunnelProfile.model_validate({
            'profile_id': self.profile_id, 'name': value('name'), 'enabled': self.initial.get('enabled', True),
            'endpoint': {'host': value('host'), 'port': int(value('port'))},
            'node_ids': [node], 'access': {'squad_ids': list(self.query_one('#squads', SelectionList).selected),
                                         'user_ids': self.initial.get('access', {}).get('user_ids', [])},
            'tls': {'domain': value('domain'), 'email': value('email'),
                    'challenge': self.query_one('#challenge', Select).value,
                    'environment': self.query_one('#environment', Select).value},
        }).model_dump(mode='json')

    def status(self, text):
        self.query_one('#status', Static).update(text)

    @on(Input.Changed)
    @on(Select.Changed)
    @on(SelectionList.SelectedChanged)
    def edited(self, event):
        control = getattr(event, 'control', None)
        if self.is_mounted and control is not None and control.has_focus:
            self.edit_revision += 1
            self.saved, self.digest = False, None
            self.query_one('#validate', Button).disabled = True
            self.query_one('#apply', Button).disabled = True
            self.status('Черновик изменён: сохраните и проверьте заново.')

    @on(Button.Pressed, '#close')
    def close_screen(self):
        self.dismiss(False)

    @on(Button.Pressed, '#save')
    async def save(self):
        self.digest = None
        self.query_one('#apply', Button).disabled = True
        revision = self.edit_revision
        try:
            await self.api.save_trusttunnel_profile(self.draft())
        except ValidationError as error:
            fields = ', '.join('.'.join(str(part) for part in e['loc']) for e in error.errors(include_input=False))
            self.status('Не сохранено: проверьте поля ' + fields)
            return
        except (ValueError, APIError) as error:
            self.status('Не сохранено: ' + str(error))
            return
        if revision != self.edit_revision:
            return
        self.saved = True
        self.query_one('#validate', Button).disabled = False
        self.status('Черновик сохранён. Работающий туннель не изменён — выполните проверку.')

    @on(Button.Pressed, '#validate')
    async def validate(self):
        if not self.saved:
            return
        self.digest = None
        self.query_one('#apply', Button).disabled = True
        revision = self.edit_revision
        try:
            result = await self.api.validate_trusttunnel_profile(self.profile_id)
        except APIError as error:
            self.status('Проверка не выполнена: ' + str(error))
            return
        if revision != self.edit_revision:
            return
        if result.get('valid') and result.get('draft_digest'):
            self.digest = result['draft_digest']
            self.query_one('#apply', Button).disabled = False
            self.status('Проверка пройдена Agent. Можно применять.')
        else:
            codes = [i['code'] for r in result.get('results', []) for i in r.get('issues', [])]
            self.status('Проверка не пройдена: ' + ('; '.join(codes) or 'нода недоступна'))

    @on(Button.Pressed, '#apply')
    async def apply(self):
        if not self.saved or self.digest is None:
            return
        try:
            await self.api.apply_trusttunnel_profile(self.profile_id, self.digest)
        except APIError as error:
            self.status('Не применено: ' + str(error))
            return
        self.dismiss(True)


class TrustTunnelExportScreen(ModalScreen[None]):
    """Credential-bearing output is shown only after an explicit export request."""
    BINDINGS = [Binding('escape', 'close_export', 'Закрыть')]

    def __init__(self, api: ControllerAPI, peer_id: str):
        super().__init__()
        self.api, self.peer_id = api, peer_id
        self.exported_format = None

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Label('TrustTunnel · native export', classes='title')
            with VerticalScroll(classes='form'):
                yield Static('Экспорт содержит credentials пользователя. Показывается только по нажатию «Экспортировать».', markup=False)
                yield field('Формат', Select([('Native deep-link', 'deeplink'), ('CLI TOML', 'toml')],
                                            value='deeplink', allow_blank=False, id='format'))
                yield field('Выделите результат мышью, чтобы скопировать',
                            TextArea('', read_only=True, id='export-content'))
            yield Static('Выберите формат и нажмите «Экспортировать».', id='status', markup=False)
            with Horizontal(classes='buttons'):
                yield Button('Закрыть', id='close')
                yield Button('Экспортировать', id='export', variant='primary')

    @on(Select.Changed, '#format')
    def clear_export(self, event: Select.Changed):
        if (self.is_mounted and event.value == self.query_one('#format', Select).value
                and event.value != self.exported_format):
            self.query_one('#export-content', TextArea).text = ''

    @on(Button.Pressed, '#export')
    async def export(self):
        area = self.query_one('#export-content', TextArea)
        area.text = ''
        export_format = self.query_one('#format', Select).value
        status = self.query_one('#status', Static)
        try:
            result = TrustTunnelExportResponse.model_validate(
                await self.api.export_trusttunnel_peer(self.peer_id, export_format))
        except APIError as error:
            status.update('Экспорт не выполнен: ' + str(error))
            return
        except ValidationError:
            status.update('Экспорт не выполнен: некорректный ответ Controller')
            return
        if str(result.peer_id) != self.peer_id or result.format != export_format:
            status.update('Экспорт не выполнен: ответ не соответствует запросу')
            return
        if export_format != self.query_one('#format', Select).value:
            return
        self.exported_format = result.format
        area.text = result.content.get_secret_value()
        status.update('Экспорт получен. Закройте окно после копирования.')

    @on(Button.Pressed, '#close')
    def action_close_export(self):
        self.query_one('#export-content', TextArea).text = ''
        self.dismiss(None)
