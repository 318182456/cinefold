"""Account-scoped chat turns in the database; only recent pages enter memory."""
import hashlib
import json
import threading
import time
import uuid

from sqlalchemy import select, delete
from app.database.models import Cache
from app.database.session import session_scope

NS='agent-chat'
_active=set()
_lock=threading.Lock()


def _prefix(user):return hashlib.sha256(user.encode()).hexdigest()+':'


def begin(user,question):
    with session_scope() as db:
        row=Cache(namespace=NS,key=_prefix(user)+uuid.uuid4().hex,content=json.dumps({'question':question[:2000],'answer':'','started_at':time.time(),'status':'running'},ensure_ascii=False))
        db.add(row);db.flush();turn_id=row.id
    with _lock:_active.add(turn_id)
    return turn_id


def finish(user,turn_id,result):
    replaced=[]
    try:
        with session_scope() as db:
            row=db.get(Cache,turn_id)
            if row is None or row.namespace!=NS or not row.key.startswith(_prefix(user)):return
            data=json.loads(row.content)
            data.update(answer=str(result.get('answer') or '')[:32000],tools=list(result.get('tools_used') or [])[:20],
                        proposals=[{**p,'status':'pending'} for p in result.get('proposals') or []][:20],status='completed',finished_at=time.time(),failed=bool(result.get('failed')))
            row.content=json.dumps(data,ensure_ascii=False)
            if data['proposals']:
                older=db.scalars(select(Cache).where(Cache.namespace==NS,Cache.key.like(_prefix(user)+'%'),Cache.id<turn_id,Cache.content.contains('"status": "pending"'))).all()
                for previous in older:
                    old=json.loads(previous.content)
                    for proposal in old.get('proposals') or []:
                        if proposal.get('status')=='pending':
                            proposal['status']='superseded';replaced.append(proposal['id'])
                    previous.content=json.dumps(old,ensure_ascii=False)
        from app.modules.agent.actions import take_proposal
        for proposal_id in replaced:take_proposal(proposal_id)
    finally:
        with _lock:_active.discard(turn_id)


def read(user,before=0,limit=20):
    from app.modules.agent.actions import list_proposals
    limit=max(1,min(50,limit))
    live={p['id'] for p in list_proposals()}
    query=select(Cache).where(Cache.namespace==NS,Cache.key.like(_prefix(user)+'%'))
    if before:query=query.where(Cache.id<before)
    with session_scope() as db:
        rows=db.scalars(query.order_by(Cache.id.desc()).limit(limit+1)).all()
        has_more=len(rows)>limit;rows=rows[:limit];messages=[]
        for row in reversed(rows):
            try:data=json.loads(row.content)
            except (ValueError,TypeError):continue
            messages.append({'id':f'{row.id}-user','role':'user','content':data.get('question',''),'timestamp':data.get('started_at')})
            proposals=[]
            for proposal in data.get('proposals') or []:
                proposal=dict(proposal)
                if proposal.get('status')=='pending' and proposal.get('id') not in live:proposal['status']='expired'
                proposals.append(proposal)
            answer=data.get('answer') or ('正在处理这条提问…' if row.id in _active else '本轮回答未完成，可以重新提问。')
            messages.append({'id':f'{row.id}-assistant','role':'assistant','content':answer,'timestamp':data.get('finished_at'),
                             'tools':data.get('tools') or [],'proposals':proposals,'failed':data.get('failed',False)})
        return {'messages':messages,'has_more':has_more,'next_before':min((row.id for row in rows),default=0)}


def update_proposal(user,proposal_id,status,result=''):
    with session_scope() as db:
        rows=db.scalars(select(Cache).where(Cache.namespace==NS,Cache.key.like(_prefix(user)+'%'),Cache.content.contains(proposal_id))).all()
        for row in rows:
            data=json.loads(row.content)
            for proposal in data.get('proposals') or []:
                if proposal.get('id')==proposal_id:proposal.update(status=status,result=result)
            row.content=json.dumps(data,ensure_ascii=False)


def clear(user):
    from app.modules.agent.actions import take_proposal
    proposals=[]
    with session_scope() as db:
        rows=db.scalars(select(Cache).where(Cache.namespace==NS,Cache.key.like(_prefix(user)+'%'),Cache.content.contains('"status": "pending"'))).yield_per(50)
        for row in rows:
            try:proposals.extend(p['id'] for p in json.loads(row.content).get('proposals') or [] if p.get('status')=='pending')
            except (ValueError,TypeError):pass
        db.execute(delete(Cache).where(Cache.namespace==NS,Cache.key.like(_prefix(user)+'%')))
    for proposal_id in proposals:take_proposal(proposal_id)
