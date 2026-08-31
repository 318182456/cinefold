"""Emby 媒体库查询。"""
from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import get_settings


class Emby:
    def __init__(self, url: str = "", api_key: str = ""):
        settings = get_settings()
        self.url = (url or settings.emby_url).rstrip("/")
        self.api_key = api_key or settings.emby_api_key

    def search(self, keyword: str) -> bool:
        """番号是否已入库。"""
        if not self.url or not self.api_key:
            return False

        try:
            # Emby 一般就在内网，httpx 的 NO_PROXY 不认 CIDR，
            # 只能在这里直接不信任环境代理，否则请求被转出去必然超时
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(
                    f"{self.url}/emby/Items",
                    params={
                        "api_key": self.api_key,
                        "SearchTerm": keyword,
                        "IncludeItemTypes": "Movie",
                        "Recursive": "true",
                        "Limit": 5,
                    },
                )
                response.raise_for_status()
                items = response.json().get("Items", [])

            # Emby 的模糊搜索会命中相近番号，这里要求名称真正包含关键词
            keyword_upper = keyword.upper()
            for item in items:
                if keyword_upper in (item.get("Name") or "").upper():
                    logger.debug(f"[{keyword}] Emby 已存在")
                    return True
            return False
        except Exception as exc:
            logger.warning(f"查询 Emby 失败: {exc}")
            return False

    def find_item(self, code: str) -> dict:
        """按番号找条目，返回原始 Item 字典。找不到返回空字典。

        与 search 的判定一致（要求名称真含番号），但那个只回布尔值，
        这里要拿 Id 去改字段，所以单独走一趟。
        """
        if not self.url or not self.api_key:
            return {}

        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(
                    f"{self.url}/emby/Items",
                    params={
                        "api_key": self.api_key,
                        "SearchTerm": code,
                        "IncludeItemTypes": "Movie",
                        "Recursive": "true",
                        "Limit": 5,
                    },
                )
                response.raise_for_status()
                items = response.json().get("Items", [])

            code_upper = code.upper()
            for item in items:
                if code_upper in (item.get("Name") or "").upper():
                    return item
            return {}
        except Exception as exc:
            logger.warning(f"查询 Emby 条目失败 {code}: {exc}")
            return {}

    def update_overview(self, code: str, block: str) -> bool:
        """把一段文字并进条目的简介（Overview）。

        Emby 的条目更新接口要求回传完整对象：POST /Items/{id} 是整体覆盖，
        只发 Overview 一个字段会把其余元数据清空。所以必须先用
        /Users/{uid}/Items/{id} 取回完整条目，改掉 Overview 再发回去
        —— 列表接口返回的是精简对象，拿它回传同样会丢字段。

        怎么拼由 services.review.merge_text 决定，这里不重复实现。
        """
        if not self.url or not self.api_key:
            return False

        item = self.find_item(code)
        item_id = item.get("Id")
        if not item_id:
            logger.debug(f"[影评] Emby 里没有 {code} 的条目，跳过推送")
            return False

        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                # 取完整条目。这个接口要 userId，随便取一个管理员账号即可 ——
                # 简介是条目级字段，不因用户而异
                user_id = self._any_user_id(client)
                if not user_id:
                    return False

                detail = client.get(
                    f"{self.url}/emby/Users/{user_id}/Items/{item_id}",
                    params={"api_key": self.api_key},
                )
                detail.raise_for_status()
                full = detail.json()

                # 拼接规则（AI 段放最前、按首尾标记精确替换旧的那段）只在
                # services/review 里实现一份。这里再写一遍必然与那边漂移 ——
                # 同一段文字在 NFO 与 Emby 里长得不一样才是真麻烦
                from app.services.review import merge_text

                full["Overview"] = merge_text(full.get("Overview") or "", block)

                saved = client.post(
                    f"{self.url}/emby/Items/{item_id}",
                    params={"api_key": self.api_key},
                    json=full,
                )
                saved.raise_for_status()

            logger.info(f"[影评] 已推送 Emby 条目 {code}")
            return True
        except Exception as exc:
            logger.warning(f"[影评] 更新 Emby 简介失败 {code}: {exc}")
            return False

    def _any_user_id(self, client: httpx.Client) -> str:
        """取任意一个用户 Id。取完整条目的接口需要它。"""
        try:
            response = client.get(
                f"{self.url}/emby/Users", params={"api_key": self.api_key}
            )
            response.raise_for_status()
            users = response.json() or []
            return (users[0] or {}).get("Id", "") if users else ""
        except Exception as exc:
            logger.warning(f"获取 Emby 用户列表失败: {exc}")
            return ""

    def test_connection(self) -> tuple[bool, str]:
        if not self.url or not self.api_key:
            return False, "未配置 Emby 地址或 API Key"
        try:
            # Emby 一般就在内网，httpx 的 NO_PROXY 不认 CIDR，
            # 只能在这里直接不信任环境代理，否则请求被转出去必然超时
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(
                    f"{self.url}/emby/System/Info", params={"api_key": self.api_key}
                )
                response.raise_for_status()
                return True, f"连接成功，Emby {response.json().get('Version', '')}"
        except Exception as exc:
            return False, str(exc)
