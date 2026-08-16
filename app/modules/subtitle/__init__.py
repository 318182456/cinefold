"""字幕抓取聚合入口。

对上层只暴露 `search(code)` 与 `SubtitleItem`，站点差异全部收在各 parser 里。
站点失败时自动尝试下一个，与 ladysite 的取舍一致。

只认简体中文：JAV 字幕站上「中文」经常混着繁体与机翻英文，
挑不出简体就宁可不放文件 —— 媒体库里出现看不懂的字幕比没有字幕更糟。
"""
from __future__ import annotations

from importlib import import_module

from loguru import logger

from app.modules.subtitle.base import SubtitleItem
from app.utils import get_true_code

# 抓取顺序。subtitlecat 收录最全且无需登录，作主力；
# GitHub 字幕仓库按番号命名、地址稳定，作它跑路时的兜底
SUBTITLE_SITES: tuple[str, ...] = ("subtitlecat", "subtitlegh")

# key → (模块, 类名)。与 ladysite 一样延迟导入，避免把 pyquery
# 的解析开销拉进那些根本不抓字幕的调用路径
_SITE_CLASSES: dict[str, tuple[str, str]] = {
    "subtitlecat": ("subtitlecat", "SubtitleCat"),
    "subtitlegh": ("github", "GithubSubtitle"),
}


def _build(key: str):
    """按 key 建站点实例。源被停用或未配置地址时返回 None。"""
    entry = _SITE_CLASSES.get(key)
    if entry is None:
        return None
    module_name, class_name = entry
    try:
        module = import_module(f"app.modules.subtitle.{module_name}")
        return getattr(module, class_name)()
    except Exception as exc:
        logger.debug(f"[字幕] 站点 {key} 初始化失败: {exc}")
        return None


def search(code: str) -> SubtitleItem | None:
    """按番号找一条简体中文字幕。找不到返回 None。

    站点顺序即优先级，命中即返回 —— 字幕不像封面，多抓几个版本没有意义，
    反而要在目录里堆出一串同名文件。
    """
    code = get_true_code(code)
    if not code:
        return None

    for key in SUBTITLE_SITES:
        site = _build(key)
        if site is None:
            continue
        try:
            item = site.search(code)
        except Exception as exc:
            # 单站不通不该影响后面的站
            logger.warning(f"[字幕] {key} 抓取 {code} 失败: {exc}")
            continue
        if item is not None and item.content:
            logger.info(f"[字幕] {code} 命中 {key}（{item.filename}）")
            return item

    logger.info(f"[字幕] {code} 未找到简体中文字幕")
    return None


__all__ = ["SubtitleItem", "search", "SUBTITLE_SITES"]
