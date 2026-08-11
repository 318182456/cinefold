"""媒体文件与下载文件的硬链接关联。

刮削工具（MDCng 等）完成后会在媒体库里为源文件建硬链接，Emby 扫到的是
链接侧的路径。Emby 删除影片时只带得到链接侧信息，要反查回源文件和种子，
就得把这条对应关系存下来。

关联的锚点是 inode 而不是路径：
- 硬链接天然共享 inode，只要同一文件系统，inode 相同即同一份数据
- 刮削命名规则随时可能改，路径会漂，inode 不会

种子 hash 不存在这里。History 表已经是 hash → code → save_path，
同一 code 的多条记录天然表达「转种」（一个文件多个种子），
按 code 去 History 查即可，不重复存一份。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class MediaLink(DBBase):
    __tablename__ = "media_link"

    # 链接侧路径（媒体库里的那份）。Emby 删除事件按这个反查，故作主键
    link_path: Mapped[str] = mapped_column(String(500), primary_key=True)

    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 源文件路径（下载器里的那份）
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 源文件 inode。跨文件系统时为空，此时只能退回按路径匹配
    inode: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    # 源文件所在设备号。inode 仅在同一设备内唯一，跨卷比对必须带上
    device: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
