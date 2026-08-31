"""演员与厂牌的画像聚合。

模型没看过影片，但库里躺着大量同一演员、同一厂牌的历史作品。把这些
作品的类别标签聚合起来，就是有据可查的画像：

    某演员的 30 部作品里 22 部带「巨乳」 ──> 身材这一项有依据了
    某厂牌的作品普遍带「ドキュメンタリー」──> 拍摄风格有依据了

这比让模型凭番号编要靠谱得多 —— 每一条都能追溯到具体多少部作品。
所以聚合结果里连"多少部里出现过多少次"一起带上，提示词据此要求模型
只复述高频标签，低频的当噪声丢掉。

导演维度暂时做不了：抓取侧（modules/ladysite）压根没采集导演字段，
库里也没有这一列。厂牌与系列是同一类信号里现成能用的那部分 ——
同一厂牌的机位、剪辑、企划套路本来就高度一致，先用它顶上。
真要按导演区分，得先在数据源那侧把字段抓回来。
"""
from __future__ import annotations

import re
from collections import Counter

from loguru import logger
from sqlalchemy import select

from app.database.models import Code
from app.database.session import session_scope

# 标签分隔符各站不一，与 reviewai._count_casts 同源
_SPLIT = re.compile(r"[,，、/|]+")

# 画像最多取几个高频标签。再多就成了标签墙，模型反而抓不住重点
TOP_TAGS = 8

# 一个标签至少要在这么多部作品里出现过才算数。只出现一次的多半是
# 那一部的特例，甚至是刮削错的脏标签，不能拿来概括一个人/一个厂牌
MIN_HITS = 2

# 聚合时最多回看多少部作品。高产演员有几百部，全查回来只是拖慢，
# 高频标签在前几十部里就已经稳定了
MAX_WORKS = 60


def _split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in _SPLIT.split(raw) if t.strip()]


def _aggregate(rows: list[str | None], total: int) -> list[dict]:
    """把一组作品的 genres 聚成 [{tag, hits, total}]，按命中数降序。"""
    counter: Counter[str] = Counter()
    for raw in rows:
        # 同一部里重复出现的标签只算一次，否则刮削重复会把权重灌上去
        for tag in set(_split_tags(raw)):
            counter[tag] += 1

    return [
        {"tag": tag, "hits": hits, "total": total}
        for tag, hits in counter.most_common(TOP_TAGS)
        if hits >= MIN_HITS
    ]


def actor_profile(casts: str | None) -> list[dict]:
    """按出演演员聚合画像。返回每个演员的高频标签。

    只看该演员在本库里的其它作品 —— 当前这部自己的标签由调用方单独
    传给模型，不该混进画像里充当"历史证据"。
    """
    names = _split_tags(casts)
    if not names:
        return []

    out: list[dict] = []
    with session_scope() as session:
        for name in names:
            # casts 是拼接字符串，只能 LIKE。演员名不长，前后各加分隔
            # 容错太复杂，这里接受少量误匹配 —— 聚合本身有 MIN_HITS
            # 兜底，混进来一两部不影响高频标签
            rows = session.scalars(
                select(Code.genres)
                .where(Code.casts.like(f"%{name}%"))
                .where(Code.genres.isnot(None))
                .order_by(Code.release_date.desc())
                .limit(MAX_WORKS)
            ).all()

            tags = _aggregate(list(rows), len(rows))
            if tags:
                out.append({"name": name, "works": len(rows), "tags": tags})
    return out


def studio_profile(producer: str | None, series: str | None) -> list[dict]:
    """按厂牌与系列聚合画像。拍摄手法这一维靠它顶。"""
    out: list[dict] = []
    with session_scope() as session:
        for label, column, value in (
            ("厂牌", Code.producer, producer),
            ("系列", Code.series, series),
        ):
            if not value:
                continue
            rows = session.scalars(
                select(Code.genres)
                .where(column == value)
                .where(Code.genres.isnot(None))
                .order_by(Code.release_date.desc())
                .limit(MAX_WORKS)
            ).all()

            tags = _aggregate(list(rows), len(rows))
            if tags:
                out.append({
                    "kind": label, "name": value,
                    "works": len(rows), "tags": tags,
                })
    return out


def build_profile(meta: dict) -> dict:
    """给一部作品拼出可用的画像证据。查库失败不该拖垮生成。"""
    try:
        return {
            "actors": actor_profile(meta.get("casts")),
            "studios": studio_profile(
                meta.get("producer"), meta.get("series")
            ),
        }
    except Exception as exc:
        logger.warning(f"[影评] 聚合画像失败，将只按当前元数据生成: {exc}")
        return {}
