"""极影视查询适配，接口字段参照本地 article 源码。"""
from http.cookies import SimpleCookie

import httpx
from loguru import logger

from app.core.config import get_settings
from app.utils import find_serial_numbers


class ZSpace:
    def __init__(self):
        settings = get_settings()
        self.url = settings.zspace_url.rstrip("/")
        self.cookie = settings.zspace_cookie.replace("\r", "").replace("\n", "").strip()

    def search(self, keyword: str) -> bool:
        if not self.url or not self.cookie:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(self.cookie)
            values = {key: morsel.value for key, morsel in cookies.items()}
            payload = {key: values.get(key, "") for key in ("plat", "version", "device_id", "device", "token")}
            payload.update(key_word=keyword, _l="zh_cn", nasid=values.get("nas_id", values.get("nasid", "")))
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.post(f"{self.url}/zvideo/search/index", params={"webagent": "v2"}, data=payload, headers={"Cookie": self.cookie})
                response.raise_for_status()
                data = response.json()
            if str(data.get("code")) != "200":
                return False
            node = data.get("data") or {}
            for item in [*(node.get("collection") or []), *(node.get("video") or [])]:
                text = " ".join(str(item.get(key) or "") for key in ("title", "file_name", "file_path"))
                if keyword.upper() in find_serial_numbers(text):
                    return True
            return False
        except Exception as exc:
            logger.warning(f"极影视查询失败：{type(exc).__name__}")
            return False
