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

from app.services.scrapemaintenance import guarded as maintenance_guarded

import os
import re
import threading
import time
from contextlib import contextmanager
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
from loguru import logger

from app.core.config import get_settings
from app.database.models import Code
from app.database.session import session_scope
from app.modules.scrape import images as scrape_images
from app.modules.scrape import merge, naming, nfo, preferences
from app.utils import get_true_code, mediafile
from app.utils.mediafile import MediaFileInfo

# 图片下载的并发数。与 cache_lack_photos 保持一致 —— 图源对单 IP
# 的连接数敏感，开大反而被限速
IMAGE_WORKERS = 5
_SCRAPE_LOCK = threading.RLock()
_CODE_LOCKS: dict[str, list] = {}
_CODE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _code_lock(code: str):
    """同影片串行，不让不同影片的联网等待占住全局写锁。"""
    key = merge.base_code(code) or code
    with _CODE_LOCKS_GUARD:
        entry = _CODE_LOCKS.setdefault(key, [threading.RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _CODE_LOCKS_GUARD:
            entry[1] -= 1
            if not entry[1]:
                _CODE_LOCKS.pop(key)


def _by_code(fn):
    @wraps(fn)
    def run(code, *args, **kwargs):
        with _code_lock(code):
            return fn(code, *args, **kwargs)
    return run


def _serialized(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        # 只保护产物提交；抓资料、下图、字幕与通知不占用全局锁。
        with _SCRAPE_LOCK:
            return fn(*args, **kwargs)
    return run

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
    warnings: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.links) and not self.error


def _load_meta(code: str, fetch: bool = True, refresh: bool = False) -> dict:
    """取番号元数据。库里没有且 fetch=True 时去抓。

    抓完写回库 —— 下次刮削同一番号（重刮、补分集）就不用再跨境请求。
    复用 fill_lack_codes_by_list 的落库语义：只补空字段，不动已有的
    翻译与人工修改。
    """
    from app import services

    current = _read_meta(code)
    if current and merge.base_code(code):
        # 将复用的基础资料补到本地版本条目，列表和详情也能立即看到。
        services._ensure_codes([code])
        _save_meta(code, current)
    if not fetch or not refresh and not _needs_metadata(current):
        return current

    # 库里没有：多源抓取并合并。刮削比订阅更看重字段完整度，
    # 所以走 merge.fetch_merged（全抓完挑最好的）而不是 ladysite
    # 的 first-wins，理由见 modules/scrape/merge.py
    services._ensure_codes([code])
    detail = merge.fetch_merged(code)
    if detail:
        if refresh:
            _save_meta(code, detail, overwrite=True)
        else:
            _save_meta(code, detail)
        _translate_quietly(code)
    meta = _read_meta(code)
    sources = detail.get("_sources", []) or getattr(detail, "sources", [])
    if sources:
        meta["_sources"] = sources
    if detail.get('_image_candidates'):
        meta['_image_candidates'] = detail['_image_candidates']
    if detail.get('_lookup_code'):
        meta['_lookup_code'] = detail['_lookup_code']
    if refresh and not detail:
        meta["_refresh_failed"] = True
    return meta


def _read_meta(code: str) -> dict:
    with session_scope() as session:
        row = session.get(Code, code)
        base = merge.base_code(code)
        original = session.get(Code, base) if base else None
        inherited = merge.clean_detail(original.to_dict(), code) if original else {}
        own = merge.clean_detail(row.to_dict(), code) if row else {}
        detail = {**inherited, **own}
        if detail.get('casts'):
            from app.services.actornames import localize_casts
            detail['casts'] = localize_casts(detail['casts'], session)
        if not detail:
            return {}
        return {**detail, "code": code,
                "local_banner": (row.local_banner if row else '') or (original.local_banner if original else '') or '',
                "portrait_side": (row.portrait_side if row else '') or (original.portrait_side if original else '') or ''}


def _needs_metadata(meta: dict) -> bool:
    return not (
        (meta.get("title") or meta.get("cn_title"))
        and all(meta.get(key) for key in ("casts", "genres", "release_date", "duration"))
        and any(meta.get(key) for key in ("banner", "poster", "local_banner", "_local_cover"))
    )


def load_group_meta(code: str, items: list[MediaFileInfo], fetch: bool = True, refresh: bool = False) -> dict:
    """预览与执行共用：本地文件补空 → 资料库 → 联网补缺，不覆盖已有资料。"""
    from app import services
    from app.modules.scrape import local
    local_meta = local.read_group(code, [i.path for i in items])
    if refresh:
        meta = _load_meta(code, fetch=fetch, refresh=True)
        # 重新抓取优先使用本轮远程封面；未获得远程图时才借用本地图片。
        if not (meta.get("banner") or meta.get("poster")):
            meta.update({k: v for k, v in local_meta.items() if k.startswith("_local_")})
        return meta
    if local_meta:
        services._ensure_codes([code])
        _save_meta(code, local_meta)
        current = {**_read_meta(code), **{k: v for k, v in local_meta.items() if k.startswith("_local_")}}
        meta = _load_meta(code, fetch=fetch and _needs_metadata(current))
        return {"code": code, **meta, **{k: v for k, v in local_meta.items() if k.startswith("_local_")}}
    return _load_meta(code, fetch=fetch)


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


def _save_meta(code: str, detail: dict, overwrite: bool = False) -> None:
    """把抓来的元数据落库。只补空字段，不动已有值。

    与 fill_lack_codes_by_list 同一语义：已翻译的 cn_title、
    人工改过的字段不会被冲掉。
    """
    with session_scope() as session:
        row = session.get(Code, code)
        if row is None:
            return
        from app.services.actornames import localize_casts
        if row.casts:
            row.casts = localize_casts(row.casts, session)
        for key, value in merge.clean_detail(detail, code).items():
            if key == 'casts':
                value = localize_casts(value, session)
            existing = getattr(row, key, None)
            if (overwrite or existing in (None, "")
                    or key == 'casts' and merge._better('casts', existing, value)
                    or key in ("title", "cn_title") and not merge.clean_detail({key: existing}, code).get(key)):
                setattr(row, key, value)
                if overwrite and existing != value:
                    if key == "banner":
                        row.local_banner = ""
                    elif key == "still_photo":
                        row.local_still_photo = ""


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
        mosaic="无码" if info.uncensored or preferences.category(info.code) in ('uncensored', 'fc2') else "有码",
        resolution=resolution or next((label for pattern, label in (
            (r'(?<![a-z0-9])(?:8k|4320p)(?![a-z0-9])', '8K'),
            (r'(?<![a-z0-9])(?:4k|2160p)(?![a-z0-9])', '4K'),
            (r'(?<!\d)1080p(?!\d)', '1080P'), (r'(?<!\d)720p(?!\d)', '720P'),
        ) if re.search(pattern, str(info.path), re.I)), ''),
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
                if 'now_printing' in str(response.url).lower():
                    return b''
                return scrape_images.jpeg_bytes(response.content)
        except Exception as exc:
            logger.debug(f"[{code}] 图片下载失败 {url}: {exc}")
            return b""

    workers = min(IMAGE_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fetch, urls))


def _cover_bytes(meta: dict, code: str, refresh: bool = False) -> bytes:
    """取封面内容。

    本地缓存优先：Web UI 那边通常已经缓存过（cache_lack_photos），
    读盘比跨境下载快几个数量级。

    缓存没有时，banner 与 poster 两个来源都下，挑分辨率高的那张 ——
    同一部片各站给的尺寸差别很大（缩略图 300px vs 原图 800px），
    海报要裁一半还要在 Emby 上放大显示，清晰度直接决定观感。
    """
    from app.utils import imagecache

    if meta.get("_local_cover"):
        try:
            data = Path(meta["_local_cover"]).read_bytes()
            if scrape_images.jpeg_bytes(data):
                return data
        except OSError:
            pass

    relative = (meta.get("local_banner") or "").split(",")[0].strip()
    if relative and not refresh:
        path = imagecache.resolve_relative(relative)
        if path is not None and path.is_file():
            try:
                data = path.read_bytes()
                if scrape_images.jpeg_bytes(data):
                    return data
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
        hit = None if refresh else imagecache.find_cached(url, code, kind)
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

    # 横版背景优先保留 banner；不能被面积更大的竖版 poster 替代。
    banner = [(name, data) for name, data in candidates if name.startswith('banner')]
    _, best = merge.pick_best_image(banner)
    if not best:
        _, best = merge.pick_best_image(candidates)
    return best


def _poster_bytes(meta: dict, code: str, refresh: bool = False) -> bytes:
    """保留独立竖版海报，背景始终使用完整封面。"""
    local = _preserved_image(meta.get('_local_poster'))
    if local:
        return local
    url = meta.get('poster') or ''
    if not url or url == meta.get('banner'):
        return b''
    from app.utils import imagecache
    cached = None if refresh else imagecache.find_cached(url, code, 'poster')
    try:
        candidates = [cached.read_bytes()] if cached else _download([url], code)
    except OSError:
        candidates = _download([url], code)
    for data in candidates:
        image = scrape_images._load(data)
        if image is not None:
            try:
                if cached is None:
                    imagecache.store(data, url, code, 'poster')
                return data
            finally:
                image.close()
    return b''


def _preserved_image(path) -> bytes:
    if not path:
        return b''
    try:
        data = Path(path).read_bytes()
        return data if scrape_images.jpeg_bytes(data) else b''
    except OSError:
        return b''


def inspect_source(path: Path | str, subtitle_probe=None, identity_resolver=None) -> MediaFileInfo:
    """统一入库解析，并读取可验证的本地编号及 A/B/C 兄弟分段。"""
    from app.modules.scrape import local
    info = mediafile.parse(path)
    if not info.code and not info.ignored and not info.trailer:
        info.code = local.identify(info.path)
        if info.code:
            info.western = False
    if not info.code and not info.ignored and identity_resolver is not None:
        info.code = identity_resolver.resolve(info.path)
        if info.code:
            info.western = False
    if info.code.endswith('-C') and not info.part:
        siblings = []
        for letter in ('A', 'B'):
            name = re.sub(r'(?<=\d)[-_]?C(?=$|[-_.])', '-' + letter, info.path.stem, count=1, flags=re.I)
            sibling = info.path.with_name(name + info.path.suffix)
            try:
                if sibling != info.path and sibling.is_file():
                    siblings.append(mediafile.parse(sibling))
            except OSError:
                pass
        mediafile.resolve_sibling_parts([info, *siblings])
    if not info.subbed:
        from app.services.subtitle import _has_sidecar
        info.subbed = subtitle_probe.has_sidecar(info.path) if subtitle_probe else _has_sidecar(info.path)
    return info


def group_sources(paths) -> dict[str, list[MediaFileInfo]]:
    from app.services.subtitle import _DirProbe
    from app.services.sourceidentity import SourceIdentityResolver
    groups = {}
    probe = _DirProbe()
    resolver = SourceIdentityResolver()
    infos = [inspect_source(p, probe, resolver) for p in paths]
    mediafile.resolve_sibling_parts(infos)
    for info in infos:
        groups.setdefault(info.code, []).append(info)
    for items in groups.values():
        items.sort(key=lambda i: (i.part, i.path.name))
        mediafile._resolve_collisions(items)
    return groups


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
    from app.services.sourceidentity import SourceIdentityResolver
    info = inspect_source(path, identity_resolver=None if code else SourceIdentityResolver())

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


@_by_code
@maintenance_guarded
def scrape_group(
    code: str,
    items: list[MediaFileInfo],
    fetch_meta: bool = True,
    target_dir: str = "",
    overwrite: bool | None = None,
    refresh_metadata: bool = False,
    existing_targets: dict[str, Path] | None = None,
    preserved_meta: dict | None = None,
    force: bool = False,
) -> ScrapeResult:
    """刮削同一番号的一组文件（可能是多个分集）。

    元数据只抓一次，产物按分集各出一份。这是本模块的核心 ——
    issue #503 的症结就是把分集当成了不同番号。
    """
    started = time.perf_counter()
    result = ScrapeResult(code=code)
    settings = get_settings()
    overwrite = settings.scrape_overwrite if overwrite is None else overwrite
    if force:
        fetch_meta, refresh_metadata, overwrite = True, True, True

    playable = [i for i in items if not i.trailer and not i.ignored]
    dropped = [i for i in items if i.trailer or i.ignored]
    for item in dropped:
        # 预告片不是正片，刮进媒体库会多出一堆几十秒的"影片"
        result.skipped.append(f"{'广告视频' if item.ignored else '预告片'}跳过: {item.path.name}")

    if not playable:
        result.error = "这一组里没有正片"
        return result

    from app.services.mediaduplicate import check_existing
    existing_targets, duplicates = check_existing(code, playable, existing_targets)
    if duplicates:
        result.error = '；'.join(duplicates)
        return result
    actions = getattr(existing_targets, 'actions', {})
    for item in playable:
        destination = existing_targets.get(str(item.path))
        if destination and ' - version-' in destination.stem:
            reason = actions.get(str(item.path), {}).get('reason') or '同番号存在不同内容，自动另存版本'
            result.warnings.append(f'{reason}，已有影片保留: {destination.name}')
    if playable and all(actions.get(str(item.path), {}).get('action') == 'keep' for item in playable):
        from app.services import duplicatepolicy, scrapeinventory
        for item in playable:
            action = actions[str(item.path)]
            target = existing_targets[str(item.path)]
            if action.get('remember'):
                if duplicatepolicy.signature(target) != action['signature']:
                    result.error = '保留影片已变化，请重试'
                    return result
                duplicatepolicy.remember(item.path, target, action['policy'])
            result.links.append(target)
            result.skipped.append(action['reason'] + ': ' + item.path.name)
        scrapeinventory.update_result(playable, code)
        return result

    if force:
        # Force requires a fresh, identity-checked network result. Never turn a
        # failed remote request into a successful scrape using old DB/NFO data.
        fresh = merge.fetch_merged(code)
        result.sources = fresh.get('_sources', []) or getattr(fresh, 'sources', [])
        meta = {'code': code, **merge.clean_detail(fresh, code)}
        if not (meta.get('title') or meta.get('cn_title')):
            result.error = f'强制重新刮削未取得新的网络元数据，保留现有文件: {code}'
            return result
        from app import services
        services._ensure_codes([code])
        _save_meta(code, meta, overwrite=True)
        meta['_sources'] = result.sources
        if fresh.get('_image_candidates'):
            meta['_image_candidates'] = fresh['_image_candidates']
    else:
        meta = (load_group_meta(code, playable, fetch=fetch_meta, refresh=True)
                if refresh_metadata else load_group_meta(code, playable, fetch=fetch_meta))
    if preserved_meta and not force:
        meta = {**{k: v for k, v in preserved_meta.items() if v}, **meta,
                **{k: v for k, v in preserved_meta.items() if k.startswith('_local_') and v}}
    if meta.get('casts'):
        from app.services.actornames import localize_casts
        localized = localize_casts(meta['casts'])
        if localized != meta['casts']:
            meta['casts'] = localized
            _save_meta(code, {'casts': localized})
    if meta.get('_lookup_code'):
        result.warnings.append(f'商品编号 {code} 使用已验证候选 {meta["_lookup_code"]} 获取元数据')
    result.sources = meta.get("_sources", [])
    if meta.get("_refresh_failed"):
        result.warnings.append("本轮未取得新的网络元数据，沿用现有资料；请检查数据源后重试")
    if not (meta.get("title") or meta.get("cn_title")):
        if result.sources and all(s.get('status') == 'not_started' for s in result.sources):
            result.error = f"刮削线程繁忙，等待超时，尚未查询数据源: {code}"
        else:
            result.error = f"取不到元数据: {code}"
        return result

    # 图片对一个番号只准备一份，各分集共用同一份内容
    cover = _cover_bytes(meta, code, refresh=True) if refresh_metadata else _cover_bytes(meta, code)
    if not cover:
        result.warnings.append("未获取到封面，请检查图源和网络后补刮")
    still_urls = [
        u.strip()
        for u in (meta.get("still_photo") or "").split(",")
        if u.strip()
    ][: max(0, settings.scrape_still_limit)]
    stills = [data for path in meta.get('_local_stills', [])
              if (data := _preserved_image(path))][:scrape_images.MAX_STILLS]
    if len(stills) < len(still_urls):
        stills.extend(_download(still_urls[len(stills):], code))
    image_set = scrape_images.build_image_set(
        cover, stills, meta.get("portrait_side") or "",
        crop=scrape_images.should_crop(code, meta, getattr(settings, 'scrape_poster_crop', 'auto')),
        poster=_poster_bytes(meta, code, refresh_metadata),
    )
    thumb = _preserved_image(meta.get('_local_thumb'))
    if thumb:
        image_set.thumb = thumb
    # 一个来源的剧照不可用时，使用本轮校验过身份的其他来源补全。
    for urls in meta.get('_image_candidates', {}).get('still_photo', []):
        if len(image_set.stills) >= len(still_urls):
            break
        alternatives = [u.strip() for u in urls.split(',') if u.strip()][:max(0, settings.scrape_still_limit)]
        candidate = scrape_images.build_image_set(cover, _download(alternatives, code), crop=False)
        if len(candidate.stills) > len(image_set.stills):
            image_set.stills = candidate.stills
    stills_missing = len(image_set.stills) < len(still_urls)
    if stills_missing:
        result.warnings.append(f'剧照下载不完整：{len(image_set.stills)}/{len(still_urls)}')

    preserved_sets = {}
    for part, saved in meta.get('_local_by_part', {}).items():
        part_images = replace(image_set, stills=list(image_set.stills))
        for key, kind in (('_local_cover', 'fanart'), ('_local_poster', 'poster'), ('_local_thumb', 'thumb')):
            data = _preserved_image(saved.get(key))
            if data:
                setattr(part_images, kind, data)
        saved_stills = [data for path in saved.get('_local_stills', [])
                        if (data := _preserved_image(path))]
        if saved_stills:
            part_images.stills = saved_stills[:scrape_images.MAX_STILLS]
        preserved_sets[str(part)] = part_images
    meta['_preserved_image_sets'] = preserved_sets

    result, registered_new = _commit_group(
        code, playable, meta, settings, overwrite, target_dir, existing_targets,
        image_set, stills_missing, result,
    )
    if result.links:
        # 影片已提交；慢的附加任务使用独立有界队列，不占刮削工作线程。
        try:
            from app.services.scrapefollowup import schedule
            from app.modules.notify.scrape import snapshot
            should_notify = bool(result.ok and (result.nfo_written or registered_new) and getattr(settings, 'scrape_notify', False))
            followup = schedule(
                code, str(meta.get('cn_title') or meta.get('title') or ''), len(result.links),
                subtitles=bool(getattr(settings, 'subtitle_enabled', False)),
                review=bool(getattr(settings, 'review_enabled', False)),
                notify=should_notify,
                notification=snapshot(meta,playable,result.links,time.perf_counter()-started) if should_notify else None,
            )
            if followup in ('full', 'error'):
                result.warnings.append("入库完成，后台补全队列已满或不可用；字幕、看点和通知可稍后补全")
        except Exception:
            logger.exception(f"[{code}] 无法提交后台补全")
            result.warnings.append("入库完成，未能提交字幕、看点和通知后台任务")

    logger.info(
        f"[{code}] 刮削{'未完成' if result.error else '完成'}: {len(result.links)} 个产物, "
        f"NFO {result.nfo_written}, 图片 {result.images_written}"
        + (f", 跳过 {len(result.skipped)}" if result.skipped else "")
    )
    return result


@_serialized
def _commit_group(code, playable, meta, settings, overwrite, target_dir,
                  existing_targets, image_set, stills_missing, result):
    """重新核对重复项后提交文件，避免并发抓取期间的库内变化。"""
    from app.services.mediaduplicate import check_existing
    existing_targets, duplicates = check_existing(code, playable, existing_targets)
    if duplicates:
        result.error = '；'.join(duplicates)
        return result, False
    total = len([i for i in playable if i.part > 0])
    linked_items = []
    failures = []
    for info in playable:
        info_total = total
        target = (existing_targets or {}).get(str(info.path)) or _target_path(meta, info, target_dir)
        from app.services import duplicatepolicy
        action = getattr(existing_targets, 'actions', {}).get(str(info.path), {})
        if action.get('action') == 'keep':
            if action.get('remember'):
                if duplicatepolicy.signature(target) != action['signature']:
                    failures.append('保留影片已变化，请重试')
                    continue
                duplicatepolicy.remember(info.path, target, action['policy'])
            result.links.append(target)
            result.skipped.append(action['reason'] + ': ' + info.path.name)
            linked_items.append(info)
            continue
        replacing = action.get('action') == 'replace'

        # 先检查冲突，不能先覆盖另一份视频的图片/NFO。
        try:
            from app.services.mediaduplicate import verified_existing
            reused_copy = (existing_targets.get(str(info.path)) == target and target.exists()
                           and not target.samefile(info.path) and verified_existing(info.path, target))
            conflict = target.exists() and not target.samefile(info.path) and not reused_copy and not replacing
        except OSError as exc:
            failures.append(f'无法检查目标文件: {exc}')
            continue
        if conflict:
            failures.append(f'目标已存在且不是同一文件: {target}')
            continue
        from app.modules.scrape import watermark
        selected_images = meta.get('_preserved_image_sets', {}).get(str(info.part), image_set)
        marked_images = watermark.apply(selected_images, info, getattr(settings, "scrape_watermark", {}))
        written = scrape_images.write_images(
            target, marked_images, overwrite=overwrite
        )
        if written:
            result.images_written += 1
        missing_images = [kind for kind in ("poster", "fanart", "thumb") if not scrape_images.valid_file(target.with_name(f"{target.stem}-{kind}.jpg"))]
        if selected_images.stills and 'stills' not in written:
            missing_images.append('stills')
        if missing_images:
            note = f"图片产物缺失（{', '.join(missing_images)}）: {target.name}"
            result.warnings.append(note)
            if getattr(settings, "scrape_require_images", False):
                failures.append(note)
        if getattr(settings, 'scrape_require_images', False) and (missing_images or stills_missing):
            if stills_missing:
                failures.append(f'剧照下载不完整: {target.name}')
            continue

        data = build_nfo_data(
            code, meta, info,
            total_parts=info_total,
            poster_file=written.get("poster", ""),
            fanart_file=written.get("fanart", ""),
        )
        if nfo.write(
            target.with_suffix(".nfo"), data, overwrite=overwrite
        ):
            result.nfo_written += 1
        else:
            from app.modules.scrape import local
            if overwrite or not local.read(target.with_suffix(".nfo"), code).get("title"):
                note = f"NFO 未能写入或已有 NFO 无效: {target.with_suffix('.nfo')}"
                failures.append(note)
                result.skipped.append(note)
                continue
        # 元数据齐备后最后公开视频，让 Emby 首次扫描即可读到 NFO。
        if reused_copy and not verified_existing(info.path, target):
            failures.append(f'已合并文件发生变化，请重新检查: {target}')
            continue
        if replacing:
            try:
                duplicatepolicy.replace_video(info.path, target, action['signature'])
                ok, note = True, ''
                result.warnings.append(action['reason'] + ': ' + target.name)
                result.warnings.append('替换已启用删除联动保护；请在 Emby 扫描完成后解除保护')
            except (OSError, ValueError) as exc:
                ok, note = False, str(exc)
        else:
            ok, note = _make_link(target if reused_copy else info.path, target)
        if not ok:
            result.skipped.append(note)
            failures.append(note)
            continue
        result.links.append(target)
        linked_items.append(info)

    result.error = "；".join(failures)
    registered_new = False
    if result.links:
        try:
            registered_new = bool(_register(code, linked_items, result))
        except Exception:
            logger.exception(f"[{code}] 入库登记失败")
            failures.append("硬链接关联或入库状态登记失败，请重试；源文件与产物保留")
    if failures:
        result.error = "；".join(failures)
    if result.links:
        try:
            from app.services.scrapeinventory import update_result
            update_result(linked_items, code)
        except Exception:
            logger.warning(f"[{code}] 源视频清单未能更新，后续扫描会重新核对")
    return result, registered_new


def build_nfo_data(
    code: str,
    meta: dict,
    info: MediaFileInfo,
    total_parts: int = 0,
    poster_file: str = "",
    fanart_file: str = "",
) -> nfo.NfoData:
    """组装一份 NFO 数据。

    试算与真正写入共用这一处 —— 各写一遍迟早会对不上，那时试算展示的
    内容就不是最终写进去的东西了，预览也就失去意义。
    """
    return nfo.NfoData(
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
        total_parts=total_parts,
        uncensored=info.uncensored,
        subbed=info.subbed,
        poster_file=poster_file,
        fanart_file=fanart_file,
        extra_genres=_extra_genres(info),
    )


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


def _register(code: str, items: list[MediaFileInfo], result: ScrapeResult) -> bool:
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
    from sqlalchemy import select
    from app.database.models import MediaLink
    registered_new = False
    with session_scope() as session:
        for info, link in linked:
            existing = session.get(MediaLink, str(link))
            registered_new |= existing is None
            from app.services.mediaduplicate import verified_existing
            from app.services.duplicatepolicy import retained
            if existing is not None and retained(info.path, link):
                continue
            if existing is not None and link.exists() and (
                not link.samefile(info.path) and verified_existing(info.path, link)
                or existing.code != code and link.samefile(info.path)
            ):
                # Retain the canonical source; the second source is a verified alias.
                continue
            write_link(code, str(info.path), str(link), session=session)
        if not result.error:
            medialink.mark_completed(code, session=session)

    logger.info(f"[{code}] 已登记 {len(linked)} 条硬链接关联")

    return registered_new


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

    groups = group_sources(videos)
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
