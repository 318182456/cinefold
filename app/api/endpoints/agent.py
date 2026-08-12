"""AI 助手对话。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity

router = APIRouter(tags=["agent"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str = Field(default="", max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list)


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

    history = [{"role": m.role, "content": m.content} for m in body.history]
    return ResponseEntity.ok(ask(body.question, history))


class ConfirmRequest(BaseModel):
    proposal_id: str


@router.post("/agent/confirm")
def agent_confirm(body: ConfirmRequest, current_user: str = Depends(get_current_user)):
    """执行助手提出的下载器操作。只有走过这一步才会真的动手。"""
    from app.modules.agent.actions import execute

    result = execute(body.proposal_id)
    if not result.get("ok"):
        return ResponseEntity.fail(result.get("message") or "执行失败", data=result)
    return ResponseEntity.ok(result, message=result.get("message") or "已执行")


@router.post("/agent/cancel")
def agent_cancel(body: ConfirmRequest, current_user: str = Depends(get_current_user)):
    """放弃一个待确认操作。"""
    from app.modules.agent.actions import take_proposal

    taken = take_proposal(body.proposal_id)
    return ResponseEntity.ok({"cancelled": bool(taken)})
