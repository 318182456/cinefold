"""延迟删除的扣留记录。

文件从监控目录消失时不立刻删链接，先在这张表里扣留一段时间。宽限期内
如果同 inode 的文件在别处出现，说明那是一次移动/改名，不是删除 ——
改 media_link 的路径即可，硬链接和 inode 都不用动，Emby 侧完全无感知。

为什么必须落库而不是放内存：宽限期通常是几十分钟，容器重启很常见。
状态存内存时重启就丢，重启后第一次对账会把所有扣留中的文件当成真删除，
一次性删掉一批 —— 那正是延迟删除想避免的事。

inode 是判定「同一份数据」的依据。移动不改 inode，这是文件系统保证的；
路径会变，文件名会变，内容也可能被改写，只有 inode 撑得住。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class PendingDelete(DBBase):
    __tablename__ = "pending_delete"

    # 媒体库侧的链接路径，与 media_link.link_path 对应
    link_path: Mapped[str] = mapped_column(String(500), primary_key=True)

    # 归属的监控规则。规则删掉后这条扣留也就没意义了
    watch_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)

    code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 消失前记录的 inode / device。移动判定全靠这两个值比对
    inode: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    device: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 哪一侧消失的：source（源目录里没了）/ library（媒体库里没了）。
    # 两侧的后续动作不同，必须区分
    side: Mapped[str] = mapped_column(String(16), nullable=False, default="source")

    # 首次发现消失的时间。宽限期从这里算
    detected_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )

    def to_dict(self) -> dict:
        return {
            "link_path": self.link_path,
            "watch_id": self.watch_id,
            "code": self.code,
            "source_path": self.source_path,
            "inode": self.inode,
            "device": self.device,
            "side": self.side,
            "detected_time": self.detected_time.strftime("%Y-%m-%d %H:%M:%S")
            if self.detected_time else "",
        }
