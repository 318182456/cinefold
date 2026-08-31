"""AI 生成的影评要点。

按番号存一份。落库而不是每次现生成，理由有三：

- 每次生成都是一次 AI 请求，慢且花钱。同一部片的要点不会变
- NFO 会被刮削工具重刮覆盖，覆盖后要能原样补回来，不能只写在文件里
- 页面要能看到生成了什么、不满意能重生成，得有个地方读

要点字段分开存而不是塞一段文本：Emby 简介、页面展示、以后可能的筛选，
各要的形态不同，拼装交给渲染层（services/review.py），存的是结构。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class Review(DBBase):
    __tablename__ = "review"

    # 一个番号一条。番号本身就是主键，重生成走覆盖
    code: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 出演人数。0 表示没数出来（casts 为空且模型也没给）
    cast_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 身材描述。空表示证据不足 —— 宁可不写也不让模型编，
    # 见 modules/review/reviewai.py 顶部
    body_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 拍摄风格。同样空表示证据不足。目前由厂牌/系列画像推出，
    # 库里没有导演字段
    style: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 看点要点，换行分隔。列表存成文本是因为只用于展示，
    # 从没有按单条要点查询的需求，拆表纯属过度设计
    highlights: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 简评正文
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 写进 NFO 了没有。刮削工具重刮会把 NFO 冲掉，补写任务据此回填。
    # 空表示还没写过
    nfo_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )
