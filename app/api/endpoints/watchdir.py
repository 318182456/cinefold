"""监控目录管理。

一条规则 = 「源目录 → 媒体库子目录」的硬链接同步。增删改规则后监听会重建。

同步真的会动文件（反向删除还会删种子和源文件），所以每条写操作都配了
dry_run 预览入口。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from app.api.endpoints import get_current_user
from app.core.config import get_settings
from app.database.models import WatchDir
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.services.watchdir import (
    backfill_torrents, cancel_hold, list_holds, sync_all, sync_rule,
)

router = APIRouter(prefix="/watchdirs", tags=["监控目录"])


class WatchDirRequest(BaseModel):
    source_dir: str
    # 目标目录绝对路径。填了就用它，与 MEDIALINK_LIBRARY_PATH 无关
    target_dir: str = ""
    # 旧方式：相对媒体库根目录的子目录名。target_dir 为空时才用
    target_subdir: str = ""
    name: str = ""
    enabled: bool = True
    recursive: bool = True
    reverse_delete: bool = False
    code_prefix: str = ""


class WatchDirUpdate(BaseModel):
    """部分更新。未传的字段保持原值。"""
    source_dir: str | None = None
    target_dir: str | None = None
    target_subdir: str | None = None
    name: str | None = None
    enabled: bool | None = None
    recursive: bool | None = None
    reverse_delete: bool | None = None
    code_prefix: str | None = None


def _reload_watcher() -> None:
    """规则变更后重建监听。失败不影响接口返回 —— 定时对账仍会工作。"""
    try:
        from app.modules.watcher import restart_watching
        restart_watching()
    except Exception as exc:
        logger.warning(f"重建目录监听失败: {exc}")


@router.get("")
def list_watchdirs(current_user: str = Depends(get_current_user)):
    """规则列表，附带运行环境的校验结果。"""
    with session_scope() as session:
        rows = [r.to_dict() for r in session.scalars(
            select(WatchDir).order_by(WatchDir.create_time.desc())
        ).all()]

    settings = get_settings()
    library = settings.medialink_library_path

    # 页面要据此提示配置问题
    for item in rows:
        item["source_exists"] = Path(item["source_dir"]).is_dir()
        # 目标目录按 target_dir → 库根+子目录 的优先级算出来给页面直接显示，
        # 免得前端再重复一遍这个回退逻辑
        ok, _, target = _resolve_target(
            item["target_dir"], item["target_subdir"]
        )
        item["resolved_target"] = str(target) if ok else ""
        item["target_exists"] = bool(ok) and target.is_dir()

    # 刮削输出目录也列出来，标记为受保护 —— 它由刮削工具写入、由 webhook 联动
    # 维护，不是监控规则，但用户需要看得见「这个目录也在管辖范围内」，
    # 且不该能从这里删掉它。留空时退回库根（两者等同的旧配置）。
    # 放在列表最前，用 id=0 与真实规则区分（真实规则 id 从 1 起）
    #
    # source_dir 留空是有意为之：刮削没有固定源目录 —— 源文件在哪由刮削工具
    # 逐条通过 webhook 上报，一个番号一个位置。填成输出目录会显示出
    # 「源目录 = 目标目录」这种自相矛盾的结果，页面据 source_dir 为空
    # 改显示成「由刮削 webhook 逐条登记」
    scrape_dir = (settings.medialink_scrape_dir or "").strip() or library
    if scrape_dir:
        rows.insert(0, {
            "id": 0,
            "protected": True,
            "source_dir": "",
            "target_dir": scrape_dir,
            "target_subdir": "",
            "resolved_target": scrape_dir,
            "name": "刮削输出目录",
            "enabled": True,
            "recursive": True,
            "reverse_delete": settings.medialink_delete_enabled,
            "code_prefix": "",
            "source_exists": False,
            "target_exists": Path(scrape_dir).is_dir(),
            "last_scan_time": "",
            "file_count": 0,
            "last_error": "",
            "create_time": "",
        })

    from app.modules.watcher import is_watching

    return ResponseEntity.ok({
        "items": rows,
        "library_path": library,
        "library_exists": bool(library) and Path(library).is_dir(),
        "delete_enabled": settings.medialink_delete_enabled,
        "watching": is_watching(),
    })


@router.post("")
def create_watchdir(
    body: WatchDirRequest, current_user: str = Depends(get_current_user)
):
    """新增规则。源目录必须存在，且与目标目录在同一文件系统。"""
    source_dir = (body.source_dir or "").strip().rstrip("/\\")
    if not source_dir:
        return ResponseEntity.fail("源目录不能为空", code=400)

    source = Path(source_dir)
    if not source.is_dir():
        return ResponseEntity.fail(f"源目录不存在或不可读: {source_dir}", code=400)

    target_dir = (body.target_dir or "").strip().rstrip("/\\")
    target_subdir = (body.target_subdir or "").strip().strip("/\\")

    ok, message, target = _resolve_target(target_dir, target_subdir)
    if not ok:
        return ResponseEntity.fail(message, code=400)

    # 硬链接不能跨文件系统，配置时就该拦住，而不是等同步时每个文件报一次错
    ok, message = _check_same_fs(source, target)
    if not ok:
        return ResponseEntity.fail(message, code=400)

    with session_scope() as session:
        existing = session.scalar(
            select(WatchDir).where(WatchDir.source_dir == source_dir)
        )
        if existing is not None:
            return ResponseEntity.fail("该源目录已配置", code=400)

        session.add(WatchDir(
            source_dir=source_dir,
            target_dir=target_dir,
            target_subdir=target_subdir,
            name=(body.name or "").strip() or source.name,
            enabled=body.enabled,
            recursive=body.recursive,
            reverse_delete=body.reverse_delete,
            code_prefix=(body.code_prefix or "").strip(),
        ))

    _reload_watcher()
    return ResponseEntity.ok(message="已添加监控目录")


# 固定路径的路由必须声明在 /{rule_id} 之前 —— FastAPI 按声明顺序匹配，
# 否则 /watchdirs/holds 会先命中 /{rule_id} 并因 int 解析失败返回 422
@router.post("/sync")
def sync_every(
    dry_run: bool = True, current_user: str = Depends(get_current_user)
):
    """同步全部启用的规则。"""
    results = sync_all(dry_run=dry_run)
    return ResponseEntity.ok({
        "results": [r.as_dict() for r in results],
        "count": len(results),
    })


@router.post("/backfill")
def backfill(watch_id: int = 0, current_user: str = Depends(get_current_user)):
    """给还没登记种子的关联补查一次下载器。

    建链接那一刻种子可能还不在下载器里（下载未完成、完成后才移入监控目录、
    事后做种），此时 History 是空的，删除时就查不到种子。这个接口补上。
    定时对账里也会自动跑一次。
    """
    added = backfill_torrents(watch_id)
    return ResponseEntity.ok(
        {"added": added}, message=f"补登记 {added} 个种子"
    )


@router.get("/holds")
def list_pending_deletes(
    watch_id: int = 0, current_user: str = Depends(get_current_user)
):
    """扣留中的删除：文件已消失，但还在宽限期内观察，尚未删除。

    宽限期内同 inode 的文件在别处出现就判定为移动，不会真删。这个列表让用户
    看得见「系统正在观察什么」，也能手动撤销。
    """
    return ResponseEntity.ok({
        "items": list_holds(watch_id),
        "grace_seconds": get_settings().watchdir_delete_grace,
    })


@router.delete("/holds")
def cancel_pending_delete(
    link_path: str, current_user: str = Depends(get_current_user)
):
    """撤销一条扣留 —— 确认这个文件不该被删。

    撤销后下一轮对账若仍发现文件消失，会重新开始计时。要彻底不删请关掉
    规则的反向删除，或把文件放回去。
    """
    if not link_path:
        return ResponseEntity.fail("缺少 link_path", code=400)
    if not cancel_hold(link_path):
        return ResponseEntity.fail("扣留记录不存在", code=404)
    return ResponseEntity.ok(message="已撤销扣留")


@router.put("/{rule_id}")
def update_watchdir(
    rule_id: int, body: WatchDirUpdate,
    current_user: str = Depends(get_current_user),
):
    """修改规则。改了源目录或目标目录都会重新校验文件系统。"""
    if rule_id == 0:
        # 列表里 id=0 是刮削输出目录的占位项，不是真规则
        return ResponseEntity.fail(
            "刮削输出目录不是监控规则，请在「设置 → 媒体联动」修改", code=400
        )

    with session_scope() as session:
        row = session.get(WatchDir, rule_id)
        if row is None:
            return ResponseEntity.fail("规则不存在", code=404)

        if body.source_dir is not None:
            source_dir = body.source_dir.strip().rstrip("/\\")
            if not source_dir:
                return ResponseEntity.fail("源目录不能为空", code=400)
            if not Path(source_dir).is_dir():
                return ResponseEntity.fail(f"源目录不存在或不可读: {source_dir}", code=400)

            clash = session.scalar(
                select(WatchDir).where(
                    WatchDir.source_dir == source_dir, WatchDir.id != rule_id
                )
            )
            if clash is not None:
                return ResponseEntity.fail("该源目录已被另一条规则占用", code=400)
            row.source_dir = source_dir

        if body.target_dir is not None:
            row.target_dir = body.target_dir.strip().rstrip("/\\")
        if body.target_subdir is not None:
            row.target_subdir = body.target_subdir.strip().strip("/\\")

        # 源目录或目标目录任一改动，都要按改后的组合重新校验 ——
        # 只在改源目录时校验的话，单独改目标目录能绕过跨文件系统检查
        if body.source_dir is not None or body.target_dir is not None \
                or body.target_subdir is not None:
            ok, message, target = _resolve_target(row.target_dir, row.target_subdir)
            if not ok:
                return ResponseEntity.fail(message, code=400)
            ok, message = _check_same_fs(Path(row.source_dir), target)
            if not ok:
                return ResponseEntity.fail(message, code=400)
        if body.name is not None:
            row.name = body.name.strip()
        if body.enabled is not None:
            row.enabled = body.enabled
        if body.recursive is not None:
            row.recursive = body.recursive
        if body.reverse_delete is not None:
            row.reverse_delete = body.reverse_delete
        if body.code_prefix is not None:
            row.code_prefix = body.code_prefix.strip()

    _reload_watcher()
    return ResponseEntity.ok(message="已更新")


@router.delete("/{rule_id}")
def delete_watchdir(
    rule_id: int, current_user: str = Depends(get_current_user)
):
    """删除规则。只删规则本身，已建立的硬链接与 media_link 记录都保留。

    要清理硬链接请去硬链接管理页 —— 删规则是「不再自动同步」的意思，
    不该顺手删掉用户媒体库里的文件。
    """
    if rule_id == 0:
        # 刮削输出目录的占位项，删不得
        return ResponseEntity.fail(
            "刮削输出目录受保护，不能从这里删除。如需变更请到「设置 → 媒体联动」修改",
            code=400,
        )

    with session_scope() as session:
        row = session.get(WatchDir, rule_id)
        if row is None:
            return ResponseEntity.fail("规则不存在", code=404)
        session.delete(row)

    _reload_watcher()
    return ResponseEntity.ok(message="已删除规则，已建立的硬链接保留")


@router.post("/{rule_id}/sync")
def sync_one(
    rule_id: int, dry_run: bool = True,
    current_user: str = Depends(get_current_user),
):
    """手动同步一条规则。dry_run 默认为真，只报会做什么。"""
    if rule_id == 0:
        return ResponseEntity.fail(
            "刮削输出目录由刮削工具与 webhook 联动维护，没有可同步的监控规则",
            code=400,
        )

    result = sync_rule(rule_id, dry_run=dry_run)
    if result.errors and not (result.linked or result.unlinked):
        return ResponseEntity.fail(
            "; ".join(result.errors[:3]), code=400, data=result.as_dict()
        )
    return ResponseEntity.ok(result.as_dict())


def _resolve_target(
    target_dir: str, target_subdir: str
) -> tuple[bool, str, Path]:
    """算出目标目录。返回 (是否可用, 错误信息, 目标路径)。

    target_dir 优先；为空时回退到「媒体库根目录 + 子目录」的旧方式。
    两者都没有就报错 —— 不知道往哪建链接。
    """
    if target_dir:
        target = Path(target_dir)
        if not target.is_absolute():
            return False, f"目标目录必须是绝对路径: {target_dir}", target
        return True, "", target

    library = get_settings().medialink_library_path
    if not library:
        return False, (
            "请填写目标目录，或先在设置里配置媒体库根目录"
            "（MEDIALINK_LIBRARY_PATH）"
        ), Path()

    base = Path(library)
    return True, "", base / target_subdir if target_subdir else base


def _existing_ancestor(path: Path) -> Path | None:
    """往上找到第一个已存在的祖先目录。

    目标目录常常还没建出来（首次配置），此时无法直接 stat。但它的父目录
    必定在某一层已存在，拿那一层判断文件系统是等价的 —— 同一目录树内
    不会跨文件系统，除非中间挂了别的卷，那种情况罕见且建链接时会报错。
    """
    try:
        current = path if path.is_absolute() else path.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _check_same_fs(source: Path, target: Path) -> tuple[bool, str]:
    """源目录与目标目录是否在同一文件系统。

    硬链接的硬性限制：跨文件系统建不了。配置阶段就拦住，比同步时每个文件
    报一次 EXDEV 好排查。取不到 st_dev（权限、路径不存在）时放过 ——
    宁可让同步时报错，也不要因为探测失败挡住合法配置。

    目标目录还不存在时，拿最近的已存在祖先来比 —— 首次配置时目录多半
    还没建，直接放过就等于这道校验形同虚设。
    """
    probe = _existing_ancestor(target)
    if probe is None:
        return True, ""  # 连祖先都找不到，留给同步时报
    try:
        if source.stat().st_dev != probe.stat().st_dev:
            return False, (
                f"源目录与目标目录不在同一文件系统，无法建硬链接。"
                f"请确认两者在 Docker 里挂载自同一个卷"
                f"（对比依据: {probe}）"
            )
    except OSError:
        return True, ""
    return True, ""
