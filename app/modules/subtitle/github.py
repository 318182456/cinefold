"""GitHub 字幕仓库。

社区维护的番号字幕仓库，文件按番号命名。作 subtitlecat 的兜底：
更新滞后，但地址稳定、不过盾、不限速，站点跑路时它还在。

仓库地址配在数据源页面上（key=subtitlegh），形如
`https://raw.githubusercontent.com/<owner>/<repo>/<branch>`。
换仓库不必改代码 —— 这类仓库归档、转手都很常见。
"""
from __future__ import annotations

from loguru import logger

from app.modules.ladysite.base import SiteClient
from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES,
    MIN_SUBTITLE_BYTES,
    SUBTITLE_SUFFIXES,
    SubtitleItem,
    decode_subtitle,
    is_simplified_chinese,
    looks_like_subtitle,
)

# 默认指向一个收录较全的社区仓库
HOST = "https://raw.githubusercontent.com/CnSubtitles/subtitles/main"

# 仓库里常见的目录布局。番号 SSIS-001 依次试：
#   /SSIS-001.srt          平铺
#   /SSIS/SSIS-001.srt     按厂牌前缀分目录
_LAYOUTS = ("{code}", "{prefix}/{code}")


class GithubSubtitle:
    name = "subtitlegh"

    def __init__(self, host: str = ""):
        if not host:
            client = SiteClient.from_source(self.name)
            if client is not None:
                self.client = client
                return

        # raw.githubusercontent.com 不限速，但仍留一点间隔避免触发风控
        self.client = SiteClient(host or HOST, interval=0.5)

    def search(self, code: str) -> SubtitleItem | None:
        prefix = code.split("-")[0] if "-" in code else code

        for layout in _LAYOUTS:
            stem = layout.format(code=code, prefix=prefix)
            for suffix in SUBTITLE_SUFFIXES:
                content = self._fetch(f"/{stem}{suffix}")
                if not content:
                    continue
                if not is_simplified_chinese(content):
                    logger.debug(f"[字幕] {code} 仓库命中但非简体，跳过")
                    continue
                return SubtitleItem(
                    code=code,
                    title=f"{code}{suffix}",
                    site=self.name,
                    content=content,
                    suffix=suffix,
                )
        return None

    def _fetch(self, path: str) -> str:
        """取一个文件。不存在（404）时返回空串。

        与 subtitlecat 同理，编码要按字节猜，不能走 SiteClient.get。
        """
        import httpx

        from app.core.config import get_settings

        settings = get_settings()
        url = f"{self.client.host}{path}"
        try:
            with httpx.Client(
                timeout=15.0,
                follow_redirects=True,
                proxy=settings.proxy or None,
                verify=False,
                trust_env=False,
            ) as client:
                response = client.get(url)
                if response.status_code == 404:
                    return ""
                response.raise_for_status()
                raw = response.content
        except Exception as exc:
            logger.debug(f"[字幕] 仓库取 {path} 失败: {exc}")
            return ""

        if not MIN_SUBTITLE_BYTES <= len(raw) <= MAX_SUBTITLE_BYTES:
            return ""

        text = decode_subtitle(raw)
        return text if looks_like_subtitle(text) else ""
