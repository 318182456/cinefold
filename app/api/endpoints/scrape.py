"""自建刮削。

替代外部刮削工具（MDCng 等）：抓元数据 → 建硬链接 → 写 NFO → 落图，
再顺带抓字幕、生成 AI 看点。

刮削会真的往媒体库写文件，所以：
  - 单文件/目录两个入口都支持 dry_run，先看会产出什么再动手
  - 覆盖行为由 SCRAPE_OVERWRITE 控制，默认只补缺失的，
    不冲掉用户手改过的 NFO 与换过的图
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.endpoints import get_current_user
from app.api.endpoints.watchdir import _check_same_fs
from app.core.config import get_settings
from app.schemas.reponse import ResponseEntity
from app.services import scrape as scrape_service
from app.utils import get_true_code, mediafile

router = APIRouter(prefix="/scrape", tags=["刮削"])


class ScrapeRequest(BaseModel):
    # 影片文件或目录的绝对路径。
    # 试算时可以留空，只填 code —— 那时算的是「假如有这么一部片，
    # 产物会落在哪、叫什么名字」，用来验证命名模板，不需要真有文件
    path: str = ""
    # 只试算，不建链接不写文件
    dry_run: bool = False
    # 库里已有元数据时是否仍去抓。关掉能省跨境请求，重刮时常用
    fetch_meta: bool = True
    # 人工指定番号。文件名认不出来时用（欧美片、改过名的文件、
    # 番号只写在种子名里的）。
    #
    # 传文件路径时：这个文件按指定番号刮。
    # 传目录时：只认领目录里认不出番号的文件，已认出的不动 ——
    # 否则一次指定会把整个下载目录刮成同一部片
    code: str = ""
    # 本次的硬链接根目录，覆盖设置里的「刮削输出目录」。
    # 留空用全局配置。用于把某一批片子刮到别处（按厂牌分库、
    # 临时刮到另一块盘、或拿空目录试跑看产物长什么样）
    target_dir: str = ""


def _explain_missing(target: Path) -> str:
    """路径不存在时，指出到底断在哪一层。

    光说「路径不存在」没法排查 —— 用户在 NAS 上看得见这个文件，
    容器里却没有，最常见的原因是那一层根本没挂进容器，而不是路径写错。
    实测就踩过：compose 里加了 /volume3 的挂载但容器没重建，
    restart 不会应用新挂载，必须 up -d 重建。

    逐层往上找，报出「最深的那个存在的祖先」，用户一眼就能看出是
    第几层断的：报 / 说明整个 /volume3 没挂，报 /volume3/h_video
    说明挂了但下面那层名字对不上。
    """
    existing = None
    for parent in target.parents:
        try:
            if parent.exists():
                existing = parent
                break
        except OSError:
            continue

    if existing is None:
        return f"路径不存在，且找不到任何可访问的上级目录: {target}"

    # 列出断点那一层实际有什么，名字打错时直接就看出来了
    hint = ""
    try:
        names = sorted(p.name for p in existing.iterdir())
        if names:
            shown = "、".join(names[:8])
            more = f" 等 {len(names)} 项" if len(names) > 8 else ""
            hint = f"。该目录下现有: {shown}{more}"
    except OSError as exc:
        hint = f"。该目录无法列出（{exc.strerror or exc}），可能是权限问题"

    # 断在最后一层（父目录在、只有这个文件/目录名不存在）时，多半是名字
    # 写错或文件已被移走，跟挂载没关系 —— 这时再说「检查挂载」是误导
    if existing == target.parent:
        return f"容器内找不到: {target.name}（所在目录存在）{hint}"

    return (
        f"容器内找不到这个路径: {target}。"
        f"最深能访问到的是 {existing}{hint}。"
        "宿主机上有而容器里没有，通常是这一层没挂进容器 —— "
        "改完 docker-compose.yml 的 volumes 要用 `docker compose up -d` 重建，"
        "restart 不会应用新挂载"
    )


def _result_dict(result: scrape_service.ScrapeResult) -> dict:
    return {
        "code": result.code,
        "ok": result.ok,
        "links": [str(p) for p in result.links],
        "nfo_written": result.nfo_written,
        "images_written": result.images_written,
        "skipped": result.skipped,
        "error": result.error,
    }


@router.post("/preview")
async def preview(body: ScrapeRequest, current_user: str = Depends(get_current_user)):
    """试算：这个路径会被解析成什么、产物落在哪，不动任何文件。

    接入前先跑这个 —— 命名模板写错时，产出的目录结构要等刮完才发现就晚了。

    fetch_meta 为真时会真的联网检索元数据，慢（首次几十秒），
    所以整个函数放线程池跑，别占着事件循环
    """
    return await run_in_threadpool(_preview_sync, body)


def _preview_sync(body: ScrapeRequest) -> dict:
    settings = get_settings()
    manual = get_true_code(body.code) if body.code else ""
    if body.code and not manual:
        return ResponseEntity.fail(f"指定的番号不是合法格式: {body.code}", code=400)

    # 只给番号不给路径：算「假如有这么一部片，产物会落在哪」。
    # 用来验证命名模板，不需要真有文件 —— 改完模板想看效果时，
    # 不必先去找一个实际存在的影片
    if not body.path.strip():
        if not manual:
            return ResponseEntity.fail(
                "请填写影片路径，或只填一个番号试算命名效果", code=400,
            )
        return _preview_code_only(manual, body, settings)

    target = Path(body.path)
    if not target.exists():
        return ResponseEntity.fail(_explain_missing(target), code=400)

    if target.is_file():
        videos = [target]
    else:
        from app.services.medialink import is_adoptable_video

        videos = [
            p for p in target.rglob("*")
            if p.is_file() and is_adoptable_video(p)
        ]

    groups = mediafile.group_parts(videos)
    pending = groups.pop("", [])

    if manual and pending:
        # 与 scrape_dir 同一规则：只认领认不出的，已认出的不动
        for info in pending:
            info.code = manual
        merged = groups.setdefault(manual, [])
        merged.extend(pending)
        merged.sort(key=lambda i: (i.part, i.path.name))
        mediafile._resolve_collisions(merged)
        pending = []

    unknown = [
        {"path": str(i.path), "western": i.western} for i in pending
    ]

    items = []
    # 抓取按番号去重：分集共用一份元数据，逐个文件抓等于同一番号抓好几遍。
    #
    # fetch_meta 为真时真的联网检索 —— 页面上那个勾选就是这个意思，
    # 不抓的话「本地无元数据」的番号永远显示不出标题演员封面，
    # 用户也就无从判断刮得对不对（原来这里写死了 fetch=False，
    # 那个勾选形同虚设）
    meta_cache: dict[str, dict] = {}
    for code, infos in groups.items():
        meta = meta_cache.get(code)
        if meta is None:
            meta = scrape_service._load_meta(code, fetch=body.fetch_meta)
            meta_cache[code] = meta
        for info in infos:
            target = scrape_service._target_path(meta, info, body.target_dir)
            preview_images = {} if info.trailer else _preview_images(meta, settings)
            items.append({
                "code": code,
                "source": str(info.path),
                "part": info.part,
                "trailer": info.trailer,
                "subbed": info.subbed,
                "uncensored": info.uncensored,
                "target": str(target),
                "has_meta": bool(meta),
                # NFO 的实际内容与图片地址。让用户直接看到会写进去什么，
                # 而不是只看到文件名 —— 元数据抓得对不对，看内容才知道。
                # 预告片不会被刮，列出这些只会误导
                "nfo": "" if info.trailer else _preview_nfo(meta, info, target),
                "images": preview_images,
                "outputs": (
                    [] if info.trailer
                    else _planned_outputs(target, settings, preview_images)
                ),
            })

    return ResponseEntity.ok({
        "items": items,
        "unknown": unknown,
        "dir_template": settings.scrape_dir_template,
        "file_template": settings.scrape_file_template,
        "warnings": _warn_paths(items),
    })


def _preview_code_only(code: str, body: ScrapeRequest, settings) -> dict:
    """只给番号的试算：假设有这么一部片，算产物路径与文件清单。

    用来验证命名模板 —— 改完模板想看效果，不必先去找一个真实存在的影片。
    fetch_meta 为真时真的去检索：只填番号想看这部片的情报，本来就该
    联网抓，否则库里没有的番号只能显示成一堆「未知」。
    """
    from app.utils.mediafile import MediaFileInfo

    meta = scrape_service._load_meta(code, fetch=body.fetch_meta)
    # 造一个假的源文件：文件名就是番号，后缀取最常见的 .mp4。
    # 目录用番号本身，让 {source_filename} 之类的字段也算得出来
    info = MediaFileInfo(path=Path(f"{code}/{code}.mp4"), code=code)
    target = scrape_service._target_path(meta, info, body.target_dir)
    images = _preview_images(meta, settings)

    item = {
        "code": code,
        "source": "",
        "part": 0,
        "trailer": False,
        "subbed": False,
        "uncensored": False,
        "target": str(target),
        "has_meta": bool(meta),
        "outputs": _planned_outputs(target, settings, images),
        "nfo": _preview_nfo(meta, info, target),
        "images": images,
    }
    return ResponseEntity.ok({
        "items": [item],
        "unknown": [],
        "dir_template": settings.scrape_dir_template,
        "file_template": settings.scrape_file_template,
        "warnings": _warn_paths([item]),
        # 前端据此提示「这是假设的路径，没有真实文件」
        "code_only": True,
    })


def _preview_nfo(meta: dict, info, target: Path) -> str:
    """渲染这个产物的 NFO 内容，原样返回 XML 文本。

    与真正写入共用 build_nfo_data + nfo.render，所以看到的就是
    最终会落盘的那份 —— 元数据抓得对不对，看文件名看不出来，
    得看内容。
    """
    from app.modules.scrape import nfo

    data = scrape_service.build_nfo_data(
        info.code, meta, info,
        poster_file=f"{target.stem}-poster.jpg",
        fanart_file=f"{target.stem}-fanart.jpg",
    )
    try:
        return nfo.render(data).decode("utf-8")
    except Exception as exc:  # 渲染失败也要能看到原因，而不是空白
        return f"<!-- NFO 渲染失败: {exc} -->"


def _preview_images(meta: dict, settings) -> dict:
    """图片的可访问地址，给前端直接显示缩略图。

    走 /image-proxy 而不是直接给源站 URL：图源有防盗链，浏览器直连
    会拿到 403。本地已缓存的走 /image-local，省一次回源。

    海报给的是**裁好的竖版**（poster=1），与真正落进媒体库的那张一致 ——
    源站封面多是横版双拼图，显示原图看不出 Emby 里最终长什么样。
    背景（fanart）用原图，刮削时也不裁。
    """
    from app.utils import imagecache

    code = meta.get("code") or ""

    def resolve(url: str, kind: str, poster: bool = False) -> str:
        if not url:
            return ""
        suffix = "&poster=1" if poster else ""
        # 缓存命中就走本地，不必再跨境
        hit = imagecache.find_cached(url, code, kind)
        if hit is not None:
            path = quote(imagecache.relative_of(hit))
            return f"/api/v1/image-local?path={path}{suffix}"
        return (
            f"/api/v1/image-proxy?url={quote(url, safe='')}"
            f"&code={quote(code)}&kind={kind}{suffix}"
        )

    relative = (meta.get("local_banner") or "").split(",")[0].strip()
    if relative:
        local = f"/api/v1/image-local?path={quote(relative)}"
        poster, fanart = f"{local}&poster=1", local
    else:
        source = meta.get("banner") or meta.get("poster") or ""
        poster = resolve(source, "banner", poster=True)
        fanart = resolve(source, "banner")

    stills = [
        resolve(u.strip(), "still")
        for u in (meta.get("still_photo") or "").split(",")
        if u.strip()
    ][: max(0, settings.scrape_still_limit)]

    return {
        # cover 保留原名，前端旧版本仍能显示；语义上它就是 fanart
        "cover": fanart,
        "poster": poster,
        "fanart": fanart,
        "stills": [s for s in stills if s],
    }


def _planned_outputs(target: Path, settings, images: dict) -> list[dict]:
    """这个产物会写出哪些文件。硬链接 + NFO + 图片，按写入顺序。

    试算的意义就在这里 —— 让用户在开刮前看全会往媒体库里放什么。
    只列文件名（同目录），路径已经在 target 里给过了。

    images 是 _preview_images 的结果，用来决定图片那几项要不要列 ——
    没有封面地址就不会有海报/背景/缩略图，剧照同理。照列会让人以为
    刮完能拿到 10 个文件，实际只出 2 个。
    """
    from app.modules.scrape import images as scrape_images

    outputs = [
        {"kind": "hardlink", "name": target.name},
        {"kind": "nfo", "name": target.with_suffix(".nfo").name},
    ]

    has_cover = bool(images.get("poster") or images.get("fanart"))
    still_count = len(images.get("stills") or [])
    for kind, name in scrape_images.planned_names(target, still_count):
        # 海报/背景/缩略图都来自那张封面，没封面就一个都没有
        if kind != "still" and not has_cover:
            continue
        outputs.append({"kind": kind, "name": name})
    return outputs


def _warn_paths(items: list[dict]) -> list[str]:
    """挑出一看就不对劲的产物路径。

    目前只查一种：路径里出现连续重复的目录名（日本AV/日本AV/…）。
    这是「刮削输出目录」已经以分类名结尾、模板里又写了一次 {category}
    造成的，配置上很自然就会踩到，而刮完才发现就得手工挪文件了。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        parts = Path(item["target"]).parts
        dup = next(
            (a for a, b in zip(parts, parts[1:]) if a == b and a not in ("/", "\\")),
            "",
        )
        if dup and dup not in seen:
            seen.add(dup)
            out.append(
                f"产物路径里「{dup}」重复了一层。多半是「刮削输出目录」"
                f"已经以 {dup} 结尾，目录模板里又写了一次 —— "
                "把模板里对应那段去掉，或把输出目录改到上一层"
            )
    return out


@router.post("/run")
async def run(body: ScrapeRequest, current_user: str = Depends(get_current_user)):
    """执行刮削。文件或目录都可以。"""
    target = Path(body.path)
    if not target.exists():
        return ResponseEntity.fail(_explain_missing(target), code=400)

    if body.dry_run:
        return await preview(body, current_user)

    settings = get_settings()
    root = (
        body.target_dir
        or settings.medialink_scrape_dir
        or settings.medialink_library_path
    )
    if not root:
        return ResponseEntity.fail(
            "未配置媒体库目录，刮削产物无处可放。"
            "请先设置「刮削输出目录」，或在本次刮削时指定硬链接目录",
            code=400,
        )

    # 硬链接建不了跨文件系统的。配置阶段就拦住，比刮到一半每个文件
    # 报一次 EXDEV 好排查 —— 复用监控目录那套校验，判据一致
    ok, message = _check_same_fs(target, Path(root))
    if not ok:
        return ResponseEntity.fail(message, code=400)

    logger.info(f"[刮削] 手动触发: {body.path}")
    # 抓取、建链接、写文件全是阻塞 IO，别占着事件循环
    if target.is_file():
        results = [
            await run_in_threadpool(
                scrape_service.scrape_file, target, body.fetch_meta, body.code,
                body.target_dir
            )
        ]
    else:
        results = await run_in_threadpool(
            scrape_service.scrape_dir, target, body.fetch_meta, body.code,
            body.target_dir
        )

    return ResponseEntity.ok({
        "total": len(results),
        "ok": sum(1 for r in results if r.ok),
        "results": [_result_dict(r) for r in results],
    })


@router.get("/fields")
def fields(current_user: str = Depends(get_current_user)):
    """命名模板可用的字段清单，给前端做提示用。"""
    from app.modules.scrape.naming import FIELDS, HAS_JINJA

    return ResponseEntity.ok({
        "fields": [{"name": k, "desc": v} for k, v in FIELDS.items()],
        # 前端据此决定要不要提示「高级语法不可用」
        "jinja_available": HAS_JINJA,
    })
