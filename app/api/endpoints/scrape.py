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
    # 影片文件或目录的绝对路径
    path: str
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
def preview(body: ScrapeRequest, current_user: str = Depends(get_current_user)):
    """试算：这个路径会被解析成什么、产物落在哪，不动任何文件。

    接入前先跑这个 —— 命名模板写错时，产出的目录结构要等刮完才发现就晚了。
    """
    target = Path(body.path)
    if not target.exists():
        return ResponseEntity.fail(f"路径不存在: {body.path}", code=400)

    settings = get_settings()
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

    manual = get_true_code(body.code) if body.code else ""
    if body.code and not manual:
        return ResponseEntity.fail(f"指定的番号不是合法格式: {body.code}", code=400)

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
    for code, infos in groups.items():
        # 试算不抓网络：只用库里已有的元数据，否则点一下预览就跨境请求几十次
        meta = scrape_service._load_meta(code, fetch=False)
        for info in infos:
            items.append({
                "code": code,
                "source": str(info.path),
                "part": info.part,
                "trailer": info.trailer,
                "subbed": info.subbed,
                "uncensored": info.uncensored,
                "target": str(scrape_service._target_path(meta, info, body.target_dir)),
                "has_meta": bool(meta),
            })

    return ResponseEntity.ok({
        "items": items,
        "unknown": unknown,
        "dir_template": settings.scrape_dir_template,
        "file_template": settings.scrape_file_template,
    })


@router.post("/run")
async def run(body: ScrapeRequest, current_user: str = Depends(get_current_user)):
    """执行刮削。文件或目录都可以。"""
    target = Path(body.path)
    if not target.exists():
        return ResponseEntity.fail(f"路径不存在: {body.path}", code=400)

    if body.dry_run:
        return preview(body, current_user)

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
