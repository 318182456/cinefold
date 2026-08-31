"""JavSub.ai 解析。

流程：
1. 搜索番号（/search/?q=code），站点命中单部片时直接 302 重定向到详情页，
   多条匹配时返回卡片列表，通过 text_contains_code 精确匹配目标番号。
2. 详情页解析 .sub-row 中的可用字幕下载链接（Free 与 Demo），按语言偏好排序：
   简体中文 > 兜底语言 > 繁体中文（粤语直接排除）。
3. 下载后经过编码探测与 as_simplified_chinese 统一规整为简体中文。
"""
from __future__ import annotations

import re

from loguru import logger
from pyquery import PyQuery

from app.modules.ladysite.base import SiteClient, absolute_url, text_contains_code
from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES,
    MIN_SUBTITLE_BYTES,
    SubtitleItem,
    as_simplified_chinese,
    decode_subtitle,
    looks_like_subtitle,
    pick_suffix,
)

HOST = "https://javsub.ai"

# 语言标注匹配
_ZH_HINT = re.compile(
    r"chinese|中文|简体|簡體|zh[-_]?(cn|hans)?", re.IGNORECASE
)
_ZH_EXCLUDE = re.compile(r"cantonese|yue", re.IGNORECASE)
_ZH_TRADITIONAL = re.compile(
    r"traditional|繁體|繁体|zh[-_]?(tw|hk|hant)", re.IGNORECASE
)

# 单番号最多尝试的候选链接数
MAX_CANDIDATES = 6


class JavSub:
    name = "javsub"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        # 默认启用 bypass_first，Cloudflare 站点直连易被拦截
        self.client = SiteClient(host or HOST, interval=2.0, bypass_first=True)

    def search(self, code: str) -> SubtitleItem | None:
        detail_path, title, detail_html = self._find_detail(code)
        if not detail_path:
            return None

        html = detail_html or self.client.get(detail_path)
        if not html:
            return None

        for url in self._candidate_links(html, detail_path)[:MAX_CANDIDATES]:
            content = self._download(url, referer=detail_path)
            if not content:
                continue
            # 繁体转为简体，非中文（日文、英文等）返回空串跳过
            content = as_simplified_chinese(content)
            if not content:
                logger.debug(f"[字幕] {code} javsub 候选不是中文，跳过: {url}")
                continue
            return SubtitleItem(
                code=code,
                title=title or code,
                site=self.name,
                content=content,
                suffix=pick_suffix(url),
            )
        return None

    def _find_detail(self, code: str) -> tuple[str, str, str]:
        """搜索番号，返回 (详情页绝对地址, 标题, 页面HTML)。

        站点对唯一命中常直接 302 跳转到详情页，此时 client.get 返回的就是详情页 HTML；
        若返回列表页，则按卡片逐一比对番号。
        """
        html = self.client.get("/search/", params={"q": code})
        if not html:
            return "", "", ""

        doc = PyQuery(html)

        # 判定返回的是否已经是详情页（含 .sub-row 或 .detail-header）
        title_text = doc("title").text().strip()
        h1_text = doc("h1").text().strip()
        if (doc(".sub-row").length > 0 or doc(".detail-header").length > 0) and (
            text_contains_code(title_text, code) or text_contains_code(h1_text, code)
        ):
            # 尝试从 canonical 或 og:url 取精准详情页地址
            canonical = doc('link[rel="canonical"]').attr("href") or doc(
                'meta[property="og:url"]'
            ).attr("content")
            detail_url = (
                absolute_url(canonical, self.client.host)
                if canonical
                else f"{self.client.host}/subtitles/"
            )
            title = h1_text or title_text or code
            return detail_url, title, html

        # 列表页匹配
        for card in doc(".card, .free-card").items():
            link = card if card.is_("a") else card("a").eq(0)
            href = (link.attr("href") or "").strip()
            if not href:
                continue
            card_title = (
                card(".name").text()
                or card(".fc-name").text()
                or link.text()
                or ""
            ).strip()
            if not text_contains_code(card_title, code) and not text_contains_code(
                href, code
            ):
                continue
            return absolute_url(href, self.client.host), card_title or code, ""

        return "", "", ""

    def _candidate_links(self, html: str, detail_url: str = "") -> list[str]:
        """从详情页提取字幕下载候选链接，按简中 > 兜底 > 繁中排序。"""
        doc = PyQuery(html)
        preferred: list[str] = []
        fallback: list[str] = []
        traditional: list[str] = []

        for row in doc(".sub-row").items():
            lang_text = row(".lang").text().strip()
            label = f"{lang_text} {row.text()}"[:300]
            if _ZH_EXCLUDE.search(label):
                continue

            is_trad = bool(_ZH_TRADITIONAL.search(label))
            is_simp = bool(_ZH_HINT.search(label)) and not is_trad

            # 遍历行内的下载超链接
            for a in row("a").items():
                href = (a.attr("href") or "").strip()
                if not href:
                    continue
                url = absolute_url(href, self.client.host)
                if is_simp:
                    preferred.append(url)
                elif is_trad:
                    traditional.append(url)
                else:
                    fallback.append(url)

        # 检查 trailer-vtt 字幕轨（若包含中文字幕轨）
        for track in doc("track").items():
            srclang = (track.attr("srclang") or "").lower()
            src = (track.attr("src") or "").strip()
            if not src:
                continue
            url = absolute_url(src, self.client.host)
            if srclang in ("zh", "zh-cn", "zh-hans"):
                preferred.append(url)
            elif srclang in ("zh-tw", "zh-hk", "zh-hant"):
                traditional.append(url)
            elif "chinese" in (track.attr("label") or "").lower():
                preferred.append(url)

        seen: set[str] = set()
        ordered = preferred + fallback + traditional
        return [u for u in ordered if not (u in seen or seen.add(u))]

    def _download(self, url: str, referer: str = "") -> str:
        """下载字幕内容并解码。

        若直接请求返回 403 / 拦截，尝试复用 SiteClient.get 或带 Referer 请求。
        """
        import httpx

        from app.core.config import get_settings

        settings = get_settings()
        headers = self.client.headers({
            "Referer": referer or f"{self.client.host}/",
            "Accept": "*/*",
        })

        try:
            with httpx.Client(
                timeout=20.0,
                follow_redirects=True,
                proxy=settings.proxy or None,
                verify=False,
                trust_env=False,
            ) as client:
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    raw = response.content
                    if MIN_SUBTITLE_BYTES <= len(raw) <= MAX_SUBTITLE_BYTES:
                        text = decode_subtitle(raw)
                        if looks_like_subtitle(text):
                            return text
        except Exception as exc:
            logger.debug(f"[字幕] javsub 直连下载 {url} 失败: {exc}")

        # 若直连受限，尝试通过 SiteClient (可走 bypass 绕过服务) 获取
        try:
            text = self.client.get(url, headers=headers)
            if text and looks_like_subtitle(text):
                return text
        except Exception as exc:
            logger.debug(f"[字幕] javsub 客户端下载 {url} 失败: {exc}")

        return ""

