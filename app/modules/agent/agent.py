"""对话 agent。走 OpenAI 兼容接口 + function calling，复用翻译那套 AI 配置。"""
from __future__ import annotations

import json

import httpx
from loguru import logger

from app.core.config import get_settings
from app.core.version import APP_VERSION

from .tools import PROPOSAL_TOOLS, REGISTRY, TOOL_SCHEMAS, call_tool


def _extract_proposals(payload: str) -> list[dict]:
    """从工具返回的 JSON 里取出提案。取不到就当没有，不影响对话。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    proposal = data.get("proposal") if isinstance(data, dict) else None
    return [proposal] if isinstance(proposal, dict) else []

SYSTEM_PROMPT = f"""你是 cinefold（版本 {APP_VERSION}）的运维助手。cinefold 是影片资源自动化订阅下载器，
链路是：抓取番号 → 按规则过滤 → 推送下载器 → 校验入库 → 消息通知。

番号的订阅状态流转：未订阅 → 已订阅待资源 → 下载中 → 已下载 → 已入库，中间失败会标为失败。

回答规则：
- 涉及具体数据一律先调工具查，绝不凭印象编造数字、番号、日期。
- 工具查不到就直说查不到，并给出下一步排查建议。
- 用简体中文回答，简洁直接，能用一两句说清就不要展开。
- 数据条目多的时候用 Markdown 表格或列表呈现。
- 用户问「什么情况」「怎么样了」这类笼统问题，先用 overview 看全局，
  必要时再结合 list_tasks、read_logs 补充。

关于下载器操作：
- 你可以操作 qBittorrent / Transmission 的任务（暂停、恢复、重新检查、
  强制汇报、删除、转移做种）。流程固定为：先用 list_downloads 拿到真实 hash，
  再用 propose_action 提交。
- propose_action 只是生成待确认项，不会真的执行。调用后必须告诉用户
  「请在下方确认后执行」，绝不能说「已经暂停了」「已经删掉了」这类话。
- 执行与否由用户点界面上的按钮决定，你没有"执行"这个能力。若上一轮你已
  提过案、用户这轮只回「确认」「好」「执行」这类话，不要再调 propose_action
  重复提案 —— 直接告诉用户点上面那条确认条上的按钮即可。
- 用户改条件（如「不保留文件」「改成暂停」）时才重新提案，并说明这条会
  取代上一条。
- 删除分两种：delete 只从下载器移除任务、磁盘文件保留；delete_with_files
  连文件一起删且不可逆。用户没有明确说要删文件时，一律选 delete。
- transfer 是转移做种：把 qBittorrent 里已下载完的种子交给 Transmission
  接着做种，文件不移动也不复制。只对进度 100% 的任务有效，没下载完的会被
  跳过；两个下载器缺一个就用不了，这时如实说明而不是提议。
- 筛选条件要复述清楚（比如「进度低于 10% 的 3 个任务」），让用户能核对
  范围对不对。范围拿不准时先问，不要自己扩大。
- list_downloads 返回 note 说还有未列出的任务时，必须告诉用户「符合条件的
  共 N 个，这次只处理其中 M 个，剩下的需要再操作一次」。一次最多 20 个，
  绝不能让用户以为点一次确认就全处理完了。
- 订阅、下载新资源、改配置这些仍然做不到，被问到就说明并指出该去哪个页面。"""

# 一轮对话里最多允许模型连续调多少次工具。给足串联查询的空间
# （比如先 overview 再 read_logs），又不至于在模型犯傻时无限打转
MAX_TOOL_ROUNDS = 5

# 只把最近若干条历史发回模型。对话上下文越长越贵，运维问答也很少需要翻旧账
MAX_HISTORY = 12


class ChatAgent:
    def __init__(self):
        settings = get_settings()
        # 助手有独立配置，留空则回退到翻译用的那套，已配过 AI 的用户开箱可用。
        # 注意是整组回退：混用两边的地址和 Key 只会拼出一个连不上的组合
        if settings.agent_url and settings.agent_api_key:
            url, model, api_key = settings.agent_url, settings.agent_model, settings.agent_api_key
        else:
            url, model, api_key = settings.openai_url, settings.openai_model, settings.openai_api_key

        self.switched_on = settings.agent_enabled
        self.url = (url or "").rstrip("/")
        self.model = model or "gpt-4o-mini"
        self.api_key = api_key or ""
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.switched_on and self.url and self.api_key)

    @property
    def endpoint(self) -> str:
        if self.url.endswith("/chat/completions"):
            return self.url
        return f"{self.url}/chat/completions"

    def _post(self, client: httpx.Client, messages: list[dict]) -> dict:
        response = client.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
                "temperature": 0.3,
            },
        )
        response.raise_for_status()
        return response.json()

    def chat(self, question: str, history: list[dict] | None = None) -> dict:
        """跑一轮问答，返回答复与本轮实际调用过的工具。

        工具名一并返回，前端展示"查了什么"，用户才知道答复的依据。
        """
        if not self.switched_on:
            return {
                "answer": "AI 助手已在 设置 → 其他 → AI 助手 中关闭。",
                "tools_used": [],
                "proposals": [],
                "enabled": False,
            }
        if not self.enabled:
            return {
                "answer": (
                    "还没配置 AI 助手的接口。请到 设置 → 其他 → AI 助手 "
                    "填写接口地址、模型和 API Key（留空则沿用翻译的 AI 配置）。"
                ),
                "tools_used": [],
                "proposals": [],
                "enabled": False,
            }
        if not (question or "").strip():
            return {
                "answer": "请说说你想了解什么。",
                "tools_used": [],
                "proposals": [],
                "enabled": True,
            }

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in (history or [])[-MAX_HISTORY:]:
            role = item.get("role")
            content = (item.get("content") or "").strip()
            # 历史里只回灌纯文本问答，工具调用的中间过程不留档
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        tools_used: list[str] = []
        # 本轮产生的待确认操作，随答复一起带给前端渲染确认条
        proposals: list[dict] = []

        try:
            with httpx.Client(timeout=120, proxy=self.proxy) as client:
                for _ in range(MAX_TOOL_ROUNDS):
                    data = self._post(client, messages)
                    choices = data.get("choices") or []
                    if not choices:
                        return {
                            "answer": "AI 接口没有返回内容，稍后再试。",
                            "tools_used": tools_used,
                            "proposals": proposals,
                            "enabled": True,
                        }

                    message = choices[0].get("message") or {}
                    tool_calls = message.get("tool_calls") or []

                    if not tool_calls:
                        answer = (message.get("content") or "").strip()
                        return {
                            "answer": answer or "没能得出结论，换个说法再问问？",
                            "tools_used": tools_used,
                            "proposals": proposals,
                            "enabled": True,
                        }

                    # 带着 tool_calls 原样回填，再逐个附上执行结果
                    messages.append({
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    })

                    for call in tool_calls:
                        func = call.get("function") or {}
                        name = func.get("name") or ""
                        result = call_tool(name, func.get("arguments") or "{}")
                        if name in REGISTRY:
                            tools_used.append(name)
                        if name in PROPOSAL_TOOLS:
                            proposals += _extract_proposals(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.get("id") or "",
                            "content": result,
                        })

            # 轮次用尽还在调工具，说明模型绕不出来了
            logger.warning(f"agent 工具调用超过 {MAX_TOOL_ROUNDS} 轮仍未收敛")
            return {
                "answer": "这个问题查得有点绕，我没能收敛出结论。试试把问题问得更具体些。",
                "tools_used": tools_used,
                "proposals": proposals,
                "enabled": True,
            }

        except httpx.HTTPStatusError as exc:
            logger.warning(f"agent 接口返回错误: {exc.response.status_code} {exc}")
            return {
                "answer": f"AI 接口返回错误（HTTP {exc.response.status_code}），请检查接口地址、模型名与 API Key。",
                "tools_used": tools_used,
                "proposals": proposals,
                "enabled": True,
            }
        except Exception as exc:
            logger.warning(f"agent 对话异常: {exc}")
            return {
                "answer": f"对话失败：{exc}",
                "tools_used": tools_used,
                "proposals": proposals,
                "enabled": True,
            }


def ask(question: str, history: list[dict] | None = None) -> dict:
    return ChatAgent().chat(question, history)
