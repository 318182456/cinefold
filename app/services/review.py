"""AI 影评的生成、落库与写出。

三条触发路径，与字幕那层同构：

    刮削登记完成 ──> generate_for_code()      入库即生成，全自动
    定时任务     ──> fill_lack_reviews()      扫媒体库补漏
    页面按钮     ──> generate_for_code(force) 人工指定，可覆盖

生成结果落库（review 表）后写向两处：

    NFO 文件   影片旁的 <同名>.nfo，改写 <plot>，Emby 扫库即读
    Emby API   直接改条目的 Overview，不必等扫库

两处都写的理由是它们各有失效场景：刮削工具重刮会把 NFO 冲掉，
而 Emby 的库里若还没有这个条目，API 就无处可写。两条一起上，
任一条成了就能看到；NFO 那份还兼作重刮后的恢复依据。

要点拼成简介时的格式，是照 Emby 简介栏能显示的样子来的：Emby 的
Overview 不渲染 markdown，列表符号只能用纯文本的「·」，换行倒是认。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Code, MediaLink, Review
from app.database.session import session_scope
from app.utils import get_true_code

# 拼进 NFO/Emby 简介时，AI 那段的首尾标记。
#
# 非要有标记不可：写 plot 是"改写别人的字段" —— 刮削工具已经往里放了
# 官方简介。没有标记就分不清哪段是自己写的，重复生成时只能整段覆盖，
# 把官方简介一起冲掉。有了标记就能只替换自己那段。
#
# 要首尾两个而不是只有开头一个，是因为 AI 段现在排在官方简介**前面**
# （见 merge_text：手机端简介折叠只露前几行，垫在后面等于白写）。
# 只有开头标记时，切分只能从标记一刀切到结尾 —— 那会把后面的官方简介
# 一起切掉。有了结束标记才能精确地只挖走中间这段。
#
# 两个都短且不单占一行（见 render）：折叠后只有三四行可用，
# 拿一整行去画分隔线太奢侈了
MARKER = "【AI】"
END_MARKER = "▲"

# 早先版本用过的标记。已经写进 NFO 与 Emby 的条目都带着它们，
# 剥离时必须照样认得 —— 认不出来就不会被剥掉，新的一段又拼在前面，
# 同一条简介里并排躺着两段 AI 内容。只能往里加，不能删
LEGACY_MARKERS = ("── AI 看点 ──",)
LEGACY_END_MARKERS = ("──────────",)

# 要点之间的分隔符。Emby 简介不渲染 markdown，用不了 - 或 *。
# 是分隔符不是前缀 —— 要点全折进一行，靠它断开
BULLET = " · "


# ----------------------------------------------------------------------
# 生成
# ----------------------------------------------------------------------
def generate_for_code(code: str, force: bool = False, manual: bool = False) -> bool:
    """给一个番号生成影评并写出。返回是否生成成功。

    force=True 时重新生成（页面上"不满意再来一版"走这条）。
    manual=True 跳过总开关：开关管的是自动行为，人点了按钮就是明确要生成
    （与字幕手动重抓同理）。
    """
    code = get_true_code(code)
    if not code:
        return False

    settings = get_settings()
    if not manual and not settings.review_enabled:
        return False

    if not force and _existing(code) is not None:
        logger.debug(f"[影评] {code} 已有记录，跳过")
        return True

    meta = _load_meta(code)
    if not meta:
        logger.debug(f"[影评] {code} 库里没有元数据，跳过")
        return False

    from app.modules.review import build_review

    data = build_review(meta)
    if not data or not (data.get("summary") or data.get("highlights")):
        logger.info(f"[影评] {code} 未生成出有效内容")
        return False

    _save(code, data)
    logger.info(f"[影评] {code} 已生成")

    # 写出失败不该让生成结果丢掉 —— 库里那份已经存住了，
    # 定时任务下一轮会按 nfo_time 为空把它补写出去
    try:
        write_out(code)
    except Exception as exc:
        logger.warning(f"[影评] {code} 写出失败，稍后由定时任务补: {exc}")

    return True


def fill_lack_reviews(limit: int = 0) -> int:
    """扫媒体库，给还没有影评的影片补生成。返回补上的部数。

    定时任务走这条。早期入库的片子、当时 AI 没配好的片子，靠这个补上。

    limit 为 0 时用配置里的每轮上限 —— 不能不设限：每部一次 AI 请求，
    媒体库上千部的话一轮能跑很久，还烧钱。
    """
    settings = get_settings()
    if not settings.review_enabled:
        logger.debug("[影评] 未启用，跳过补漏")
        return 0

    limit = limit or max(settings.review_fill_limit, 1)

    filled = 0
    for code in _codes_lacking_review(limit):
        try:
            if generate_for_code(code):
                filled += 1
        except Exception as exc:
            # 单个番号失败不该中断整轮
            logger.warning(f"[影评] 生成 {code} 失败: {exc}")

    # 顺带把该写没写出去的补上：生成成功但当时写 NFO 失败的、
    # 以及刮削工具重刮把 plot 冲掉的，都在这轮回填。
    # 这一步不发 AI 请求，内容库里都有，所以不受 limit 的成本考量约束 ——
    # 但仍传 limit 进去，免得一轮扫穿整个媒体库
    rewritten = _rewrite_pending(limit)

    if filled or rewritten:
        logger.info(f"[影评] 本轮生成 {filled} 部，补写 {rewritten} 处")
    return filled


# ----------------------------------------------------------------------
# 落库
# ----------------------------------------------------------------------
def _existing(code: str) -> Review | None:
    with session_scope() as session:
        row = session.get(Review, code)
        if row is None:
            return None
        session.expunge(row)
        return row


def _load_meta(code: str) -> dict:
    """取生成所需的元数据。番号在 code 表里没有记录时返回空。"""
    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            return {}
        return {
            "code": row.code,
            "title": row.title,
            "cn_title": row.cn_title,
            "casts": row.casts,
            "genres": row.genres,
            "duration": row.duration,
            "series": row.series,
            "producer": row.producer,
        }


def _save(code: str, data: dict) -> None:
    """写入或覆盖一条影评记录。"""
    highlights = "\n".join(data.get("highlights") or [])

    with session_scope() as session:
        row = session.get(Review, code)
        if row is None:
            row = Review(code=code)
            session.add(row)

        row.cast_count = data.get("cast_count") or 0
        row.body_type = data.get("body_type") or None
        row.style = data.get("style") or None
        row.highlights = highlights or None
        row.summary = data.get("summary") or None
        # 内容变了，先前写出去的那份就过期了。置空让写出逻辑重来
        row.nfo_time = None


def _codes_lacking_review(limit: int) -> list[str]:
    """媒体库里还没有影评记录的番号。

    只找已入库的（media_link 里有记录）—— 没进媒体库的番号生成了也没处
    显示，白花 AI 请求。
    """
    with session_scope() as session:
        return list(session.scalars(
            select(MediaLink.code)
            .where(MediaLink.code.notin_(select(Review.code)))
            .distinct()
            .limit(limit)
        ).all())


def _rewrite_pending(limit: int) -> int:
    """把该写而没写在 NFO 里的补写出去。返回补写成功的部数。

    两类都要管：

    1. nfo_time 为空 —— 生成时写盘失败，或当时影片旁还没有 NFO
    2. nfo_time 有值，但 NFO 里已经找不到那段 —— 刮削工具重刮把 plot
       整个冲掉了。这类光看 nfo_time 是发现不了的：库里那个时间戳记的是
       「上次写成功」，磁盘上的内容却已经没了，不去读文件就永远选不中，
       看点就此永久丢失

    所以不能只信时间戳，得真去看一眼文件。读 NFO 是本地 IO，
    比重新生成便宜得多 —— 贵的是 AI 请求，而这里一次都不发：
    内容还在库里，写回去就行。
    """
    with session_scope() as session:
        codes = list(session.scalars(
            select(Review.code).order_by(Review.update_time.desc()).limit(limit)
        ).all())

    done = 0
    for code in codes:
        try:
            if not _needs_rewrite(code):
                continue
            if write_out(code):
                done += 1
        except Exception as exc:
            logger.warning(f"[影评] 补写 {code} 失败: {exc}")
    return done


def _needs_rewrite(code: str) -> bool:
    """这个番号的 NFO 需不需要补写。

    没登记 NFO 位置（还没进媒体库）时返回 False —— 无处可写，
    每轮都去试只是白跑。
    """
    paths = _nfo_paths(code)
    if not paths:
        return False

    for path in paths:
        try:
            if not path.is_file():
                # 影片在但 NFO 还没刮出来，等刮削工具补上再说
                continue
            if MARKER not in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError as exc:
            logger.debug(f"[影评] 读 NFO 失败 {path}: {exc}")
            continue
    return False


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------
def render(row: Review) -> str:
    """把要点拼成写进简介的那段文本。

    整段排在官方简介之前（见 merge_text），而客户端折叠后只露前三四行 ——
    所以这里必须尽量少占行：

    - 标记不单占一行，和事实挤在同一行的首尾
    - 要点全部折进一行，用「·」分隔，而不是每条一行
    - 简评单独一行放最后，被折掉也不影响前面的事实与要点

    压缩后固定两行（有简评时三行），折叠状态下基本能完整露出来。
    """
    facts: list[str] = []
    if row.cast_count:
        facts.append(f"出演 {row.cast_count} 人")
    if row.body_type:
        facts.append(row.body_type)
    if row.style:
        facts.append(row.style)

    items = [x.strip() for x in (row.highlights or "").splitlines() if x.strip()]

    lines: list[str] = []
    # 首行：标记 + 事实。事实为空时标记也不单独占行，留给要点那行
    head = " / ".join(facts)
    lines.append(f"{MARKER} {head}" if head else MARKER)

    if items:
        lines.append(BULLET.join(items))

    if row.summary:
        lines.append(row.summary)

    # 结束标记贴在最后一行末尾，不再单占一行
    lines[-1] = f"{lines[-1]} {END_MARKER}"
    return "\n".join(lines)


def _has_content(block: str) -> bool:
    """这段里除了首尾标记之外还有没有实质内容。

    标记现在是内嵌的（见 render），空内容渲染出来是「【AI】 ▲」这么一行，
    整行去比对不上，得先把标记抠掉再看还剩什么。剩不下东西还照写的话，
    简介开头就会多出一行没有意义的符号。
    """
    for raw in block.splitlines():
        if raw.replace(MARKER, "").replace(END_MARKER, "").strip():
            return True
    return False


def merge_text(original: str, block: str) -> str:
    """把 AI 那段并进原简介，已有的替换掉而不是重复追加。

    **AI 那段放在最前面。** Emby 客户端（尤其手机端）的简介栏默认折叠，
    只露前三四行 —— 官方简介动辄几百字，看点垫在它后面就永远在折叠线
    以下，得手动展开才看得到，等于白写。看点是给人一眼扫的，必须占前排。

    刮削工具放的官方简介仍然全须全尾留着，只是挪到后面 —— 它才是这部片
    真正的介绍，不能因为要腾位置就截断或丢弃。

    切分用 END_MARKER 而不是从 MARKER 一刀切到底：AI 段现在在前面，
    从 MARKER 往后切会把官方简介一起切掉。
    """
    original = _strip_block(original)
    if not original:
        return block
    return f"{block}\n\n{original}"


def _strip_block(text: str) -> str:
    """从简介里剥掉之前写进去的 AI 段，返回剩下的原文。

    几种历史形态都要认得出来，否则重复生成会越堆越长：

    1. 当前：MARKER 开头、END_MARKER 结尾，排在最前面
    2. 早先：「── AI 看点 ──」开头一直到结尾，排在最后面

    认不出旧标记的后果是实打实的：那段不会被剥掉，新的一段又拼在前面，
    同一条简介里于是并排躺着两段 AI 内容。已经写出去的条目都是旧标记，
    所以这张表只能加不能删。
    """
    text = (text or "").strip()
    if not text:
        return ""

    for marker in (MARKER, *LEGACY_MARKERS):
        if marker not in text:
            continue

        head, _, rest = text.partition(marker)
        # 有结束标记就只吃到标记为止，后面的官方简介留着
        for end in (END_MARKER, *LEGACY_END_MARKERS):
            if end and end in rest:
                _, _, tail = rest.partition(end)
                return f"{head.strip()}\n\n{tail.strip()}".strip()

        # 没有结束标记 = 早先那版，AI 段一直写到结尾，前面那截才是原文
        return head.strip()

    return text


# ----------------------------------------------------------------------
# 写出
# ----------------------------------------------------------------------
def write_out(code: str) -> bool:
    """把影评写进 NFO 与 Emby。任一处成功即算写出。"""
    row = _existing(code)
    if row is None:
        return False

    block = render(row)
    if not _has_content(block):
        # 只有标记没有内容，写出去只是给简介添两行分隔线
        return False

    wrote_nfo = _write_nfo(code, block)
    wrote_emby = _push_emby(code, block)

    if wrote_nfo:
        with session_scope() as session:
            fresh = session.get(Review, code)
            if fresh is not None:
                fresh.nfo_time = datetime.now()

    return wrote_nfo or wrote_emby


def _nfo_paths(code: str) -> list[Path]:
    """番号在媒体库里对应的 NFO 路径。

    同一部片可能有多个硬链接（分类目录各放一份），每处的 NFO 都要写，
    否则从另一个入口看就没有 —— 与字幕落盘同理。
    """
    with session_scope() as session:
        rows = session.scalars(
            select(MediaLink.link_path).where(MediaLink.code == code)
        ).all()

    out: list[Path] = []
    for raw in rows:
        video = Path(raw)
        try:
            if not video.is_file():
                continue
        except OSError:
            continue
        # NFO 与影片同名同目录，这是 Emby/Jellyfin/Plex 共同的约定
        out.append(video.with_suffix(".nfo"))
    return out


def _write_nfo(code: str, block: str) -> bool:
    """改写 NFO 里的 plot。返回是否至少写成了一处。

    只动 plot 一个节点，其余原样保留 —— 这份文件是刮削工具的产物，
    里面的演员、图片、标签都还要用，重写整份等于把人家的活儿废了。
    NFO 不存在时不新建：没有刮削产物说明这条链路还没走完，
    我们凭元数据造一份只会和后来的刮削结果打架。
    """
    written = 0
    for path in _nfo_paths(code):
        if not path.is_file():
            logger.debug(f"[影评] {code} 旁边没有 NFO，跳过: {path}")
            continue

        try:
            tree = ElementTree.parse(path)
            root = tree.getroot()

            plot = root.find("plot")
            if plot is None:
                plot = ElementTree.SubElement(root, "plot")
            plot.text = merge_text(plot.text or "", block)

            # Emby 详情页在标题下方单独显示 tagline，一行看点放这儿最显眼
            if _tagline(block):
                tagline = root.find("tagline")
                if tagline is None:
                    tagline = ElementTree.SubElement(root, "tagline")
                tagline.text = _tagline(block)

            tree.write(path, encoding="utf-8", xml_declaration=True)
            written += 1
        except ElementTree.ParseError as exc:
            # 刮削工具写坏过的 NFO 不少见。解析不了就别碰，
            # 硬写会把整份文件毁掉
            logger.warning(f"[影评] {code} 的 NFO 解析失败，未改动: {path} ({exc})")
        except OSError as exc:
            logger.warning(f"[影评] {code} 写 NFO 失败: {path} ({exc})")

    return written > 0


def _tagline(block: str) -> str:
    """取事实那一行当 tagline。没有就返回空。

    首行现在是「MARKER + 事实」拼在一起（见 render），所以要把标记剥掉
    再判断，不能整行去比。
    """
    for raw in block.splitlines():
        line = raw.replace(MARKER, "").replace(END_MARKER, "").strip()
        if line:
            return line if " / " in line else ""
    return ""


def _push_emby(code: str, block: str) -> bool:
    """把简介推给 Emby。Emby 没配或找不到条目时返回 False。"""
    settings = get_settings()
    if not (settings.emby_url and settings.emby_api_key):
        return False

    try:
        from app.modules.mediaserver.emby import Emby
        return Emby().update_overview(code, block)
    except Exception as exc:
        logger.warning(f"[影评] {code} 推送 Emby 失败: {exc}")
        return False
