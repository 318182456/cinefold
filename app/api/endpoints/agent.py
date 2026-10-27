"""AI 助手对话。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
import json
import queue
import threading
from fastapi.responses import StreamingResponse

from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["agent"])
_chat_slots = threading.BoundedSemaphore(2)


@router.get('/agent/history')
def chat_history(before:int=0,current_user:str=Depends(get_current_user)):
    from app.modules.agent import history
    return ResponseEntity.ok(history.read(current_user,before))


@router.delete('/agent/history')
def clear_chat_history(current_user:str=Depends(get_current_user)):
    from app.modules.agent import history
    history.clear(current_user)
    return ResponseEntity.ok(message='聊天记录已清空')


class ChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(default="", max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=24)


@router.get("/agent/status")
def agent_status(current_user: str = Depends(get_current_user)):
    """助手是否可用。前端据此决定悬浮球显示与否。"""
    from app.modules.agent import ChatAgent

    agent = ChatAgent()
    return ResponseEntity.ok({
        "enabled": agent.enabled,
        "switched_on": agent.switched_on,
        "model": agent.model if agent.enabled else "",
    })


@router.post("/agent/chat")
def agent_chat(body: ChatRequest, current_user: str = Depends(get_current_user)):
    from app.modules.agent import ask
    from app.modules.agent import history as saved_history

    history = [{"role": m.role, "content": m.content} for m in body.history]
    turn=saved_history.begin(current_user,body.question)
    try:
        result=ask(body.question,history)
    except Exception:
        saved_history.finish(current_user,turn,{'answer':'本轮回答失败，请重新提问。','failed':True})
        raise
    saved_history.finish(current_user,turn,result)
    return ResponseEntity.ok(result)


@router.post('/agent/chat/stream')
def agent_chat_stream(body:ChatRequest,current_user:str=Depends(get_current_user)):
    if not _chat_slots.acquire(blocking=False):return ResponseEntity.fail('助手正在处理其他会话，请稍后再试',code=429)
    from app.modules.agent import ChatAgent
    from app.modules.agent import history
    try:turn=history.begin(current_user,body.question)
    except Exception:
        _chat_slots.release()
        return ResponseEntity.fail('无法保存聊天记录，请检查数据库连接',code=503)
    events=queue.Queue(maxsize=32);stop=threading.Event()
    def send(event):
        try:events.put_nowait(event)
        except queue.Full:
            try:events.get_nowait()
            except queue.Empty:pass
            events.put_nowait(event)
    def run():
        try:
            result=ChatAgent().chat(body.question,[m.model_dump() for m in body.history],on_progress=lambda data:send({'event':'progress','data':data}),stop=stop)
            history.finish(current_user,turn,result)
            send({'event':'result','data':result})
        except Exception:
            try:history.finish(current_user,turn,{'answer':'本轮回答失败，请重新提问。','failed':True})
            except Exception:pass
            send({'event':'error','message':'助手请求中断，请稍后重试'})
        finally:_chat_slots.release()
    try:threading.Thread(target=run,daemon=True,name='agent-chat').start()
    except Exception:
        _chat_slots.release()
        history.finish(current_user,turn,{'answer':'无法启动本轮对话，请重试。','failed':True})
        raise
    def stream():
        try:
            while True:
                try:event=events.get(timeout=10)
                except queue.Empty:
                    yield json.dumps({'event':'heartbeat'})+'\n'
                    continue
                yield json.dumps(event,ensure_ascii=False)+'\n'
                if event['event'] in ('result','error'):break
        finally:stop.set()
    return StreamingResponse(stream(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})


class ConfirmRequest(BaseModel):
    proposal_id: str
    # 删除类操作时由确认对话上的复选框传入；None 表示沿用提案里的动作
    delete_files: bool | None = None


@router.get("/agent/proposals")
def pending_proposals(current_user: str = Depends(get_current_user)):
    from app.modules.agent.actions import list_proposals
    return ResponseEntity.ok(list_proposals())


@router.post("/agent/confirm")
def agent_confirm(body: ConfirmRequest, current_user: str = Depends(get_current_user)):
    """执行助手提出的下载器操作。只有走过这一步才会真的动手。"""
    from app.modules.agent.actions import execute

    result = execute(body.proposal_id, body.delete_files)
    if not result.get("ok"):
        return ResponseEntity.fail(result.get("message") or "执行失败", data=result)
    from app.modules.agent import history
    try:history.update_proposal(current_user,body.proposal_id,'done',result.get('message') or '已执行')
    except Exception:
        # A history write failure must not turn an already executed operation
        # back into a pending button that appears safe to execute again.
        result['history_saved']=False
        result['message']=(result.get('message') or '操作已执行')+'（聊天记录状态保存失败）'
    return ResponseEntity.ok(result, message=result.get("message") or "已执行")


@router.post("/agent/cancel")
def agent_cancel(body: ConfirmRequest, current_user: str = Depends(get_current_user)):
    """放弃一个待确认操作。"""
    from app.modules.agent.actions import take_proposal

    taken = take_proposal(body.proposal_id)
    from app.modules.agent import history
    try:history.update_proposal(current_user,body.proposal_id,'cancelled')
    except Exception:pass
    return ResponseEntity.ok({"cancelled": bool(taken)})
