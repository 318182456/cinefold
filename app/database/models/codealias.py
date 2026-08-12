"""哈希 code 与原始文件名的对应关系。

code 由文件名生成，而 code 列是 varchar(64)。日文长片名轻松超过 64 字符，
这类文件的 code 只能换成文件名哈希（见 watchdir.make_code）。代价是 code
不再有可读性：列表页和日志里看到 `short-9f2a…` 认不出是哪部片子。

这张表把丢掉的那部分补回来 —— 哈希 → 原文件名。只有走哈希的 code 才会
在这里留一条，短文件名的 code 本身就是文件名，不必记。

不并进 media_link：那张表按 link_path 一行一条，同一 code 可能有多份链接
（转种、多集），文件名会重复存好几遍。这里按 code 做主键，一部片子一条。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import DBBase


class CodeAlias(DBBase):
    __tablename__ = "code_alias"

    # 哈希后的 code，与 media_link.code 对得上
    code: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 原始文件名（不含扩展名）。长度不限，正是因为它放不进 code 才有这张表
    filename: Mapped[str] = mapped_column(Text, nullable=False)

    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
