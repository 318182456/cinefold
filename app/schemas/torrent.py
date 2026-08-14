"""种子数据结构。

字段定义与原项目 BT_URL 自定义源的返回格式保持一致，
外部 BT 源可以直接按这个结构返回 {"data": [...]}。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Torrent:
    id: int = 0
    site: str = ""
    title: str = ""
    size_mb: float = 0.0
    seeders: int = 0
    chinese: bool = False
    uc: bool = False
    uhd: bool = False
    vr: bool = False
    free: bool = False
    download_url: str = ""
    # 站内详情页，仅用于展示
    detail_url: str = ""
    # 关联番号，搜索时回填
    code: str = ""
    # 自定义 BT 源接口自报的站名，仅用于展示。
    # site 必须由 cinefold 自己决定（按站点过滤、排序、反查都认它），
    # 外部接口报什么都不能覆盖它，否则「参与自动下载」这类开关会被绕过。
    source_site: str = ""

    @property
    def display_site(self) -> str:
        """展示用站名：自定义 BT 源标成「BT · 原站名」，其余就是站名本身。"""
        if self.source_site and self.source_site != self.site:
            return f"{self.site} · {self.source_site}"
        return self.site

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "site": self.site,
            "title": self.title,
            "size_mb": round(self.size_mb, 2),
            "seeders": self.seeders,
            "chinese": self.chinese,
            "uc": self.uc,
            "uhd": self.uhd,
            "vr": self.vr,
            "free": self.free,
            "download_url": self.download_url,
            "detail_url": self.detail_url,
            "code": self.code,
            "source_site": self.source_site,
            "display_site": self.display_site,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Torrent":
        return cls(
            id=int(data.get("id") or 0),
            site=str(data.get("site") or ""),
            title=str(data.get("title") or ""),
            size_mb=float(data.get("size_mb") or 0),
            seeders=int(data.get("seeders") or 0),
            chinese=bool(data.get("chinese")),
            uc=bool(data.get("uc")),
            uhd=bool(data.get("uhd")),
            vr=bool(data.get("vr")),
            free=bool(data.get("free")),
            download_url=str(data.get("download_url") or ""),
            detail_url=str(data.get("detail_url") or ""),
            code=str(data.get("code") or ""),
            source_site=str(data.get("source_site") or ""),
        )
