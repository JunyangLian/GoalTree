"""Attachment scope and actual collaboration gates; no external model requests."""
import base64
import io
import json
import threading

import pytest

from pico.conversation_assets import ConversationAssets, cooperation
from pico.cooperation import checkpoints
from pico.providers.clients import FakeModelClient
from pico.web import Workbench, RequestError, make_server


def encoded(text):
    return base64.b64encode(text if isinstance(text, bytes) else text.encode()).decode()


def bound(app):
    return {'session_id': app.goals.binding('', '', 'assistant')['session_id'], 'goal_id': ''}


def complete(app, body, allow=True):
    task = app.task(app.submit(body)['id'])
    approvals = []
    while True:
        with task.condition:
            assert task.condition.wait_for(lambda: task.finished is not None or task.approval is not None, 30)
            if task.finished is not None:
                break
            id = task.approval['id']
            if id not in [a['id'] for a in approvals]:
                approvals.append(dict(task.approval))
                app.approve(task.id, id, allow(task.approval) if callable(allow) else allow)
            # Wait for resolution; do not double-submit an existing approval.
            assert task.condition.wait_for(lambda: task.approval is None or task.approval['id'] != id or task.finished is not None, 30)
    return task, approvals


def tool(name, args):
    return '<tool>' + json.dumps({'name': name, 'args': args}) + '</tool>'


def planner_outputs():
    return [tool('goal_action', {'proposal': json.dumps({'action': 'create_goal', 'title': 'A scoped objective'})}),
            tool('plan_tasks', {'steps': json.dumps([
                {'id': 'stage', 'title': 'Choose approach', 'verification_mode': 'manual'},
                {'id': 'detail', 'parent_id': 'stage', 'title': 'Review assumptions', 'verification_mode': 'manual'}])}),
            '<final>Saved the stage and its child; did not execute implementation.</final>']


@pytest.mark.parametrize('level,expected', [('step', 3), ('balanced', 2), ('autonomous', 0)])
def test_planning_levels_enforce_real_checkpoints(tmp_path, level, expected):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(planner_outputs()))
    task, approvals = complete(app, {'mode': 'assistant', 'prompt': 'Plan this project', 'policy': 'auto',
        'cooperation': {'planning': level, 'implementation': 'balanced'}, 'max_steps': 6})
    assert task.status == 'completed', task.error
    assert task.policy == 'ask'  # User autonomy did not become safety auto-approval.
    assert len(approvals) == expected
    assert all(a['kind'].startswith('planning_') for a in approvals)
    assert len(app.goals.read()['tasks']) == 2
    assert all(not t['runs'] for t in app.goals.read()['tasks'].values())
    assert any(e['event'] == 'cooperation_configured' for e in task.events)
    assert task.view()['cooperation']['planning'] == level


def test_declined_planning_step_has_no_side_effect(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([
        planner_outputs()[0], '<final>The proposed goal was declined; no goal was created.</final>']))
    task, approvals = complete(app, {'mode': 'assistant', 'prompt': 'Plan',
        'cooperation': {'planning': 'step', 'implementation': 'autonomous'}}, allow=False)
    assert len(approvals) == 1 and not app.goals.read()['goals']
    assert any(e.get('tool_error_code') == 'collaboration_denied' for e in task.events)


def test_balanced_details_do_not_repeat_direction_approval():
    context = {'existing_tasks': [{'id': 'a', 'title': 'Stage A', 'parent_id': ''}]}
    settings = {'planning': 'balanced', 'implementation': 'autonomous'}
    assert not checkpoints(settings, 'assistant', 'plan_tasks', {'steps': json.dumps([
        {'id': 'b', 'parent_id': 'a', 'title': 'Routine detail'}])}, False, context)
    assert checkpoints(settings, 'assistant', 'plan_tasks', {'steps': json.dumps([
        {'id': 'a', 'title': 'Different direction'}])}, False, context)


@pytest.mark.parametrize('level,read_checkpoints', [('step', 1), ('balanced', 0), ('autonomous', 0)])
def test_implementation_preferences_never_bypass_safety(tmp_path, level, read_checkpoints):
    (tmp_path/'source.txt').write_text('source', encoding='utf-8')
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([
        tool('read_file', {'path': 'source.txt'}), tool('write_file', {'path': 'output.txt', 'content': 'actual output'}), '<final>Written.</final>']))
    goal = app.goals.action({'action': 'create_goal', 'title': 'Implement'})
    item = app.goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': 'Work', 'verification_mode': 'manual'})
    task, approvals = complete(app, {'goal_id': goal['id'], 'node_id': item['id'], 'mode': 'execute', 'prompt': 'Read then write',
        'cooperation': {'planning': 'autonomous', 'implementation': level}, 'policy': 'auto', 'max_steps': 6})
    assert task.status == 'completed', task.error
    assert len([a for a in approvals if a['kind'] == 'implementation_step']) == read_checkpoints
    assert len([a for a in approvals if a['kind'] == 'safety' and a['name'] == 'write_file']) == 1
    assert (tmp_path/'output.txt').read_text() == 'actual output'


def test_preferences_persist_per_conversation_not_across_goals(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([]))
    scope = bound(app)
    settings = {'planning': 'step', 'implementation': 'autonomous'}
    assert app.set_cooperation({**scope, 'cooperation': settings}) == settings
    app.tree_action({'action': 'new_conversation'})
    other = bound(app)
    assert other != scope
    assert app.tree_context('', mode='assistant')['cooperation'] == {'planning': 'balanced', 'implementation': 'balanced'}
    assert ConversationAssets(tmp_path).preferences(scope['session_id']) == settings
    with pytest.raises(RequestError, match='不匹配'):
        app.set_cooperation({**scope, 'cooperation': settings})
    for invalid in [{}, {'planning': 'auto', 'implementation': 'step'}, {'planning': [], 'implementation': 'step'}]:
        with pytest.raises(ValueError): cooperation(invalid)


def test_text_upload_is_bounded_private_and_scoped(tmp_path):
    assets = ConversationAssets(tmp_path)
    result = assets.upload('session-a', 'requirements.md', encoded('# Fish genomics\nNo model training.'))
    assert result['mime'] == 'text/plain' and result['sha256']
    assert assets.get('session-a', [result['id']])[0]['extracted'].startswith('# Fish')
    with pytest.raises(ValueError, match='当前对话'): assets.get('session-b', [result['id']])
    with pytest.raises(ValueError): assets.get('session-a', [{}])
    with pytest.raises(ValueError): assets.upload('session-a', '../escape.md', encoded('x'))
    with pytest.raises(ValueError): assets.upload('session-a', '.env', encoded('secret'))
    with pytest.raises(ValueError): assets.upload('session-a', 'unknown.exe', encoded('x'))
    with pytest.raises(ValueError): assets.upload('session-a', 'binary.txt', encoded(b'\0x'))
    with pytest.raises(ValueError): assets.upload('session-a', 'too-long.txt', encoded('x'*60001))
    with pytest.raises(ValueError): assets.upload('session-a', 'x.txt', 'invalid base64')
    assert not (tmp_path/'requirements.md').exists()  # No arbitrary workspace file write.


def test_image_normalization_and_non_vision_rejection(tmp_path):
    Image = pytest.importorskip('PIL.Image')
    buffer = io.BytesIO();Image.new('RGBA', (50, 30), (12, 100, 20, 100)).save(buffer, format='PNG')
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([]))
    scope = bound(app)
    result = app.upload_attachment({**scope, 'name': 'chart.png', 'data': encoded(buffer.getvalue())})
    row = app.assets.get(scope['session_id'], [result['id']])[0]
    assert row['mime'] == 'image/jpeg' and row['data'].startswith(b'\xff\xd8')
    with pytest.raises(RequestError, match='图片理解'):
        app.submit({**scope, 'mode': 'assistant', 'prompt': 'Read image', 'attachments': [result['id']]})
    with pytest.raises(ValueError): app.assets.upload(scope['session_id'], 'fake.png', encoded('<svg onload=alert(1)>'))


def test_native_payload_includes_real_text_and_image_without_exposing_image_in_audit(tmp_path):
    from test_goal_planning_contract import NativePlanningClient
    Image = pytest.importorskip('PIL.Image')
    class VisionClient(NativePlanningClient):
        bigmodel_glm = True
        def __init__(self):
            super().__init__(); self.outputs = ['<final>Inspected the provided material.</final>']
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=VisionClient)
    scope = bound(app)
    doc = app.upload_attachment({**scope, 'name': 'scope.md', 'data': encoded('FISH_SCOPE_ONLY')})
    image = io.BytesIO();Image.new('RGB', (10,10), 'green').save(image, format='PNG')
    picture = app.upload_attachment({**scope, 'name': 'image.png', 'data': encoded(image.getvalue())})
    task, _ = complete(app, {**scope, 'mode': 'assistant', 'prompt': 'Review attachments', 'attachments': [doc['id'], picture['id']]})
    assert task.status == 'completed', task.error
    payload = task.agent.model_client.sent[0]
    assert 'FISH_SCOPE_ONLY' in json.dumps(payload)
    parts = payload['messages'][1]['content']
    assert isinstance(parts, list) and parts[1]['type'] == 'image_url'
    assert parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    audit = task.agent.model_client.native_conversation.audit_request()
    assert 'data:image/jpeg;base64,' not in json.dumps(audit)
    assert app.session(task.session_id)['messages'][0]['attachments'][0]['id'] == doc['id']
    other = app.goals.action({'action': 'create_goal', 'title': 'Other goal'})
    with pytest.raises(RequestError, match='当前对话'):
        app.submit({'goal_id': other['id'], 'mode':'assistant','prompt':'Read foreign file','attachments':[doc['id']]})


def test_http_upload_requires_csrf_and_scope(tmp_path):
    import urllib.request, urllib.error
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([]))
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    body = json.dumps({**bound(app), 'name': 'note.txt', 'data': encoded('not an instruction')}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(urllib.request.Request(url+'/api/attachments', data=body))
        assert exc.value.code == 403
        request = urllib.request.Request(url+'/api/attachments', data=body, headers={'X-Pico-Token':app.csrf})
        result = json.load(opener.open(request))
        assert result['name'] == 'note.txt'
    finally:
        server.shutdown();server.server_close();thread.join(3)


def test_docx_and_pdf_extract_text_without_executing_embedded_content(tmp_path):
    import zipfile
    assets = ConversationAssets(tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as doc:
        doc.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fish genome project</w:t></w:r></w:p></w:body></w:document>')
    result = assets.upload('owner', 'requirements.docx', encoded(archive.getvalue()))
    assert 'Fish genome project' in assets.get('owner', [result['id']])[0]['extracted']
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = pypdf.PdfWriter();page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 10 250 Td (Fish study requirements) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    pdf = io.BytesIO();writer.write(pdf)
    result = assets.upload('owner', 'study.pdf', encoded(pdf.getvalue()))
    assert 'Fish study requirements' in assets.get('owner', [result['id']])[0]['extracted']


def test_settings_cannot_change_during_pending_confirmation_and_cancel_stops_gate(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(planner_outputs()))
    scope = bound(app)
    settings = {'planning': 'step', 'implementation': 'autonomous'}
    task = app.task(app.submit({**scope, 'mode':'assistant', 'prompt':'Plan', 'cooperation': settings})['id'])
    with task.condition:
        assert task.condition.wait_for(lambda: task.approval is not None, 20)
    with pytest.raises(RequestError, match='运行中'):
        app.set_cooperation({**scope, 'cooperation': {'planning':'autonomous','implementation':'autonomous'}})
    app.cancel(task.id)
    with task.condition:
        assert task.condition.wait_for(lambda: task.finished is not None, 20)
    assert task.status == 'stopped' and not app.goals.read()['goals']


def test_declined_later_plan_item_keeps_batch_atomic(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([
        planner_outputs()[1], '<final>The plan was declined; no task batch was saved.</final>']))
    goal = app.goals.action({'action':'create_goal','title':'Existing objective'})
    task, approvals = complete(app, {'goal_id':goal['id'], 'mode':'assistant', 'prompt':'Plan two steps',
        'cooperation': {'planning':'step','implementation':'autonomous'}}, allow=lambda a: a['args']['step']==1)
    assert len(approvals)==2 and not app.goals.read()['tasks']
    assert task.status=='completed'


@pytest.mark.parametrize('level,expected_kinds', [
    ('step', ['implementation_task','implementation_step','safety']),
    ('balanced', ['implementation_task','safety']),
    ('autonomous', ['safety']),
])
def test_coordinator_delegation_inherits_preferences_and_child_safety(tmp_path, level, expected_kinds):
    (tmp_path/'source.txt').write_text('actual source',encoding='utf-8')
    class Dispatcher(FakeModelClient):
        def __init__(self): super().__init__([]);self.step=0
        def complete(self,prompt,*args,**kwargs):
            self.step+=1
            scope=json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n',1)[1])[0]
            if scope['mode']=='execute':
                return [tool('read_file',{'path':'source.txt'}),tool('write_file',{'path':'output.txt','content':'done'}),'<final>Wrote actual output.</final>'][self.step-1]
            if self.step==1:return tool('run_task',{'task_id':scope['existing_tasks'][0]['id']})
            return '<final>Task result requires manual review.</final>'
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=Dispatcher)
    goal=app.goals.action({'action':'create_goal','title':'Objective'})
    item=app.goals.action({'action':'create_task','goal_id':goal['id'],'title':'Implement','verification_mode':'manual'})
    task,approvals=complete(app,{'goal_id':goal['id'],'mode':'assistant','prompt':'Execute the existing task',
        'cooperation':{'planning':'autonomous','implementation':level},'max_steps':10})
    assert task.status=='completed',task.error
    assert [a['kind'] for a in approvals]==expected_kinds
    assert (tmp_path/'output.txt').read_text()=='done'
    assert app.goals.read()['tasks'][item['id']]['status']=='review'
    assert app.active_id is None
