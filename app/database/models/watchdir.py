"""监控目录配置。

一个监控目录 = 「源目录 → 媒体库子目录」的一条同步规则。目录里的文件增删
由 watchdog 实时捕获，同步成媒体库侧硬链接的增删。

为什么落库而不是写在 .env：
- 数量不定，用户随时增删，改配置文件要重启
- 每条规则各有开关（reverse_delete / recursive），env 里表达不了
- 需要记录上次扫描时间、文件数，供页面展示

与 media_link 的分工：这张表存「规则」，media_link 存「规则产生的结果」。
一条规则删掉时可以选择保留已建立的关联，两者生命周期独立。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class WatchDir(DBBase):
    __tablename__ = "watch_dir"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 源目录绝对路径。唯一 —— 同一目录配两条规则会互相打架
    source_dir: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    # 媒体库子目录名（相对 MEDIALINK_LIBRARY_PATH）。留空则直接放在库根下
    target_subdir: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 是否递归子目录。关掉则只同步源目录第一层
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 反向删除：媒体库侧文件消失时，连带删源文件与种子。
    # 默认关闭 —— 正向（源侧删除→删链接）是安全的，反向会删掉占空间的源文件，
    # 不可恢复，必须由用户逐个目录显式放开
    reverse_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # code 前缀。留空则直接用文件名（不含扩展名）作 code
    code_prefix: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # --- 运行状态，仅供页面展示 ---
    last_scan_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 上次全量对账时目录里的视频文件数
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_dir": self.source_dir,
            "target_subdir": self.target_subdir,
            "name": self.name,
            "enabled": self.enabled,
            "recursive": self.recursive,
            "reverse_delete": self.reverse_delete,
            "code_prefix": self.code_prefix,
            "last_scan_time": self.last_scan_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.last_scan_time else "",
            "file_count": self.file_count,
            "last_error": self.last_error or "",
            "create_time": self.create_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.create_time else "",
        }
