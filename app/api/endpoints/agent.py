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
