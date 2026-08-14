"""ThePornDB 解析。

纯 JSON API，不解 HTML —— 唯一一个官方提供结构化接口的源，字段最规整。
覆盖欧美片为主，日本番号也收录，作为其余源全部失败时的兜底。

必须有 API Token 才能用：接口全部要求 `Authorization: Bearer <token>`，
无 token 时一律 401。Token 填在数据源页面的 Cookie 栏 —— 那是每个源唯一的
密钥槽位，不为一个源单独加表字段。填法两种都认：裸 token，或
`Authorization: Bearer xxx` 整行。

接口路径：/jav?parse=<番号> 按番号查（JAV 专用端点，比 /scenes 精确）。
"""
from __future__ import annotations

import json

from loguru import logger

from app.modules.ladysite.base import CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://api.theporndb.net"


class ThePornDb:
    name = "theporndb"

    def __init__(self, host: str = "", cookie: str = ""):
        if not host and not cookie:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        self.client = SiteClient(host or HOST, cookie, interval=1.5)

    def _token(self) -> str:
        """从 cookie 栏取 API Token，兼容裸 token 与整行 Authorization。"""
        raw = (self.client.cookie or "").strip()
        if not raw:
            return ""
        # 用户可能整行粘贴 "Authorization: Bearer xxx"
        lowered = raw.lower()
        for marker in ("authorization:", "bearer "):
            index = lowered.rfind(marker)
            if index >= 0:
                raw = raw[index + len(marker):].strip()
                lowered = raw.lower()
        return raw.strip().strip('"')

    def crawler_original(self, code: str) -> CodeInfo | None:
        normalized = get_true_code(code)
        if not normalized:
            return None

        token = self._token()
        if not token:
            logger.debug("[theporndb] 未配置 API Token，跳过；请在数据源页 Cookie 栏填入")
            return None

        raw = self.client.get(
            "/jav",
            params={"parse": normalized},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # cookie 栏存的是 token 而非真 Cookie，不能当 Cookie 头发出去
                "Cookie": "",
            },
        )
        if not raw:
            return None
        return json_to_code(raw, normalized)


def json_to_code(raw: str, code: str = "") -> CodeInfo | None:
    """解析 ThePornDB 的 JSON 响应。"""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.debug(f"[{code}] theporndb JSON 解析失败: {exc}")
        return None

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    # /jav?parse= 返回单个对象，列表端点返回数组
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data:
        return None

    info = CodeInfo(code=get_true_code(data.get("external_id") or code))
    info.title = (data.get("title") or "").strip()
    info.release_date = (data.get("date") or "")[:10]

    duration = data.get("duration")
    if duration:
        # 接口给的是秒
        try:
            info.duration = f"{int(duration) // 60}分钟"
        except (TypeError, ValueError):
            pass

    info.casts = join_list(
        (item or {}).get("name") for item in data.get("performers") or []
        if isinstance(item, dict)
    )
    info.genres = join_list(
        (item or {}).get("name") for item in data.get("tags") or []
        if isinstance(item, dict)
    )

    site = data.get("site") or {}
    if isinstance(site, dict):
        info.producer = (site.get("name") or "").strip()
        info.publisher = (site.get("network", {}) or {}).get("name", "") or ""

    # 图分 poster / background 两类，各取一张
    for item in data.get("posters") or []:
        if isinstance(item, dict) and item.get("full"):
            info.poster = item["full"]
            break
    background = data.get("background") or {}
    if isinstance(background, dict) and background.get("full"):
        info.banner = background["full"]
    if not info.banner:
        info.banner = info.poster
    if not info.poster:
        info.poster = info.banner

    if not info.code:
        info.code = get_true_code(code)
    return info if info.code and info.title else None
