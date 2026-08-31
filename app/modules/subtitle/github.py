"""GitHub 字幕仓库。

社区维护的番号字幕仓库，文件按番号命名。作 subtitlecat 的兜底：
更新滞后，但不过盾、不限速，站点跑路时它还在。

仓库地址配在数据源页面上（key=subtitlegh），形如
`https://raw.githubusercontent.com/<owner>/<repo>/<branch>`。
可以配多个，用逗号或换行分隔，依次试到命中为止：

    https://raw.githubusercontent.com/a/subs/main,
    https://raw.githubusercontent.com/b/subs/master

多个而不是一个，是因为这类仓库归档、转手、改分支名都很常见 ——
只配一个的话它一失效，兜底源就整个空转，而且失效得毫无声响
（每次取都是 404，日志只落在 debug 级）。配了多个时任一命中即返回，
其中一个没了也不影响其余的。

host 字段是 String(255)，一个地址约 60 字符，实际放得下三四个。
"""
from __future__ import annotations

import re

from loguru import logger

from app.modules.ladysite.base import SiteClient
from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES,
    MIN_SUBTITLE_BYTES,
    SUBTITLE_SUFFIXES,
    SubtitleItem,
    as_simplified_chinese,
    decode_subtitle,
    looks_like_subtitle,
)

# 不设内置默认地址。此前默认值指向一个压根不存在的仓库（404），
# 兜底源因此从一开始就在空转，而且看不出来 —— 与其再猜一个名字，
# 不如让「没配地址」显式地等于「这个源没启用」。
HOST = ""

# 仓库里常见的目录布局。番号 SSIS-001 依次试：
#   /SSIS-001.srt          平铺
#   /SSIS/SSIS-001.srt     按厂牌前缀分目录
_LAYOUTS = ("{code}", "{prefix}/{code}")

# 地址分隔符：逗号、分号、换行。页面上一行填多个，或每行一个都认
_SPLIT = re.compile(r"[,;\s]+")


def parse_hosts(raw: str) -> list[str]:
    """把配置里的一串地址拆成列表，按原顺序去重。

    容错到底：多余的空白、末尾斜杠、重复项、空项都清掉。地址是人在
    页面上手填的，格式上挑剔只会让人配不对却不知道为什么。
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT.split(raw or ""):
        host = part.strip().rstrip("/")
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


class GithubSubtitle:
    name = "subtitlegh"

    def __init__(self, host: str = ""):
        raw = host
        interval = 0.5
        if not raw:
            from app.modules.ladysite.sources import get_source

            source = get_source(self.name) or {}
            # 停用的源不该被建出来
            if source.get("enabled", True):
                raw = source.get("host", "") or HOST
                interval = source.get("interval") or 0.5

        self.hosts = parse_hosts(raw)
        # raw.githubusercontent.com 不限速，但仍留一点间隔避免触发风控。
        # 每个仓库一个 client：SiteClient 的节流是按 host 记的，
        # 共用一个实例会让不同仓库互相排队
        self._clients = [
            SiteClient(h, interval=interval) for h in self.hosts
        ]

    def search(self, code: str) -> SubtitleItem | None:
        if not self._clients:
            logger.debug("[字幕] GitHub 字幕库未配置仓库地址，跳过")
            return None

        prefix = code.split("-")[0] if "-" in code else code

        # 仓库外层、布局内层：先把一个仓库试完再换下一个。反过来的话
        # 一个番号最多要发 仓库数 × 布局数 × 后缀数 次请求，且顺序
        # 交错，日志上看不出是哪个仓库有货
        for client in self._clients:
            for layout in _LAYOUTS:
                stem = layout.format(code=code, prefix=prefix)
                for suffix in SUBTITLE_SUFFIXES:
                    content = self._fetch(client, f"/{stem}{suffix}")
                    if not content:
                        continue
                    content = as_simplified_chinese(content)
                    if not content:
                        logger.debug(
                            f"[字幕] {code} 在 {client.host} 命中但不是中文，跳过"
                        )
                        continue
                    logger.debug(f"[字幕] {code} 命中仓库 {client.host}")
                    return SubtitleItem(
                        code=code,
                        title=f"{code}{suffix}",
                        site=self.name,
                        content=content,
                        suffix=suffix,
                    )
        return None

    def _fetch(self, client: SiteClient, path: str) -> str:
        """从一个仓库取一个文件。不存在（404）时返回空串。

        与 subtitlecat 同理，编码要按字节猜，不能走 SiteClient.get。
        """
        import httpx

        from app.core.config import get_settings

        settings = get_settings()
        url = f"{client.host}{path}"
        try:
            with httpx.Client(
                timeout=15.0,
                follow_redirects=True,
                proxy=settings.proxy or None,
                verify=False,
                trust_env=False,
            ) as http:
                response = http.get(url)
                if response.status_code == 404:
                    return ""
                response.raise_for_status()
                raw = response.content
        except Exception as exc:
            logger.debug(f"[字幕] 仓库取 {url} 失败: {exc}")
            return ""

        if not MIN_SUBTITLE_BYTES <= len(raw) <= MAX_SUBTITLE_BYTES:
            return ""

        text = decode_subtitle(raw)
        return text if looks_like_subtitle(text) else ""
