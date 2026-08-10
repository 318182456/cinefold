"""JavBus 抓取。

页面结构比 javdb 简单，可直接用番号拼详情页地址。
"""
from __future__ import annotations

import os

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import ActorInfo, CodeInfo, SiteClient, join_list
from app.utils import get_true_code

HOST = "https://www.javbus.com"


class Bus:
    name = "javbus"

    def __init__(self, host: str = "", cookie: str = ""):
        self.client = SiteClient(
            host or HOST,
            cookie or os.getenv("JAVBUS_COOKIE", ""),
            interval=1.5,
        )

    def crawler_original(self, code: str) -> CodeInfo | None:
        """javbus 的详情页就是 /番号，不需要先搜索。"""
        normalized = get_true_code(code)
        if not normalized:
            return None
        html = self.client.get(f"/{normalized}")
        return self.html_to_code(html, normalized) if html else None

    def search_actor(self, name: str) -> ActorInfo | None:
        html = self.client.get(f"/searchstar/{name}")
        if not html:
            return None
        actors = self.html_to_actors(html)
        return actors[0] if actors else None

    def crawler_new(self, page: int = 1) -> list[str]:
        """首页新片列表，返回番号。"""
        path = "/" if page <= 1 else f"/page/{page}"
        html = self.client.get(path)
        return self.html_to_codes(html) if html else []

    def html_to_codes(self, html: str) -> list[str]:
        """从列表页提取番号。"""
        try:
            doc = PyQuery(html)
            codes = []
            for item in doc("#waterfall .item").items():
                # 番号在 photo-info 的最后一个 date 标签里
                found = ""
                for date_node in item(".photo-info date").items():
                    text = (date_node.text() or "").strip()
                    normalized = get_true_code(text)
                    if normalized:
                        found = normalized
                        break
                if found:
                    codes.append(found)
            return list(dict.fromkeys(codes))
        except Exception as exc:
            logger.debug(f"[javbus] 解析列表页失败: {exc}")
            return []

    # ------------------------------------------------------------------
    def html_to_code(self, html: str, code: str = "") -> CodeInfo | None:
        try:
            doc = PyQuery(html)
            info = CodeInfo(code=get_true_code(code))

            title = (doc("h3").eq(0).text() or "").strip()
            # 标题以番号开头，去掉后是真正的片名
            if title.upper().startswith(info.code.upper()):
                title = title[len(info.code):].strip()
            info.title = title

            for item in doc(".info p").items():
                text = (item.text() or "").strip()
                if "：" not in text and ":" not in text:
                    continue
                label, _, value = text.replace(":", "：").partition("：")
                label, value = label.strip(), value.strip()

                if "識別碼" in label or "识别码" in label:
                    info.code = get_true_code(value) or info.code
                elif "發行日期" in label or "发行日期" in label:
                    info.release_date = value
                elif "長度" in label or "长度" in label:
                    info.duration = value
                elif "製作商" in label or "制作商" in label:
                    info.producer = value
                elif "發行商" in label or "发行商" in label:
                    info.publisher = value
                elif "系列" in label:
                    info.series = value

            genres = [a.text() for a in doc(".genre a").items() if a.text()]
            # 演员链接指向 /star/，与类别链接区分
            casts = [a.text() for a in doc('.star-name a').items() if a.text()]
            info.genres = join_list(g for g in genres if g not in casts)
            info.casts = join_list(casts)

            cover = doc(".bigImage img").attr("src") or ""
            info.banner = self._absolute(cover)
            info.poster = info.banner

            samples = [
                self._absolute(node.attr("href") or node.attr("src") or "")
                for node in doc("#sample-waterfall a.sample-box").items()
            ]
            info.still_photo = join_list(samples)

            return info if info.code else None
        except Exception as exc:
            logger.debug(f"[javbus] 解析详情页失败: {exc}")
            return None

    def html_to_actors(self, html: str) -> list[ActorInfo]:
        try:
            doc = PyQuery(html)
            actors = []
            for item in doc("#waterfall .item").items():
                photo = item("img").attr("src") or ""
                name = (item("img").attr("title") or item(".photo-info span").text() or "").strip()
                if name:
                    actors.append(ActorInfo(name=name, photo=self._absolute(photo)))
            return actors
        except Exception as exc:
            logger.debug(f"[javbus] 解析演员列表失败: {exc}")
            return []

    def _absolute(self, url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{self.client.host}{url}"
        return url
