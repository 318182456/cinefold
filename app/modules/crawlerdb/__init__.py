"""外部爬虫库（Immortal）导入。

从 Immortal 的 PostgreSQL 读番号情报和演员，补进本地 Code / Actor 表。
只读对方的库，不写、不建表。

落库复用 services.cache_remote_codes 的语义：只补空字段、不动 status。
本地已翻译的 cn_title、已刮削的数据不会被冲掉，导入也不会让番号
自动进下载流程。
"""
from __future__ import annotations

from app.modules.crawlerdb.importer import (
    CrawlerDBError,
    fetch_casts,
    fetch_movies,
    import_all,
    import_casts,
    import_movies,
    probe_schema,
    test_connection,
)

__all__ = [
    "CrawlerDBError",
    "fetch_casts",
    "fetch_movies",
    "import_all",
    "import_casts",
    "import_movies",
    "probe_schema",
    "test_connection",
]
