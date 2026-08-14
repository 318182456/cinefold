"""种子过滤与排序。

过滤规则来自 DEFAULT_FILTER，排序规则来自 DEFAULT_SORT。
排序键用逗号分隔，`!` 前缀表示反向（降权）。
"""
from __future__ import annotations

import re
from typing import Sequence

from loguru import logger

from app.schemas.torrent import Torrent

# 中文字幕标识
CHINESE_TOKENS = ("中文", "中字", "字幕", "-c", "_c", "ch", "sub", "chinese", "uncensored-c")
CHINESE_RE = re.compile(r"(中文|中字|字幕|chinese|subtitle)", re.IGNORECASE)
# 番号后缀 -C / -CH 是中文字幕版的惯例标记
CHINESE_SUFFIX_RE = re.compile(r"[-_](c|ch|chs|cht)(?:[^a-z0-9]|$)", re.IGNORECASE)

# 无码破解
UC_RE = re.compile(r"(无码|無碼|uncensored|破解|流出|leak)", re.IGNORECASE)

# 高清
UHD_RE = re.compile(r"(4k|8k|2160p|uhd|2160)", re.IGNORECASE)

# VR。体积普遍很大且普通播放器看不了。
# 番号前缀列表补不全（SSR 这类看不出是 VR 的系列很多），主要靠标题里的
# 【VR】8KVR VR専用 这类标记，前缀只作补充
VR_RE = re.compile(
    r"(\bvr\b|[-_ ]vr[-_ ]|\d+kvr|vr専用|vr动画|vr動画|180°|360°|"
    r"\b(?:dsvr|wpvr|vrkm|kmvr|savr|crvr|hunvr|ipvr|mdvr|tmavr|urvrsp|vovs)\b)",
    re.IGNORECASE,
)

SIZE_RE = re.compile(r"^\s*([\d.]+)\s*(gb|mb|g|m)?\s*$", re.IGNORECASE)

# 逐条打被过滤原因，热门番号一次能搜出几十个种子，全打会刷屏
MAX_LOGGED_REASONS = 10


def has_chinese(title: str) -> bool:
    if not title:
        return False
    return bool(CHINESE_RE.search(title) or CHINESE_SUFFIX_RE.search(title))


def has_uc(title: str) -> bool:
    return bool(UC_RE.search(title or ""))


def has_uhd(title: str) -> bool:
    return bool(UHD_RE.search(title or ""))


def has_vr(title: str) -> bool:
    return bool(VR_RE.search(title or ""))


def _parse_size_mb(value: str) -> float:
    """"5GB" / "500" → MB 数值。

    无单位时按 MB 解析，与配置项 min_size/max_size 的语义一致。
    """
    if not value:
        return 0.0
    match = SIZE_RE.match(str(value))
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = (match.group(2) or "mb").lower()
    return number * 1024 if unit in ("gb", "g") else number


def _parse_int(value) -> int:
    """配置里的数字项。留空、非法值与 0 一律当作不限。"""
    if value in (None, ""):
        return 0
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _match_keywords(title: str, keywords: str) -> bool:
    """关键词用逗号分隔，任一命中即为 True。"""
    title_lower = (title or "").lower()
    for keyword in (keywords or "").split(","):
        keyword = keyword.strip().lower()
        if keyword and keyword in title_lower:
            return True
    return False


def filter_torrents(
    torrents: Sequence[Torrent], filter_config: dict | None = None
) -> list[Torrent]:
    """按配置过滤种子列表。"""
    config = filter_config or {}
    if not torrents:
        return []

    only_chinese = bool(config.get("only_chinese"))
    only_uc = bool(config.get("only_uc"))
    exclude_uc = bool(config.get("exclude_uc"))
    only_uhd = bool(config.get("only_uhd"))
    exclude_uhd = bool(config.get("exclude_uhd"))
    only_vr = bool(config.get("only_vr"))
    exclude_vr = bool(config.get("exclude_vr"))
    only_free = bool(config.get("only_free"))
    include_keywords = config.get("include_keywords") or ""
    exclude_keywords = config.get("exclude_keywords") or ""
    min_size = _parse_size_mb(config.get("min_size") or "")
    max_size = _parse_size_mb(config.get("max_size") or "")
    min_seeders = _parse_int(config.get("min_seeders"))
    max_seeders = _parse_int(config.get("max_seeders"))

    out: list[Torrent] = []
    # 被挡的原因逐条记下来。"过滤: 5 → 0" 这种汇总看不出该松哪一条，
    # 配置项有十几个，靠猜要试很多轮
    reasons: list[str] = []

    for torrent in torrents:
        title = torrent.title or ""

        # 属性字段可能来源站点没给，用标题兜底推断
        chinese = torrent.chinese or has_chinese(title)
        uc = torrent.uc or has_uc(title)
        uhd = torrent.uhd or has_uhd(title)
        vr = torrent.vr or has_vr(title)

        # 命中即出局。带上实际值 —— 只说"体积不合要求"还得回头翻配置
        if only_chinese and not chinese:
            reason = "only_chinese: 无中文字幕"
        elif only_uc and not uc:
            reason = "only_uc: 非无码"
        elif exclude_uc and uc:
            reason = "exclude_uc: 是无码"
        elif only_uhd and not uhd:
            reason = "only_uhd: 非 4K"
        elif exclude_uhd and uhd:
            reason = "exclude_uhd: 是 4K"
        elif only_vr and not vr:
            reason = "only_vr: 非 VR"
        elif exclude_vr and vr:
            reason = "exclude_vr: 是 VR"
        elif only_free and not torrent.free:
            reason = "only_free: 非免费种"
        elif include_keywords and not _match_keywords(title, include_keywords):
            reason = f"include_keywords: 标题不含 {include_keywords}"
        elif exclude_keywords and _match_keywords(title, exclude_keywords):
            reason = f"exclude_keywords: 标题命中 {exclude_keywords}"
        elif min_size and torrent.size_mb < min_size:
            reason = f"min_size: {torrent.size_mb:.0f}MB < {min_size:.0f}MB"
        elif max_size and torrent.size_mb > max_size:
            reason = f"max_size: {torrent.size_mb:.0f}MB > {max_size:.0f}MB"
        # 做种数太少下不动，太多的往往是老片合集
        elif min_seeders and torrent.seeders < min_seeders:
            reason = f"min_seeders: {torrent.seeders} < {min_seeders}"
        elif max_seeders and torrent.seeders > max_seeders:
            reason = f"max_seeders: {torrent.seeders} > {max_seeders}"
        else:
            # 回填推断结果，后续排序直接用
            torrent.chinese, torrent.uc, torrent.uhd, torrent.vr = chinese, uc, uhd, vr
            out.append(torrent)
            continue

        reasons.append(f"[{torrent.site}] {title[:40]} —— {reason}")

    logger.debug(f"过滤: {len(torrents)} → {len(out)}")
    if reasons:
        # 全被挡掉时升级成 warning：这时候用户面对的是空列表，
        # 日志是唯一能告诉他"该松哪一条"的地方
        log = logger.warning if not out else logger.info
        log(
            f"过滤掉 {len(reasons)} 个种子：\n  "
            + "\n  ".join(reasons[:MAX_LOGGED_REASONS])
            + (f"\n  …另有 {len(reasons) - MAX_LOGGED_REASONS} 条"
               if len(reasons) > MAX_LOGGED_REASONS else "")
        )
    return out


def sort_torrents(
    torrents: Sequence[Torrent],
    sort_rule: str = "",
    site_priority: Sequence[str] | None = None,
) -> list[Torrent]:
    """按规则多级排序，靠前的键优先级更高。

    支持的键：free / chinese / uc / uhd / vr / seeders / size / site
    `!` 前缀表示希望该属性为假（降权），如 `!uhd` 会把非 4K 排前面。
    """
    if not torrents:
        return []

    keys = [k.strip() for k in (sort_rule or "").split(",") if k.strip()]
    if not keys:
        return list(torrents)

    priority = list(site_priority or [])

    def sort_value(torrent: Torrent, key: str) -> float:
        negate = key.startswith("!")
        name = key.lstrip("!")

        if name == "free":
            value = float(torrent.free)
        elif name == "chinese":
            value = float(torrent.chinese)
        elif name == "uc":
            value = float(torrent.uc)
        elif name == "uhd":
            value = float(torrent.uhd)
        elif name == "vr":
            value = float(torrent.vr)
        elif name == "seeders":
            value = float(torrent.seeders)
        elif name == "size":
            value = float(torrent.size_mb)
        elif name == "site":
            # 站点优先级：越靠前分越高；未列出的排最后
            value = float(len(priority) - priority.index(torrent.site)) \
                if torrent.site in priority else 0.0
        else:
            value = 0.0

        return -value if negate else value

    # 降序：分值高的在前
    return sorted(
        torrents,
        key=lambda t: tuple(-sort_value(t, k) for k in keys),
    )
