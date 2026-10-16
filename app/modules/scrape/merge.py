"""多源元数据合并。

既有的 ladysite.get_code_detail 是「谁先返回用谁」（first-wins）：快，
但赢的那个站字段稀疏时就将就了 —— javlibrary 常有评分没剧照，
javbus 有剧照没评分，先到的那个决定了最终质量。

刮削对字段完整度的要求比订阅高得多：订阅只需要标题和封面能在列表页
显示，刮削的产物要长期躺在媒体库里给 Emby 读，缺演员缺系列就是永久缺。
所以这里在总超时与首次命中后的补全窗口内合并结果，再逐字段挑选。
订阅那条路仍走 get_code_detail。

挑选规则按字段类型分四种：
    先到先得   日期、厂牌这类"有就够了"的，按站点优先级取第一个非空
    取最长     简介、类别、时长，长的通常是更全的那份
    取最多     演员、剧照，按逗号分隔的元素个数比
    标题       先比语言（中文优先）再比长度，见 _better
"""
from __future__ import annotations

import re
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from loguru import logger

# 超时只能停止等待，不能杀死正在请求网络的 Python 线程。
# 共用有界线程池，避免连续失败时每轮再创建一批悬挂线程。
_WORKERS = 6
_POOL = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="scrape-source")
_SLOTS = threading.BoundedSemaphore(_WORKERS)

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


class MetadataResult(dict):
    """空结果仍保持 false，但可携带失败站点诊断。"""
    def __init__(self, sources):
        super().__init__()
        self.sources = sources


METADATA_FIELDS = {
    "title", "cn_title", "outline", "release_date", "duration", "producer", "publisher",
    "series", "genres", "casts", "star", "banner", "poster", "still_photo", "preview_url",
}
_BAD_TITLES = {
    "just a moment", "access denied", "403 forbidden", "404 not found", "attention required! | cloudflare",
    "无法访问此网站", "this site can't be reached", "this site can’t be reached", "err_connection_refused",
    "err_name_not_resolved", "err_internet_disconnected",
}


def clean_detail(detail: dict, code: str = "") -> dict:
    """丢弃错误页标题、错号结果和非元数据字段，禁止修改订阅状态。"""
    if not isinstance(detail, dict):
        return {}
    if code and detail.get("code"):
        from app.utils import get_true_code
        actual = get_true_code(str(detail["code"])) or str(detail["code"]).upper()
        expected = get_true_code(code) or code.upper()
        if (base_code(actual) or actual) != (base_code(expected) or expected):
            logger.warning(f"[{code}] 丢弃番号不符的刮削结果: {actual}")
            return {}
    result = {k: v for k, v in detail.items() if k in METADATA_FIELDS and v not in (None, "", [])}
    for key, value in list(result.items()):
        if key == "star":
            try:
                score = float(value)
                if not 0 <= score <= 10:
                    result.pop(key)
                else:
                    result[key] = score
            except (ValueError, TypeError):
                result.pop(key)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[key] = str(value).strip()
        elif key in ("genres", "casts", "still_photo") and isinstance(value, list):
            result[key] = ",".join(str(x).strip() for x in value if isinstance(x, str) and x.strip())
        else:
            result.pop(key)
    for key in ('casts', 'genres', 'series', 'publisher', 'producer'):
        if key in result:
            values = [v.strip() for v in result[key].split(',')]
            result[key] = ','.join(dict.fromkeys(v for v in values if v and not re.fullmatch(r'[-—\s]+|\d+|暫無出演者資訊|暂无出演者信息|N/?A|Unknown', v, re.I)))
            if not result[key]:
                result.pop(key)
    for key in ("title", "cn_title"):
        if key in result and str(result[key]).casefold().strip(" .!…。\r\n") in _BAD_TITLES:
            result.pop(key)
        if code and key in result:
            # 本地占位 NFO 常把番号直接填入 title，不能当作已取得片名。
            compact = lambda value: re.sub(r'[\s_-]+', '', value).casefold()
            if compact(result[key]) in {compact(code), compact(base_code(code) or code)}:
                result.pop(key)
    return result


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

    from app.core.config import get_settings
    from app.modules.scrape import preferences
    settings = get_settings()
    priorities = getattr(settings, "scrape_field_priority", {})
    excluded = getattr(settings, "scrape_field_exclude", {})
    priorities = priorities if isinstance(priorities, dict) else {}
    excluded = excluded if isinstance(excluded, dict) else {}
    global_priority = getattr(settings, "scrape_source_priority", {})
    global_explicit = isinstance(global_priority, dict) and bool(global_priority.get(preferences.category(code)))

    for result in results:
        if not result.detail:
            continue
        detail = clean_detail(result.detail, code)
        if priorities.get("cn_title") and _has_chinese(detail.get("title")):
            detail.setdefault("cn_title", detail["title"])
        for field, value in detail.items():
            if result.site in preferences.sites(excluded.get(field, "")):
                continue
            priority = preferences.sites(priorities.get(field, "")) or ([r.site for r in results] if global_explicit else [])
            if priority:
                # 显式字段优先级胜过长度等启发式；未列出的源按全局顺序兜底。
                if field not in merged or result.site in priority and (origin[field] not in priority or priority.index(result.site) < priority.index(origin[field])):
                    merged[field] = value
                    origin[field] = result.site
                continue
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


def base_code(code: str) -> str:
    """去掉 -C / -UC 这类版本后缀，返回资源站上的番号。

    版本后缀是本地用来区分「中文字幕版 / 无码版」的（文件名带、媒体库
    条目带），但资源站上只有一条 START-025 —— 拿 START-025-C 去查，
    每个站都会返回「无此番号」，整部片一个字段都抓不到。

    没有后缀时返回空串，表示「没有别的可试了」。
    """
    from app.utils import _CODE_SUFFIX

    upper = (code or "").upper()
    for suffix in ("-UCH", "-UC", "-CH", "-C", "-U"):
        if upper.endswith(suffix):
            stripped = upper[: -len(suffix)]
            # 剥完还得像个番号，别把 T-28 剥成 T-
            return stripped if _CODE_SUFFIX.match(suffix) and stripped else ""
    return ""


def fetch_merged(code: str, timeout: float | None = None) -> dict:
    """抓全部可用站点并合并。抓不到返回空字典。

    首次命中后保留可配置的补全窗口，总等待不超过 timeout。
    超时仅停止等待，底层请求靠自身的网络超时结束。

    本地字幕/无码版本直接按基础番号查询，避免把总超时耗在不存在的版本号上。
    """
    if not code:
        return {}

    from app.core.config import get_settings
    if timeout is None:
        timeout = max(1, min(300, getattr(get_settings(), "scrape_metadata_timeout", 25)))
    lookup = base_code(code) or code
    if lookup != code:
        logger.info(f"[{code}] 使用基础番号 {lookup} 获取元数据，保留本地版本标记")
    return _fetch_one(lookup, timeout)


def _fetch_one(code: str, timeout: float) -> dict:
    """按一个番号抓全部站点并合并。"""
    from app.modules import ladysite
    from app.core.config import get_settings

    from app.modules.scrape import preferences
    settings = get_settings()
    sites = preferences.ordered(code, ladysite._sites_for_code(code), getattr(settings, "scrape_source_priority", {}))
    if not sites:
        logger.warning(f"[{code}] 没有可用数据源，检查数据源开关与番号规则")
        return {}

    logger.info(f"[{code}] 多源刮削，检索 {len(sites)} 个源：{', '.join(sites)}")
    started = time.perf_counter()
    from app.modules.ladysite.transport import FetchBudget
    deadline = started + max(0, timeout)
    budget = FetchBudget(deadline)

    fetch_detail = ladysite._fetch_detail
    def fetch(site: str) -> SourceResult:
        begin = time.perf_counter()
        try:
            with budget.activate():
                detail = fetch_detail(site, code)
            if detail and not detail.get('code'):
                return SourceResult(site, None, time.perf_counter() - begin, '来源未提供可验证番号')
            return SourceResult(site, detail, time.perf_counter() - begin)
        except Exception as exc:
            return SourceResult(site, None, time.perf_counter() - begin, str(exc))
        finally:
            _SLOTS.release()

    # 批量同时准备两部影片，为另一部保留容量，避免先到的慢源占满全池。
    workers = min(len(sites), ladysite.MAX_PARALLEL_SITES, max(1, _WORKERS // 2))
    schedule = list(range(len(sites)))
    # 中文补充源不应因为前六个站都卡住而永远没有启动机会。
    # 仅调整请求启动顺序，字段选择仍按用户配置的全局顺序。
    if "airav" in sites and sites.index("airav") >= workers and workers > 1:
        index = sites.index("airav")
        schedule.remove(index)
        schedule.insert(workers - 1, index)
    grace = max(0, min(30, getattr(get_settings(), "scrape_merge_grace", 3)))
    merge_deadline = deadline
    pending, completed, reports = {}, {}, {}
    next_index = 0

    def submit_available():
        nonlocal next_index
        while next_index < len(sites) and len(pending) < workers and time.perf_counter() < merge_deadline:
            if not _SLOTS.acquire(blocking=False):
                break
            index = schedule[next_index]
            try:
                future = _POOL.submit(fetch, sites[index])
            except Exception:
                _SLOTS.release()
                raise
            # 尚未开始的任务被取消时，fetch 的 finally 不会执行。
            future.add_done_callback(lambda f: _SLOTS.release() if f.cancelled() else None)
            pending[future] = index
            next_index += 1

    try:
        submit_available()
        while pending or next_index < len(sites):
            remaining = merge_deadline - time.perf_counter()
            if remaining <= 0:
                break
            if not pending:
                # 上一轮超时的网络请求可能仍占着全部线程。等待容量恢复，
                # 不能把“尚未发出请求”当成所有站点都没有资料。
                time.sleep(min(0.05, remaining))
                submit_available()
                continue
            ready, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not ready:
                break
            for future in ready:
                index = pending.pop(future)
                result = future.result()
                cleaned = clean_detail(result.detail or {}, code)
                if cleaned:
                    completed[index] = SourceResult(result.site, {'code': code, **cleaned}, result.elapsed)
                    # 封面/评分先返回不代表拿到了片名，不能提前终止主要来源。
                    if cleaned.get('title') or cleaned.get('cn_title'):
                        merge_deadline = min(merge_deadline, time.perf_counter() + grace)
                    status = "hit"
                else:
                    status = "error" if result.error else "empty"
                reports[index] = {"site": result.site, "status": status, "elapsed": round(result.elapsed, 3)}
                if result.error:
                    reports[index]['error'] = result.error
                logger.info(f"[{code}] {result.site}: {status}，{result.elapsed:.2f}s")
            submit_available()

    finally:
        # 取消异步HTTP及过盾排队，避免上一轮继续占满来源线程。
        budget.cancel()

    reason = "merge_window" if merge_deadline < deadline else "timeout"
    for future, index in pending.items():
        future.cancel()
        reports[index] = {"site": sites[index], "status": reason, "elapsed": round(time.perf_counter() - started, 3)}
    for index in schedule[next_index:]:
        reports[index] = {"site": sites[index], "status": "not_started", "elapsed": 0}
    results = [completed[index] for index in sorted(completed)]
    hit = [r for r in results if r.detail]
    logger.info(
        f"[{code}] {len(hit)}/{len(sites)} 个源有结果，"
        f"耗时 {time.perf_counter() - started:.1f}s"
    )
    if not hit:
        return MetadataResult([reports[index] for index in sorted(reports)])
    merged = merge_details(results, code)
    merged['_image_candidates'] = {'still_photo': list(dict.fromkeys(
        r.detail['still_photo'] for r in results if r.detail.get('still_photo')
    ))}
    merged["_sources"] = [reports[index] for index in sorted(reports)]
    return merged


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
            continue
        if score > best_score:
            best_name, best_data, best_score = name, data, score

    if best_name:
        logger.debug(f"[刮削] 封面选用 {best_name}（评分 {best_score}）")
    return best_name, best_data
