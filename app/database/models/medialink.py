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

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
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

    # 向下载器反查种子连续失败了几次。手工拷进来的文件、种子早被删掉的文件
    # 永远查不到，靠这个计数把它们降频，别每轮对账都拉一次全量种子列表
    torrent_miss: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 上次反查的时间。降频判断用，空表示还没查过
    torrent_probe_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # 首次发现源文件消失的时间。空表示源文件还在（或从没查过）。
    #
    # 记在这里而不是复用 pending_delete.detected_time，是因为那张表的记录
    # 撑不到需要它的时候：扣留只在开了反向删除的规则下登记，宽限期一满就
    # 连记录带扣留一起清掉。而「源文件没了、Emby 里还在」的关联恰恰是那些
    # 永远走不到删除的 —— 规则没开反向删除、或压根不归任何规则管，
    # 它们在扣留表里没有任何痕迹，时间只能自己存一份。
    #
    # 文件回来了要清空（移动/改名会让路径漂，同 inode 的文件仍在），
    # 否则一次误报会永久留在列表里。
    source_gone_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
