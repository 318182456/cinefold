from app.modules.agent import history, actions
from tests.test_scrape_reliability import db
from tests.test_cache_maintenance import postgres_catalog


def _account_roundtrip():
    first=history.begin('alice','Remember me')
    history.finish('alice',first,{'answer':'Stored answer','tools_used':['get_rank']})
    second=history.begin('bob','Other account')
    history.finish('bob',second,{'answer':'Private'})
    restored=history.read('alice')
    assert [message['content'] for message in restored['messages']]==['Remember me','Stored answer']
    assert restored['messages'][1]['tools']==['get_rank']
    history.clear('alice')
    assert not history.read('alice')['messages'] and history.read('bob')['messages']


def test_history_roundtrip_is_account_scoped(db):_account_roundtrip()


def test_postgres_history_roundtrip_is_account_scoped(postgres_catalog):_account_roundtrip()


def test_pagination_interruption_and_clear_does_not_resurrect(db,monkeypatch):
    for index in range(24):
        turn=history.begin('alice',str(index));history.finish('alice',turn,{'answer':f'reply {index}'})
    recent=history.read('alice');older=history.read('alice',recent['next_before'])
    assert recent['has_more'] and len(recent['messages'])==40
    assert not older['has_more'] and len(older['messages'])==8
    assert not {m['id'] for m in recent['messages']} & {m['id'] for m in older['messages']}
    running=history.begin('alice','Interrupted')
    monkeypatch.setattr(history,'_active',set())
    assert '未完成' in history.read('alice')['messages'][-1]['content']
    history.clear('alice')
    history.finish('alice',running,{'answer':'late'})
    assert history.read('alice')['messages']==[]


def test_restored_proposals_expire_and_new_plan_replaces_old(db,monkeypatch):
    monkeypatch.setattr(actions,'_proposals',{})
    first=actions.create_proposal('run_task',[{'job_id':'crawl_resources','name':'task'}])
    turn=history.begin('alice','First');history.finish('alice',turn,{'answer':'Confirm first','proposals':[first]})
    assert history.read('alice')['messages'][-1]['proposals'][0]['status']=='pending'
    second=actions.create_proposal('run_task',[{'job_id':'crawl_resources','name':'task'}])
    turn=history.begin('alice','Second');history.finish('alice',turn,{'answer':'Confirm second','proposals':[second]})
    assert actions.take_proposal(first['id']) is None
    assert history.read('alice')['messages'][1]['proposals'][0]['status']=='superseded'
    actions.take_proposal(second['id'])
    assert history.read('alice')['messages'][-1]['proposals'][0]['status']=='expired'
