import copy
import json
import httpx
import pytest
NODE = '00000000-0000-4000-8000-000000000001'
PLAN = {'node_id': NODE, 'revision': 4, 'envelope_digest': 'a'*64,
        'enabled_protocols': ['amneziawg','trusttunnel'], 'bindings': [
        {'backend_role':'awg','public_port':45000,'transport':'udp'},
        {'backend_role':'trusttunnel','public_port':2443,'transport':'tcp'},
        {'backend_role':'acme-http','public_port':80,'transport':'tcp'}]}

@pytest.mark.asyncio
async def test_disable_filters_bindings_and_requires_validation():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    from awg_tui.resources import NodeResourcesScreen
    from textual.widgets import Button, SelectionList, Static
    calls=[]
    def respond(request):
        body=json.loads(request.content) if request.content else None
        calls.append((request.method,request.url.path,body))
        if request.url.path.endswith('/validate'):
            return httpx.Response(200,json={'valid':True,'digest':'b'*64,'revision':4,'issues':[]})
        return httpx.Response(200,json={'managed':True,'capabilities':{'installed_protocols':[
            {'adapter_id':'amneziawg','versions':['3.1']},{'adapter_id':'trusttunnel','versions':['1.0']}]},
            'desired':copy.deepcopy(PLAN),'actual':{'state':'READY','healthy':True,'applied_revision':3,'bindings':[]}})
    api=ControllerAPI('http://controller','admin',transport=httpx.MockTransport(respond))
    app=AWGApp(api,refresh_seconds=3600)
    app.load=lambda:None
    async with app.run_test(size=(140,50)) as pilot:
        await app.push_screen(NodeResourcesScreen(api,NODE,'de-1'))
        await pilot.pause()
        screen=app.screen
        assert 'READY' in str(screen.query_one('#resource-summary',Static).render())
        screen.query_one('#resource-protocols',SelectionList).deselect('trusttunnel')
        await pilot.pause()
        assert screen.query_one('#resource-apply',Button).disabled
        screen.query_one('#resource-validate',Button).press()
        await pilot.pause()
        assert not screen.query_one('#resource-apply',Button).disabled
        screen.query_one('#resource-apply',Button).press()
        await pilot.pause()
        submitted=next(body for method,_,body in calls if method=='PUT')
        assert submitted['expected_applied_revision']==3
        assert submitted['desired']['enabled_protocols']==['amneziawg']
        assert submitted['desired']['bindings']==[PLAN['bindings'][0]]
        assert screen.query_one('#resource-apply',Button).disabled

@pytest.mark.asyncio
@pytest.mark.parametrize('status',[200,404])
async def test_legacy_migration_no_apply(status):
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    from awg_tui.resources import NodeResourcesScreen
    from textual.widgets import Button, Static
    api=ControllerAPI('http://controller','admin',transport=httpx.MockTransport(
        lambda request:httpx.Response(status,json={'managed':False})))
    app=AWGApp(api,refresh_seconds=3600)
    app.load=lambda:None
    async with app.run_test(size=(140,50)) as pilot:
        await app.push_screen(NodeResourcesScreen(api,NODE,'legacy'))
        await pilot.pause()
        assert 'миграц' in str(app.screen.query_one('#resource-summary',Static).render()).lower()
        assert app.screen.query_one('#resource-validate',Button).disabled
        assert app.screen.query_one('#resource-apply',Button).disabled

@pytest.mark.asyncio
@pytest.mark.parametrize('reason,fragment',[('TRUSTTUNNEL_UNSUPPORTED','TrustTunnel'),('PORT_IN_USE','занят'),('UNSUPPORTED_NODE_API','миграц')])
async def test_reason_without_secret(reason,fragment):
    from awg_tui.api import ControllerAPI, APIError
    api=ControllerAPI('http://controller','admin',transport=httpx.MockTransport(
        lambda request:httpx.Response(503,json={'reason':reason,'message':'secret-password'})))
    try:
        with pytest.raises(APIError) as caught:
            await api.call('GET','/anything')
        assert fragment in str(caught.value)
        assert 'secret-password' not in str(caught.value)
    finally:
        await api.close()

@pytest.mark.asyncio
async def test_reenable_protocol_uses_authoritative_preview():
    from awg_tui.api import ControllerAPI
    from awg_tui.app import AWGApp
    from awg_tui.resources import NodeResourcesScreen
    from textual.widgets import Button, SelectionList
    previews=[]
    submitted=[]
    def respond(request):
        if request.url.path.endswith('/validate'):
            submitted.append(json.loads(request.content))
            return httpx.Response(200,json={'valid':True,'digest':'b'*64,'revision':5,'issues':[]})
        plan=copy.deepcopy(PLAN)
        if 'enabled_protocols' in request.url.params:
            previews.append(request.url.params['enabled_protocols'])
        else:
            plan['enabled_protocols']=['amneziawg']
            plan['bindings']=plan['bindings'][:1]
        return httpx.Response(200,json={'managed':True,'capabilities':{'installed_protocols':[
            {'adapter_id':'amneziawg','versions':['3.1']},{'adapter_id':'trusttunnel','versions':['1.0']}]},
            'desired':plan,'actual':{'state':'READY','applied_revision':3}})
    api=ControllerAPI('http://controller','admin',transport=httpx.MockTransport(respond))
    app=AWGApp(api,refresh_seconds=3600)
    app.load=lambda:None
    async with app.run_test(size=(140,50)) as pilot:
        await app.push_screen(NodeResourcesScreen(api,NODE,'de-1'))
        await pilot.pause()
        screen=app.screen
        protocols=screen.query_one('#resource-protocols',SelectionList)
        protocols.select('trusttunnel')
        await pilot.pause()
        screen.query_one('#resource-validate',Button).press()
        await pilot.pause()
        assert previews[-1]=='amneziawg,trusttunnel'
        assert submitted[-1]['bindings']==PLAN['bindings']
        protocols.deselect('trusttunnel')
        await pilot.pause()
        assert screen.query_one('#resource-apply',Button).disabled
