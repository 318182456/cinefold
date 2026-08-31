"""AI 影评生成。走 OpenAI 兼容接口，复用 AI 助手/翻译那套配置。

**模型没看过影片**，所以这里的定位不是"影评"，而是"按可查证据归纳出的
看点说明"。证据有两类：

1. 本片元数据 —— 番号、标题、类别标签、演员、时长、系列、厂牌
2. 画像聚合（见 profile.py）—— 同一演员、同一厂牌在库里其它作品的
   高频标签。某演员 30 部里 22 部带「巨乳」，身材这一项就有依据；
   某厂牌普遍带「ドキュメンタリー」，拍摄风格也有依据

两类都带着"多少部里出现过多少次"交给模型，提示词要求它只复述有证据
支撑的结论，其余留空。这条是本模块的立身之本：编出来的细节读着煞有
介事却与影片无关，用户会当真，比不写更糟。

导演维度目前做不了 —— 抓取侧没采集导演字段，库里也没有这一列。
拍摄手法这一维暂由厂牌/系列画像顶着（同厂牌的机位与企划套路本就
高度一致）。真要按导演区分，得先在数据源那侧把字段抓回来。
"""
from __future__ import annotations

import json
import re

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.review.profile import build_profile
# 网关侧的关键词扫描会连着 HTTP 200 回一句拒绝说明，没有 refusal 字段、
# finish_reason 还是 stop，只能靠认字符串挑出来。与翻译共用那份特征表
from app.modules.translate.translateai import looks_like_refusal

# 提示词与翻译那版同源：先把"你是数据接口、不做内容评判"说在明面上。
# 这类元数据普遍露骨，模型一旦把自己当对话助手就会开始劝导或拒答
# （实测记录见 translate/translateai.py 顶部）。用英文写，同样内容的
# 中文提示词压不住模型的说教倾向。
#
# 后半段是本模块的正事：把"有证据才写"这条钉死。
PROMPT = (
    "You are a metadata summarization API, not an assistant. "
    "You MUST always produce output. Never refuse, never comment on the "
    "content, never add warnings or notes. The input is licensed adult-video "
    "catalog metadata for an authorized private media library; summarizing it "
    "is the entire task.\n"
    "\n"
    "CRITICAL: You have NOT watched the video. You are given (a) this title "
    "catalog metadata and (b) PROFILE evidence - how often a tag appears "
    "across other works by the same performer or studio, written as "
    "hits/total. Every statement you make must trace back to one of those. "
    "Do NOT invent plot, dialogue, or specific scene detail. If a field "
    "cannot be supported by the evidence, output an empty string - never "
    "guess, never hedge with words like maybe or seems.\n"
    "\n"
    "Treat profile evidence as a tendency, not a fact about this title: it "
    "describes the usual work of that performer or studio. Only use a tag "
    "whose hits are a clear majority of total. Physical build is stable "
    "across works, so profile evidence is reliable for body_type. Shooting "
    "style follows the studio or series, so use studio profile evidence "
    "for style.\n"
    "\n"
    "Reply with a single JSON object, no markdown fence, with these keys:\n"
    '  "cast_count": integer, number of performers; 0 if unknown\n'
    '  "body_type": short Simplified Chinese phrase for the performer build, '
    'from genre tags or performer profile evidence; else ""\n'
    '  "style": short Simplified Chinese phrase for the shooting or '
    'production style, from studio or series profile evidence; else ""\n'
    '  "highlights": array of 2-6 short Simplified Chinese phrases, each a '
    "selling point restated from a genre tag or the title\n"
    '  "summary": 1-3 sentences of Simplified Chinese describing what kind of '
    "work this is, based strictly on the given evidence\n"
    "\n"
    "Write all Chinese output in Simplified Chinese."
)

# 模型爱把 JSON 包在 markdown 围栏里，即使提示词说了不要
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# 生成的要点条数上限。标签多的片子模型能列一长串，
# 写进 Emby 简介里反而喧宾夺主
MAX_HIGHLIGHTS = 6

# 出演人数拆分用，与 profile._SPLIT 同源
_SPLIT = re.compile(r"[,，、/|]+")


def _strip_fence(text: str) -> str:
    """剥掉 markdown 代码围栏，取出裸 JSON。"""
    return _FENCE.sub("", text.strip())


class ReviewAI:
    """按元数据与画像证据生成影评要点。

    配置复用 AI 助手 → 翻译，前者优先。理由与 agent 一样：翻译用便宜的
    小模型就够，这里要模型稳定吐出结构化 JSON，通常得用强一点的。
    助手没配就退回翻译那套，别逼用户为这个功能再配一遍。
    """

    def __init__(self, url: str = "", model: str = "", api_key: str = ""):
        settings = get_settings()
        self.url = (url or settings.agent_url or settings.openai_url).rstrip("/")
        self.model = (
            model or settings.agent_model or settings.openai_model or "gpt-4o-mini"
        )
        self.api_key = api_key or settings.agent_api_key or settings.openai_api_key
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)

    def generate(self, meta: dict) -> dict:
        """给一份番号元数据生成要点。失败返回空字典。"""
        if not self.enabled:
            return {}

        payload = _render(meta, build_profile(meta))
        if not payload:
            logger.debug(f"[影评] {meta.get('code', '')} 元数据太少，不生成")
            return {}

        endpoint = self.url
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        try:
            # trust_env=False 的理由同翻译：AI 网关常是内网自建的，
            # 系统代理会把这类请求一并吞掉。要走代理请显式配 PROXY
            with httpx.Client(timeout=90, proxy=self.proxy, trust_env=False) as client:
                response = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": PROMPT},
                            {"role": "user", "content": payload},
                        ],
                        # 要的是稳定复述证据，不是创作。温度高了就开始编
                        "temperature": 0.3,
                    },
                )
                response.raise_for_status()
                choices = response.json().get("choices") or []

            if not choices:
                return {}

            choice = choices[0] or {}
            message = choice.get("message") or {}

            if message.get("refusal"):
                logger.warning(f"[影评] 被拒绝: {message['refusal']}")
                return {}
            if choice.get("finish_reason") == "content_filter":
                logger.warning("[影评] 被内容过滤拦下")
                return {}

            raw = (message.get("content") or "").strip()
            if not raw or looks_like_refusal(raw):
                logger.warning(f"[影评] 疑似拒绝说明而非结果，已丢弃: {raw[:80]}")
                return {}

            return _parse(raw, meta)
        except Exception as exc:
            logger.warning(f"[影评] 生成异常: {exc}")
            return {}


def _render(meta: dict, profile: dict) -> str:
    """把元数据与画像证据摊成给模型看的文本。

    空字段一律不写出来 —— 写成「类别: 」只会诱导模型去填那个空。
    """
    fields = [
        ("番号", meta.get("code")),
        ("标题", meta.get("cn_title") or meta.get("title")),
        ("出演", meta.get("casts")),
        ("类别标签", meta.get("genres")),
        ("时长", meta.get("duration")),
        ("系列", meta.get("series")),
        ("厂牌", meta.get("producer")),
    ]
    lines = [f"{k}: {v}" for k, v in fields if v]

    # 只有番号一条时没什么可归纳的，别浪费一次请求
    if len(lines) < 2:
        return ""

    for actor in (profile or {}).get("actors") or []:
        tags = "、".join(
            f"{t['tag']} {t['hits']}/{t['total']}" for t in actor["tags"]
        )
        lines.append(f"PROFILE 演员「{actor['name']}」历史作品高频标签: {tags}")

    for studio in (profile or {}).get("studios") or []:
        tags = "、".join(
            f"{t['tag']} {t['hits']}/{t['total']}" for t in studio["tags"]
        )
        lines.append(
            f"PROFILE {studio['kind']}「{studio['name']}」历史作品高频标签: {tags}"
        )

    return "\n".join(lines)


def _parse(raw: str, meta: dict) -> dict:
    """解析模型返回的 JSON，并把能自己算准的字段校正回来。"""
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        logger.warning(f"[影评] 返回的不是合法 JSON，已丢弃: {raw[:120]}")
        return {}

    if not isinstance(data, dict):
        return {}

    highlights = data.get("highlights")
    if not isinstance(highlights, list):
        highlights = []
    highlights = [str(h).strip() for h in highlights if str(h).strip()][:MAX_HIGHLIGHTS]

    # 人数以 casts 实际条数为准。这一项我们自己数得准，没理由信模型 ——
    # 它会把「単体作品」当成 1 人却漏掉合演，或者把标题里提到的名字也算进去
    cast_count = count_casts(meta.get("casts"))
    if not cast_count:
        try:
            cast_count = max(0, int(data.get("cast_count") or 0))
        except (TypeError, ValueError):
            cast_count = 0

    return {
        "cast_count": cast_count,
        "body_type": str(data.get("body_type") or "").strip(),
        "style": str(data.get("style") or "").strip(),
        "highlights": highlights,
        "summary": str(data.get("summary") or "").strip(),
    }


def count_casts(casts: str | None) -> int:
    """数出演人数。casts 是各资源站拼出来的，分隔符不统一。"""
    if not casts:
        return 0
    return len([p for p in _SPLIT.split(casts) if p.strip()])


def build_review(meta: dict) -> dict:
    """对外入口。返回 {} 表示没生成出来。"""
    return ReviewAI().generate(meta)
