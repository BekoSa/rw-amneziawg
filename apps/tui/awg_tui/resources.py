"""Node resource controls; profile endpoints remain the source of listener allocations."""
from copy import deepcopy

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, SelectionList, Static

from .api import APIError, ControllerAPI

MIGRATION = ('Требуется одноразовая миграция ноды обновлённым install-node.sh --managed. '
             'Обычный pull образа не добавляет ingress. Существующие AWG/TT профили сохраняются.')
NAMES = {'amneziawg': 'AmneziaWG', 'trusttunnel': 'TrustTunnel'}


class NodeResourcesScreen(ModalScreen[bool]):
    BINDINGS = [Binding('escape', 'dismiss(False)', 'Закрыть')]

    def __init__(self, api: ControllerAPI, node_id: str, name: str):
        super().__init__()
        self.api, self.node_id, self.node_name = api, node_id, name
        self.resources = {}
        self.validated_plan = None
        self.edit_revision = 0
        self.loading = True
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Label('Управление нодой · ' + self.node_name, classes='title')
            with VerticalScroll(classes='form'):
                yield Static('Загрузка…', id='resource-summary', markup=False)
                yield Label('Установленные протоколы · выберите включённые')
                yield SelectionList(id='resource-protocols')
                yield Static('Порты редактируются в AWG/TT профилях. Выключение протокола снимает его слушатели '
                             'и прерывает подключения. Внешний firewall и DNS настраиваются отдельно.', classes='hint')
                yield Static('', id='resource-bindings', markup=False)
            yield Static('Проверить → Применить', id='resource-status', markup=False)
            with Horizontal(classes='buttons'):
                yield Button('Закрыть', id='resource-close')
                yield Button('Обновить', id='resource-refresh')
                yield Button('Проверить', id='resource-validate', disabled=True)
                yield Button('Применить', id='resource-apply', disabled=True, variant='success')

    async def on_mount(self):
        await self.refresh_resources()

    async def refresh_resources(self):
        self.loading = True
        self.validated_plan = None
        self.query_one('#resource-apply', Button).disabled = True
        self.query_one('#resource-validate', Button).disabled = True
        controls = self.query_one('#resource-protocols', SelectionList)
        controls.clear_options()
        try:
            resources = await self.api.node_resources(self.node_id)
            capabilities = await self.api.node_resource_capabilities(self.node_id) if resources.get('managed') else {}
        except APIError as error:
            self.query_one('#resource-summary', Static).update(
                MIGRATION if error.status_code == 404 else str(error))
            self.loading = False
            return
        self.resources = resources
        if not resources.get('managed'):
            self.query_one('#resource-summary', Static).update(MIGRATION)
            self.loading = False
            return
        caps = capabilities.get('capabilities') or resources.get('capabilities') or {}
        desired, actual = resources.get('desired') or {}, resources.get('actual') or {}
        enabled = desired.get('enabled_protocols', [])
        installed = caps.get('installed_protocols') or []
        for adapter in installed:
            protocol = adapter['adapter_id']
            if protocol in NAMES:
                controls.add_option((NAMES[protocol] + ' · ' + ', '.join(adapter.get('versions', [])),
                                     protocol, protocol in enabled))
        issue = (actual.get('error') or {}).get('code') or resources.get('last_error_code') or '—'
        self.query_one('#resource-summary', Static).update(
            f'Состояние: {actual.get("state", "UNKNOWN")} · backend health: '
            f'{"healthy" if actual.get("healthy") else "не подтверждён"}\n'
            f'Ревизии desired / accepted / applied / LKG: {desired.get("revision", "—")} / '
            f'{actual.get("accepted_revision") or "—"} / {actual.get("applied_revision") or "—"} / '
            f'{actual.get("last_known_good_revision") or "—"}\n'
            f'Dynamic ingress: {"да" if caps.get("supports_dynamic_ingress") else "не подтверждён"} · ошибка: {issue}\n'
            'Внешняя connectivity: UNKNOWN, если не подтверждена отдельной проверкой.')
        observed = actual.get('bindings') or []
        rows = ['Фактические слушатели:']
        for item in observed:
            binding = item.get('binding') or {}
            rows.append(f'{binding.get("transport", "?").upper()} {binding.get("public_address", "*")}:'
                        f'{binding.get("public_port", "?")} · {binding.get("backend_role", "?")} · '
                        f'{item.get("state", "UNKNOWN")} · backend '
                        f'{"ready" if item.get("backend_ready") else "не готов"}'
                        + (' · ' + item['error']['code'] if item.get('error') else ''))
        self.query_one('#resource-bindings', Static).update('\n'.join(rows))
        self.query_one('#resource-validate', Button).disabled = not bool(desired)
        self.loading = False
        self.render_plan()

    def plan(self):
        desired = deepcopy(self.resources.get('desired') or {})
        desired['enabled_protocols'] = list(self.query_one('#resource-protocols', SelectionList).selected)
        enabled = set(desired['enabled_protocols'])
        desired['bindings'] = [binding for binding in desired.get('bindings', [])
            if ('amneziawg' if binding['backend_role'] == 'awg' else 'trusttunnel') in enabled]
        return desired

    def render_plan(self):
        if not self.resources.get('managed'):
            return
        bindings = self.plan().get('bindings', [])
        text = '\n'.join(f'{b["transport"].upper()} {b.get("public_address", "*")}:{b["public_port"]} · '
                         f'{b["backend_role"]}' for b in bindings) or 'нет слушателей'
        self.query_one('#resource-status', Static).update('План после применения (из профилей):\n' + text)

    @on(SelectionList.SelectedChanged, '#resource-protocols')
    async def edited(self):
        if not self.loading:
            self.edit_revision += 1
            edit_revision = self.edit_revision
            self.validated_plan = None
            self.query_one('#resource-apply', Button).disabled = True
            self.query_one('#resource-validate', Button).disabled = True
            self.render_plan()
            try:
                preview = await self.api.node_resources(self.node_id,
                    list(self.query_one('#resource-protocols', SelectionList).selected))
                if edit_revision == self.edit_revision:
                    self.resources['desired'] = preview.get('desired')
                    self.query_one('#resource-validate', Button).disabled = not bool(preview.get('desired'))
                    self.render_plan()
            except APIError as error:
                self.query_one('#resource-status', Static).update(str(error))

    @on(Button.Pressed, '#resource-close')
    def close_screen(self):
        self.dismiss(False)

    @on(Button.Pressed, '#resource-refresh')
    async def reload(self):
        if not self.busy:
            await self.refresh_resources()

    @on(Button.Pressed, '#resource-validate')
    async def validate(self):
        if self.busy:
            return
        self.busy = True
        plan, edit_revision = self.plan(), self.edit_revision
        self.validated_plan = None
        self.query_one('#resource-apply', Button).disabled = True
        try:
            result = await self.api.validate_node_resources(self.node_id, plan)
            if result.get('valid') and edit_revision == self.edit_revision:
                self.validated_plan = plan
                self.query_one('#resource-apply', Button).disabled = False
                text = 'Проверка пройдена. Apply повторно проверит занятость портов.'
            else:
                text = 'Проверка не пройдена: ' + ', '.join(issue['code'] for issue in result.get('issues', []))
            self.query_one('#resource-status', Static).update(text)
        except APIError as error:
            self.query_one('#resource-status', Static).update(str(error))
        finally:
            self.busy = False

    @on(Button.Pressed, '#resource-apply')
    async def apply(self):
        if self.busy or self.validated_plan is None or self.validated_plan != self.plan():
            return
        self.busy = True
        self.query_one('#resource-apply', Button).disabled = True
        try:
            await self.api.apply_node_resources(self.node_id, self.validated_plan,
                (self.resources.get('actual') or {}).get('applied_revision'))
            await self.refresh_resources()
        except APIError as error:
            self.query_one('#resource-status', Static).update(str(error))
        finally:
            self.validated_plan = None
            self.busy = False
