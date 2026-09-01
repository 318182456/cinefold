"""自建刮削编排。

把既有的零件串成一条完整链路，替代外部刮削工具（MDCng 等）：

    源文件 ──> 解析文件名（番号/分集/标记）      utils/mediafile
           ──> 抓元数据（缺则抓，有则用库里的）  modules/ladysite
           ──> 翻译标题                        services.translate_title
           ──> 算产物路径                      modules/scrape/naming
           ──> 建硬链接                        本模块
           ──> 写 NFO                          modules/scrape/nfo
           ──> 下载并落图                      modules/scrape/images
           ──> 登记 media_link                 services/medialink
           ──> 抓字幕 + AI 看点                services/subtitle, review

与 external 模式（webhook 回调）互斥，由 SCRAPE_MODE 决定走哪条。
两边最终都汇到 media_link 表，删除联动那套逻辑完全共用。

分集是贯穿全程的一等概念：一个番号可能对应多个文件，元数据只抓一次，
但硬链接、NFO、图片每个分集各一份（MDCng issue #503 的修法）。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from loguru import logger

from app.core.config import get_settings
from app.database.models import Code
from app.database.session import session_scope
from app.modules.scrape import images as scrape_images
from app.modules.scrape import merge, naming, nfo
from app.utils import get_true_code, mediafile
from app.utils.mediafile import MediaFileInfo

# 图片下载的并发数。与 cache_lack_photos 保持一致 —— 图源对单 IP
# 的连接数敏感，开大反而被限速
IMAGE_WORKERS = 5

# 图源要带 Referer 才给图，防盗链。与 services.cache_lack_photos 同源
_IMAGE_HEADERS = {
    "Referer": "https://www.javbus.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


@dataclass
class ScrapeResult:
    """一次刮削的结果。按番号汇总，分集合在一条里。"""
    code: str = ""
    # 成功建好的产物路径（每个分集一条）
    links: list[Path] = field(default_factory=list)
    nfo_written: int = 0
    images_written: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.links) and not self.error


def _load_meta(code: str, fetch: bool = True) -> dict:
    """取番号元数据。库里没有且 fetch=True 时去抓。

    抓完写回库 —— 下次刮削同一番号（重刮、补分集）就不用再跨境请求。
    复用 fill_lack_codes_by_list 的落库语义：只补空字段，不动已有的
    翻译与人工修改。
    """
    from app import services

    with session_scope() as session:
        row = session.get(Code, code)
        if row is not None and (row.title or row.cn_title):
            return {
                "code": row.code,
                "title": row.title or "",
                "cn_title": row.cn_title or "",
                "outline": row.outline or "",
                "release_date": row.release_date or "",
                "duration": row.duration or "",
                "producer": row.producer or "",
                "publisher": row.publisher or "",
                "series": row.series or "",
                "genres": row.genres or "",
                "casts": row.casts or "",
                "star": row.star,
                "banner": row.banner or "",
                "poster": row.poster or "",
                "still_photo": row.still_photo or "",
                "local_banner": row.local_banner or "",
                "portrait_side": row.portrait_side or "",
            }

    if not fetch:
        return {}

    # 库里没有：多源抓取并合并。刮削比订阅更看重字段完整度，
    # 所以走 merge.fetch_merged（全抓完挑最好的）而不是 ladysite
    # 的 first-wins，理由见 modules/scrape/merge.py
    services._ensure_codes([code])
    detail = merge.fetch_merged(code)
    if detail:
        _save_meta(code, detail)
        _translate_quietly(code)
    return _load_meta(code, fetch=False)


def _translate_quietly(code: str) -> None:
    """把标题译成中文，落进 cn_title。失败不影响刮削。

    NFO 的 <title> 优先用 cn_title（见 nfo.NfoData.display_title），
    没有译文时只能显示日文原标题 —— 中文媒体库里一屏日文很难扫。

    只在 cn_title 为空时翻：已有译文可能是用户手改过的，或上一轮翻好的，
    重翻既费钱又可能把好的覆盖成差的。这与 translate_codes 的语义一致
    （它也只翻空的）—— translate_code_title 本身是「一律重翻」的手动入口，
    所以由这里来把关该不该调。

    翻译要调外部 API，慢且可能没配。异常一律吞掉 —— 刮削的正事是产物，
    标题是日文总比整个刮削失败强。
    """
    from app import services

    try:
        with session_scope() as session:
            row = session.get(Code, code)
            if row is None or row.cn_title or not row.title:
                return
        result = services.translate_code_title(code)
        if isinstance(result, dict) and result.get("error"):
            logger.debug(f"[{code}] 标题未翻译: {result['error']}")
    except Exception as exc:
        logger.debug(f"[{code}] 标题翻译失败，NFO 将用原标题: {exc}")


def _save_meta(code: str, detail: dict) -> None:
    """把抓来的元数据落库。只补空字段，不动已有值。

    与 fill_lack_codes_by_list 同一语义：已翻译的 cn_title、
    人工改过的字段不会被冲掉。
    """
    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            return
        for key, value in detail.items():
            if value and hasattr(row, key) and not getattr(row, key, None):
                setattr(row, key, value)


def _context(meta: dict, info: MediaFileInfo, resolution: str = "") -> naming.NamingContext:
    """把元数据与文件信息装成命名上下文。"""
    settings = get_settings()
    release = meta.get("release_date") or ""
    return naming.NamingContext(
        number=info.code,
        series=meta.get("series") or "",
        category=settings.scrape_category or "",
        actor=meta.get("casts") or "",
        title=meta.get("cn_title") or meta.get("title") or "",
        originaltitle=meta.get("title") or "",
        year=release[:4] if len(release) >= 4 and release[:4].isdigit() else "",
        studio=meta.get("producer") or "",
        publisher=meta.get("publisher") or "",
        runtime=nfo._runtime_minutes(meta.get("duration")),
        release=release,
        source_path=str(info.path),
        # 标记只来自文件名与番号后缀，不做内容推断（issue #513）
        subtitle="中文字幕" if info.subbed else "",
        mosaic="无码" if info.uncensored else "有码",
        resolution=resolution,
        part=info.part,
    )


def _target_path(
    meta: dict, info: MediaFileInfo, target_dir: str = "",
) -> Path:
    """算出这个分集的产物完整路径。

    target_dir 是本次指定的硬链接根目录，覆盖 MEDIALINK_SCRAPE_DIR。
    与监控目录规则的 target_dir 同一个意思（见 api/endpoints/watchdir.py）：
    填了就用它，留空才回退到全局配置。

    用途是「这一批片子放别处」—— 按厂牌分库、临时刮到另一块盘、
    或者拿一个空目录试跑一遍看看产物长什么样，都不用改全局配置。

    目录模板仍然生效，在指定的根目录之下展开。要完全平铺就把
    目录模板留空。
    """
    settings = get_settings()
    root = Path(
        target_dir
        or settings.medialink_scrape_dir
        or settings.medialink_library_path
        or ""
    )
    context = _context(meta, info)
    relative = naming.render_dir(settings.scrape_dir_template, context)
    filename = naming.render_file(
        settings.scrape_file_template, context, info.path.suffix.lower()
    )
    return root / relative / filename


def _make_link(source: Path, target: Path) -> tuple[bool, str]:
    """建硬链接。返回 (是否成功, 说明)。

    已存在且指向同一 inode 就算成功 —— 重刮时这是常态，不该报错也不该
    重建。指向别的文件才是真冲突，那种情况不覆盖：目标位置有另一部片子，
    删掉它是数据丢失。
    """
    try:
        if target.exists():
            if target.samefile(source):
                return True, "已存在"
            return False, f"目标已存在且不是同一文件: {target}"

        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
        return True, "已建立"
    except OSError as exc:
        # 跨文件系统是最常见的失败原因，单独说清楚 —— 用户改配置就能解决
        if getattr(exc, "errno", None) == 18:  # EXDEV
            return False, f"源与目标不在同一文件系统，无法建硬链接: {exc}"
        return False, f"建硬链接失败: {exc}"


def _download(urls: list[str], code: str) -> list[bytes]:
    """并发下载一批图片。失败的位置返回空 bytes，保持与入参等长。"""
    if not urls:
        return []
    settings = get_settings()

    def fetch(url: str) -> bytes:
        if not url:
            return b""
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                proxy=settings.proxy or None,
                headers=_IMAGE_HEADERS,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.content
        except Exception as exc:
            logger.debug(f"[{code}] 图片下载失败 {url}: {exc}")
            return b""

    workers = min(IMAGE_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch, urls))


def _cover_bytes(meta: dict, code: str) -> bytes:
    """取封面内容。

    本地缓存优先：Web UI 那边通常已经缓存过（cache_lack_photos），
    读盘比跨境下载快几个数量级。

    缓存没有时，banner 与 poster 两个来源都下，挑分辨率高的那张 ——
    同一部片各站给的尺寸差别很大（缩略图 300px vs 原图 800px），
    海报要裁一半还要在 Emby 上放大显示，清晰度直接决定观感。
    """
    from app.utils import imagecache

    relative = (meta.get("local_banner") or "").split(",")[0].strip()
    if relative:
        path = imagecache.resolve_relative(relative)
        if path is not None and path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                pass

    urls = [
        (kind, meta.get(kind) or "")
        for kind in ("banner", "poster")
    ]
    urls = [(kind, url) for kind, url in urls if url]
    if not urls:
        return b""

    candidates: list[tuple[str, bytes]] = []
    for kind, url in urls:
        # 缓存里可能已经有这张（库里只是没记 local_banner）
        hit = imagecache.find_cached(url, code, kind)
        if hit is not None:
            try:
                candidates.append((f"{kind}(缓存)", hit.read_bytes()))
                continue
            except OSError:
                pass
        candidates.append((kind, b""))

    missing = [i for i, (_, data) in enumerate(candidates) if not data]
    if missing:
        fetched = _download([urls[i][1] for i in missing], code)
        for slot, data in zip(missing, fetched):
            candidates[slot] = (candidates[slot][0], data)

    _, best = merge.pick_best_image(candidates)
    return best


def scrape_file(
    path: Path | str, fetch_meta: bool = True, code: str = "",
    target_dir: str = "",
) -> ScrapeResult:
    """刮削单个影片文件。分集请用 scrape_group，它只抓一次元数据。

    code 是人工指定的番号，用于文件名认不出来的情况（欧美片、改过名的
    文件、番号只写在压缩包名里的）。给了就不再从文件名猜 —— 用户比正则
    更清楚这是哪部片，猜出来的哪怕看着对也不该盖过明确的指定。

    分集标记仍然从文件名读：人工指定的是「哪部片」，不是「哪一集」。
    ABS-001-CD2.mp4 指定番号 SSIS-999 时，产物应是 SSIS-999-CD2。
    """
    info = mediafile.parse(path)

    manual = get_true_code(code) if code else ""
    if code and not manual:
        return ScrapeResult(error=f"指定的番号不是合法格式: {code}")

    if manual:
        if info.code and info.code != manual:
            logger.info(
                f"[刮削] 人工指定番号 {manual} 覆盖文件名识别结果 {info.code}: "
                f"{Path(path).name}"
            )
        info.code = manual
        # 指定了番号就说明用户认定它有番号，欧美片判定不再成立
        info.western = False

    if not info.code:
        reason = "欧美片命名，没有番号" if info.western else "文件名认不出番号"
        return ScrapeResult(
            error=f"{reason}: {path}。可在刮削时手动指定番号重刮"
        )
    return scrape_group(
        info.code, [info], fetch_meta=fetch_meta, target_dir=target_dir
    )


def scrape_group(
    code: str,
    items: list[MediaFileInfo],
    fetch_meta: bool = True,
    target_dir: str = "",
) -> ScrapeResult:
    """刮削同一番号的一组文件（可能是多个分集）。

    元数据只抓一次，产物按分集各出一份。这是本模块的核心 ——
    issue #503 的症结就是把分集当成了不同番号。
    """
    result = ScrapeResult(code=code)
    settings = get_settings()

    playable = [i for i in items if not i.trailer]
    dropped = [i for i in items if i.trailer]
    for item in dropped:
        # 预告片不是正片，刮进媒体库会多出一堆几十秒的"影片"
        result.skipped.append(f"预告片跳过: {item.path.name}")

    if not playable:
        result.error = "这一组里没有正片"
        return result

    meta = _load_meta(code, fetch=fetch_meta)
    if not meta:
        result.error = f"取不到元数据: {code}"
        return result

    # 图片对一个番号只准备一份，各分集共用同一份内容
    cover = _cover_bytes(meta, code)
    still_urls = [
        u.strip()
        for u in (meta.get("still_photo") or "").split(",")
        if u.strip()
    ][: max(0, settings.scrape_still_limit)]
    stills = _download(still_urls, code) if still_urls else []
    image_set = scrape_images.build_image_set(
        cover, stills, meta.get("portrait_side") or ""
    )

    total = len([i for i in playable if i.part > 0])
    for info in playable:
        info_total = total
        target = _target_path(meta, info, target_dir)

        ok, note = _make_link(info.path, target)
        if not ok:
            result.skipped.append(note)
            logger.warning(f"[{code}] {note}")
            continue
        result.links.append(target)

        written = scrape_images.write_images(
            target, image_set, overwrite=settings.scrape_overwrite
        )
        if written:
            result.images_written += 1

        data = nfo.NfoData(
            code=code,
            title=meta.get("title") or "",
            cn_title=meta.get("cn_title") or "",
            # 官方简介。只有 airav 给这个字段，多数番号是空的 ——
            # 空着也没关系，AI 看点会往 plot 里拼（services/review.py，
            # 靠 MARKER/END_MARKER 精确替换自己那段，不动官方简介）。
            #
            # 绝不拿标题兜底：那等于把同一句话在 Emby 上写两遍
            plot=meta.get("outline") or "",
            release_date=meta.get("release_date") or "",
            duration=meta.get("duration") or "",
            producer=meta.get("producer") or "",
            publisher=meta.get("publisher") or "",
            series=meta.get("series") or "",
            genres=meta.get("genres") or "",
            casts=meta.get("casts") or "",
            star=meta.get("star"),
            part=info.part,
            total_parts=info_total,
            uncensored=info.uncensored,
            subbed=info.subbed,
            poster_file=written.get("poster", ""),
            fanart_file=written.get("fanart", ""),
            extra_genres=_extra_genres(info),
        )
        if nfo.write(
            target.with_suffix(".nfo"), data, overwrite=settings.scrape_overwrite
        ):
            result.nfo_written += 1

    if result.links:
        _register(code, playable, result)

    logger.info(
        f"[{code}] 刮削完成: {len(result.links)} 个产物, "
        f"NFO {result.nfo_written}, 图片 {result.images_written}"
        + (f", 跳过 {len(result.skipped)}" if result.skipped else "")
    )
    return result


def _extra_genres(info: MediaFileInfo) -> list[str]:
    """从文件名标记推出的额外标签。

    只认文件名与番号后缀里明写的，不做内容推断 —— MDCng issue #513
    是把没破解的片子打上了破解标签，那种误标比不标麻烦得多：
    用户得逐个手动改回来。
    """
    out: list[str] = []
    if info.subbed:
        out.append("中文字幕")
    if info.uncensored:
        out.append("无码破解")
    if info.part > 0:
        out.append("分集")
    return out


def _register(code: str, items: list[MediaFileInfo], result: ScrapeResult) -> None:
    """把产物登记进 media_link，并触发状态流转、字幕、看点。

    不走 medialink.register_scrape：那个函数要扫整个媒体库按 inode 反查
    硬链接在哪（大库上是分钟级），而这里的链接是我们自己刚建的，路径确定。
    复用 watchdir._register —— 它就是为这种「路径已知」的情况写的，
    还会顺手登记 CodeAlias。

    登记后的三件事与 register_scrape 保持一致，缺一件都会让自建刮削
    比 external 模式少功能：
        mark_completed        订阅状态改成「已入库」
        字幕                  跨境抓，失败不影响登记
        AI 看点               往刚写的 NFO 的 plot 里拼
    """
    from app.services import medialink
    from app.services.watchdir import _register as write_link

    linked = list(zip(items, result.links))
    with session_scope() as session:
        for info, link in linked:
            try:
                write_link(code, str(info.path), str(link), session=session)
            except Exception as exc:
                logger.warning(f"[{code}] 登记硬链接失败 {link}: {exc}")

    logger.info(f"[{code}] 已登记 {len(linked)} 条硬链接关联")

    # 这三件事各自失败都不该影响刮削结果，逐个吞异常。
    # medialink 里那两个 _quietly 函数已经是这个语义，直接借用
    try:
        medialink.mark_completed(code)
    except Exception as exc:
        logger.warning(f"[{code}] 更新入库状态失败: {exc}")
    medialink._fetch_subtitle_quietly(code)
    medialink._generate_review_quietly(code)


def scrape_dir(
    directory: Path | str, fetch_meta: bool = True, code: str = "",
    target_dir: str = "",
) -> list[ScrapeResult]:
    """刮削一个目录下的全部影片。按番号分组，每组只抓一次元数据。

    code 是人工指定的番号：整个目录里的影片都算这一部（含它的分集）。
    用于「一部片一个文件夹但文件名乱七八糟」的情况 ——
    目录名认不出、文件名也认不出时，指定一次就能把整组刮好。

    只在目录里认不出番号的文件上生效，已经认出来的不动 —— 指定一个番号
    把整个下载目录里几十部不同的片全刮成同一部，那是灾难。
    """
    root = Path(directory)
    if not root.is_dir():
        return [ScrapeResult(error=f"不是目录: {root}")]

    manual = get_true_code(code) if code else ""
    if code and not manual:
        return [ScrapeResult(error=f"指定的番号不是合法格式: {code}")]

    from app.services.medialink import is_adoptable_video

    videos: list[Path] = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and is_adoptable_video(path):
                videos.append(path)
        except OSError:
            continue

    if not videos:
        return []

    groups = mediafile.group_parts(videos)
    unknown = groups.pop("", [])

    if manual and unknown:
        # 人工指定的番号只认领认不出的那些。已经认出番号的文件保持原样，
        # 否则指定一次会把整个目录里几十部不同的片全刮成同一部
        logger.info(f"[刮削] 人工指定番号 {manual} 认领 {len(unknown)} 个文件")
        for info in unknown:
            info.code = manual
            info.western = False
        merged = groups.setdefault(manual, [])
        merged.extend(unknown)
        merged.sort(key=lambda i: (i.part, i.path.name))

        # 认领进来的文件多半没有分集标记（本来就认不出番号，更不会
        # 规规矩矩写 CD1）。同一番号下多个文件却都不带分集号时，产物名
        # 会全撞在一起 —— 第二个开始建不了链接。按排序结果编号补上，
        # 这也符合用户的意图：指定一个番号认领多个文件，就是说这几个
        # 文件是同一部片的多个分集
        if len(merged) > 1:
            for index, info in enumerate(merged, start=1):
                if info.part == 0:
                    info.part = index
        mediafile._resolve_collisions(merged)
        unknown = []

    for info in unknown:
        # 分开说原因：欧美片是「本来就没有番号」，与「番号该有但认不出」
        # 是两回事。混在一句里用户会以为是识别 bug，去反复重刮
        if info.western:
            logger.info(
                f"[刮削] 欧美片命名，无番号可用，跳过: {info.path.name}。"
                "可指定番号重刮"
            )
        else:
            logger.warning(
                f"[刮削] 认不出番号，跳过: {info.path}。可指定番号重刮"
            )

    results = []
    for code, items in groups.items():
        try:
            results.append(scrape_group(
                code, items, fetch_meta=fetch_meta, target_dir=target_dir
            ))
        except Exception as exc:
            logger.error(f"[{code}] 刮削出错: {exc}")
            results.append(ScrapeResult(code=code, error=str(exc)))
    return results
