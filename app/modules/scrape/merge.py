"""多源元数据合并。

既有的 ladysite.get_code_detail 是「谁先返回用谁」（first-wins）：快，
但赢的那个站字段稀疏时就将就了 —— javlibrary 常有评分没剧照，
javbus 有剧照没评分，先到的那个决定了最终质量。

刮削对字段完整度的要求比订阅高得多：订阅只需要标题和封面能在列表页
显示，刮削的产物要长期躺在媒体库里给 Emby 读，缺演员缺系列就是永久缺。
所以这里改成「全抓完再逐字段挑最好的」。

代价是必须等最慢的站（first-wins 只等最快的）。只在刮削路径上用，
订阅那条路仍走 get_code_detail —— 那边一天几百个番号，等不起。

挑选规则按字段类型分四种：
    先到先得   日期、厂牌这类"有就够了"的，按站点优先级取第一个非空
    取最长     简介、类别、时长，长的通常是更全的那份
    取最多     演员、剧照，按逗号分隔的元素个数比
    标题       先比语言（中文优先）再比长度，见 _better
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from loguru import logger

# 各字段的挑选策略。没列出的字段按 first 处理
#
# longest 用在「内容越多越好」的字段上：
#   genres        逗号分隔的列表，长的那份条目多
#   duration      有的站只给"120"，有的给"120分钟"，长的信息全
#
# 标题不在这里 —— 它要先比语言，见 _TITLE_FIELDS 与 _better
#
# 不能对 release_date 用 longest：格式长短与正确性无关，
# "2024-03-15" 与 "2024/03/15" 谁长谁短纯看格式
_LONGEST_FIELDS = {
    "genres", "casts", "duration", "series",
    # 简介取最长：目前只有 airav 给，但将来多一个源时，长的那份
    # 通常是完整版（有的站只放第一句当摘要）
    "outline",
}

# 标题字段。单独一类，因为要先比语言再比长度（见 _better）
_TITLE_FIELDS = {"title", "cn_title"}

# 假名。中日共用汉字，靠汉字分不出语言，但假名是日文独有的 ——
# 有假名必是日文，没假名而有汉字就当中文。
#
# 不用「有汉字就算中文」：日文标题里汉字比例很高，那样判所有日文
# 标题都会被当成中文
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
# 汉字
_HAN = re.compile(r"[一-鿿]")


def _has_chinese(value: object) -> bool:
    """这段文本看着像中文吗（而非日文）。

    判据：有汉字且没有假名。混排（中文标题里带个日文人名）按有假名算
    日文 —— 那种标题多半整体就是日文，只是恰好汉字多。
    """
    text = str(value or "")
    return bool(_HAN.search(text)) and not _KANA.search(text)


# 逗号分隔的列表字段，按元素个数比而不是字符串长度 ——
# 一个长名字的演员会让单人的那份比双人的更"长"
_COUNT_FIELDS = {"genres", "casts", "still_photo"}

# 数值字段取最大值没有意义（评分不是越高越准），按 first 处理
_FIRST_FIELDS = {"star", "release_date", "publisher", "producer"}


@dataclass
class SourceResult:
    """一个站点返回的结果。"""
    site: str = ""
    detail: dict | None = None
    elapsed: float = 0.0
    error: str = ""


def _count_items(value: str) -> int:
    """逗号分隔字段的元素个数。"""
    return len([x for x in str(value or "").split(",") if x.strip()])


def _better(field: str, current: object, candidate: object) -> bool:
    """candidate 比 current 更适合放进 field 吗。

    current 为空时任何非空的都更好，这是最常见的情况。
    """
    if candidate in (None, "", 0):
        return False
    if current in (None, "", 0):
        return True

    if field in _COUNT_FIELDS:
        return _count_items(candidate) > _count_items(current)

    if field in _TITLE_FIELDS:
        # 标题不能纯比长度：日文标题普遍比中文长（实测 SSIS-001 的
        # 日文标题 55 字、中文 40 字），比长度会让日文永远赢，
        # 中文媒体库里显示的却全是日文 —— 与「优先中文」的预期相反。
        #
        # 所以先比语言：有中文的胜出；同为中文（或同为非中文）才比长度
        current_cn = _has_chinese(current)
        candidate_cn = _has_chinese(candidate)
        if candidate_cn != current_cn:
            return candidate_cn
        return len(str(candidate)) > len(str(current))

    if field in _LONGEST_FIELDS:
        return len(str(candidate)) > len(str(current))
    # first 策略：已经有值就不换
    return False


def merge_details(results: list[SourceResult], code: str = "") -> dict:
    """把多个站点的结果合并成一份。

    results 的顺序即站点优先级，first 策略的字段按这个顺序取。
    """
    merged: dict = {}
    # 每个字段最终来自哪个站，只为日志 —— 用户报"演员不对"时能直接
    # 看出该去哪个源查，否则只能一个站一个站试
    origin: dict[str, str] = {}

    for result in results:
        if not result.detail:
            continue
        for field, value in result.detail.items():
            if _better(field, merged.get(field), value):
                merged[field] = value
                origin[field] = result.site

    if merged and code:
        summary = ", ".join(
            f"{f}←{s}" for f, s in sorted(origin.items())
            if f in ("title", "casts", "genres", "still_photo", "star", "banner")
        )
        logger.info(f"[{code}] 多源合并完成，{len(merged)} 项字段（{summary}）")
    return merged


def fetch_merged(code: str, timeout: float = 25.0) -> dict:
    """抓全部可用站点并合并。抓不到返回空字典。

    与 get_code_detail 的区别是不提前返回 —— 全部站点都要等，
    因为要比较字段质量。超时的站点自然被排除（它的 future 拿不到结果）。
    """
    if not code:
        return {}

    from app.modules import ladysite

    sites = ladysite._sites_for_code(code)
    if not sites:
        logger.warning(f"[{code}] 没有可用数据源，检查数据源开关与番号规则")
        return {}

    logger.info(f"[{code}] 多源刮削，检索 {len(sites)} 个源：{', '.join(sites)}")
    started = time.perf_counter()

    def fetch(site: str) -> SourceResult:
        begin = time.perf_counter()
        try:
            detail = ladysite._fetch_detail(site, code)
            return SourceResult(site, detail, time.perf_counter() - begin)
        except Exception as exc:
            return SourceResult(site, None, time.perf_counter() - begin, str(exc))

    workers = min(len(sites), ladysite.MAX_PARALLEL_SITES)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, name) for name in sites]
        results: list[SourceResult] = []
        for future in futures:
            try:
                # 单站超时不拖垮整轮：剩余预算耗尽后后面的站直接算超时
                remaining = max(0.5, timeout - (time.perf_counter() - started))
                results.append(future.result(timeout=remaining))
            except Exception as exc:
                logger.debug(f"[{code}] 某个源未在预算内返回: {exc}")

    hit = [r for r in results if r.detail]
    logger.info(
        f"[{code}] {len(hit)}/{len(sites)} 个源有结果，"
        f"耗时 {time.perf_counter() - started:.1f}s"
    )
    if not hit:
        return {}
    return merge_details(results, code)


def pick_best_image(candidates: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    """从多个来源的封面里挑最清晰的一张。

    候选是 (来源说明, 图片内容)。按像素面积挑 —— 同一张封面各站给的
    尺寸差别很大（缩略图 300px 宽 vs 原图 800px），面积最大的那份最清晰。

    不用文件体积比：JPEG 质量参数会让小尺寸高质量的图比大尺寸低质量的
    更大，那样会挑错。解不出尺寸的候选退回按体积比，总比丢掉好。
    """
    from io import BytesIO

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        Image = None  # type: ignore[assignment]

    best_name, best_data, best_score = "", b"", -1
    for name, data in candidates:
        if not data:
            continue
        score = -1
        if Image is not None:
            try:
                with Image.open(BytesIO(data)) as image:
                    width, height = image.size
                score = width * height
            except Exception:
                score = -1
        if score < 0:
            # 尺寸读不出，退回体积。除以 1000 让它不至于压过真实面积分数
            score = len(data) // 1000
        if score > best_score:
            best_name, best_data, best_score = name, data, score

    if best_name:
        logger.debug(f"[刮削] 封面选用 {best_name}（评分 {best_score}）")
    return best_name, best_data
