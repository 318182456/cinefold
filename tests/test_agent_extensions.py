import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.agent import actions
from app.modules.agent.agent import ChatAgent, MAX_HISTORY_CHARS, MAX_TOOL_RESULT_CHARS
from app.modules.agent.tools import call_tool
from app.services import resourcecatalog
from tests.test_resource_crawler import native, post
from tests.test_scrape_reliability import db


def test_resource_and_task_proposals_do_nothing_before_confirmation(native,monkeypatch):
    from app import services, scheduler
    key=resourcecatalog.put('sehuatang',1,'37',post())
    pushed=[];tasks=[]
    monkeypatch.setattr(services,'download_torrent',lambda code,torrent,**kw:(pushed.append((code,torrent.download_url,kw)) or True))
    monkeypatch.setattr(scheduler,'push_job',lambda job:(tasks.append(job) or True))
    found=json.loads(call_tool('browse_resources',{'keyword':'ABC-001'}))
    assert found['items'][0]['key']==key and 'previews' not in found['items'][0]
    proposal=json.loads(call_tool('propose_resource_download',{'key':key}))['proposal']
    task=json.loads(call_tool('propose_task',{'job_id':'crawl_avbase_date'}))['proposal']
    assert pushed==[] and tasks==[]
    assert actions.execute(proposal['id'])['affected']==1
    assert pushed[0][0]=='ABC-001' and pushed[0][2]=={'downloader':'qbittorrent'}
    assert not actions.execute(proposal['id'])['ok'] and len(pushed)==1
    assert actions.execute(task['id'])['ok'] and tasks==['crawl_avbase_date']
    assert 'error' in json.loads(call_tool('propose_task',{'job_id':'invented'}))


def test_resource_change_invalidates_pending_download(native,monkeypatch):
    from app import services
    from app.database.models import ResourcePost
    key=resourcecatalog.put('sehuatang',1,'37',post())
    proposal=json.loads(call_tool('propose_resource_download',{'key':key}))['proposal']
    with native[1].begin() as session:session.get(ResourcePost,key).magnets=[]
    monkeypatch.setattr(services,'download_torrent',lambda *a,**k:(_ for _ in ()).throw(AssertionError('stale resource must not download')))
    assert not actions.execute(proposal['id'])['ok']


def test_agent_bounds_context_tool_results_and_reuses_duplicate_calls(monkeypatch):
    from app.modules.agent import agent as module
    agent=ChatAgent();agent.switched_on=True;agent.url='http://fake';agent.api_key='fake'
    rounds=[];executed=[]
    def reply(client,messages):
        rounds.append(list(messages))
        if len(rounds)==1:
            calls=[{'id':str(i),'function':{'name':'overview','arguments':'{}'}} for i in range(2)]
            return {'choices':[{'message':{'tool_calls':calls}}]}
        return {'choices':[{'message':{'content':'done'}}]}
    monkeypatch.setattr(agent,'_post',reply)
    monkeypatch.setattr(module,'call_tool',lambda name,args:(executed.append(name) or json.dumps({'items':'x'*30000})))
    history=[{'role':'user' if i%2 else 'assistant','content':'x'*8000} for i in range(12)]
    result=agent.chat('question',history)
    assert result['answer']=='done' and executed==['overview']
    assert sum(len(m['content']) for m in rounds[0] if m['role']!='system')<=MAX_HISTORY_CHARS+len('question')
    for message in rounds[1]:
        if message['role']=='tool':assert json.loads(message['content'])['truncated'] and len(message['content'])<MAX_TOOL_RESULT_CHARS+500
    assert 'error' in json.loads(call_tool('overview','[]'))


def test_agent_stream_reports_progress_and_final_result(native,monkeypatch):
    from app.api.endpoints import get_current_user
    from app.api.endpoints import agent as api
    seen=[]
    def chat(self,question,history,**kwargs):
        kwargs['on_progress']({'phase':'tool','tool':'get_rank'})
        seen.append(kwargs['stop'])
        return {'answer':'done','tools_used':['get_rank'],'proposals':[]}
    monkeypatch.setattr(ChatAgent,'chat',chat)
    app=FastAPI();app.include_router(api.router)
    with TestClient(app) as client:
        assert client.post('/agent/chat/stream',json={'question':'ranks'}).status_code==401
        app.dependency_overrides[get_current_user]=lambda:'test'
        response=client.post('/agent/chat/stream',json={'question':'ranks'})
        events=[json.loads(line) for line in response.text.splitlines()]
        assert [event['event'] for event in events]==['progress','result']
        assert events[-1]['data']['answer']=='done' and seen[0].is_set()


def test_history_failure_does_not_report_executed_action_as_failed(monkeypatch):
    from app.api.endpoints import get_current_user
    from app.api.endpoints import agent as api
    from app.modules.agent import history
    monkeypatch.setattr(actions,'execute',lambda *a:{'ok':True,'message':'任务已提交','affected':1})
    monkeypatch.setattr(history,'update_proposal',lambda *a:(_ for _ in ()).throw(RuntimeError('database unavailable')))
    app=FastAPI();app.include_router(api.router);app.dependency_overrides[get_current_user]=lambda:'test'
    with TestClient(app) as client:
        response=client.post('/agent/confirm',json={'proposal_id':'test'}).json()
        assert response['code']==200 and response['data']['ok']
        assert response['data']['history_saved'] is False
