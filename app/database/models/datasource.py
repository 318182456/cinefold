"""数据源配置。

内置源的地址、开关、节流由这张表管理，页面上可改。
把地址落库是因为这些站换域名很频繁，改配置比改代码快。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class DataSource(DBBase):
    __tablename__ = "datasource"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 内置源的标识，与 SOURCES 里的 key 对应；自定义源随意取名
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 同 host 两次请求的最小间隔秒数，0 表示用全局默认
    interval: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 越小越优先
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    cookie: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 直连必被拦的站点置 True，省掉一次必然 403 的直连
    bypass_first: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 番号路由规则。控制"哪些番号该问这个源"，抓取时据此跳过必然 404 的
    # 组合（拿 SSIS-001 去问只收 FC2 的站是白跑一次请求 + 一个并发位）。
    # 形如 "only:FC2,SIRO" / "skip:MD,MDX" / 两条用 ; 分隔。
    # 为空表示不限制，什么番号都问。语法见 sources.parse_code_rule
    code_rule: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 软删除标记。内置源删掉后不能真删行，否则 sync_builtin_sources()
    # 下次启动就把它补回来了；置 True 让 sync 跳过，也留出恢复的余地
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- 连通性测试结果，仅供页面展示 ---
    # 空表示还没测过；"ok" / "fail" / "blocked"
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "host": self.host,
            "enabled": self.enabled,
            "interval": self.interval,
            "priority": self.priority,
            # cookie 只回传是否已配置，不外泄内容
            "has_cookie": bool(self.cookie),
            "bypass_first": self.bypass_first,
            "code_rule": self.code_rule or "",
            "deleted": self.deleted,
            "status": self.status or "",
            "status_message": self.status_message or "",
            "checked_time": self.checked_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.checked_time else "",
        }
