"""转移做种：把 qBittorrent 里已下载完成的种子交给 Transmission 继续做种。

用途是把做种负担从主力下载器挪走 —— qb 负责下，tr 负责长期挂做种，
qb 那边就能腾出任务数与内存去干新活。

做法是导出 .torrent 原文件而不是用磁链：私有站种子没有 DHT，磁链拿不到
metadata。加到 tr 时保存路径对齐 qb 的 save_path，再触发一次校验，tr
认出文件已在本地就直接转做种，不会重下。

只在 tr 确认接管之后才动 qb 的任务，顺序反过来会出现「qb 删了、tr 没接上」
的空窗，那份文件就没人做种了。

文件全程不移动、不复制，两个下载器指向同一份文件。因此转移完成后从 qb
删任务时绝不能删文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from app.core.config import get_settings

# 一轮最多转移多少个的默认值。每个都要导出种子 + 触发校验，tr 校验是磁盘 IO
# 密集操作，一次涌进去几百个会把磁盘打满，反而拖慢正在下载的任务。
# 实际取 SEED_TRANSFER_BATCH_LIMIT，这里只是配置缺失时的兜底
BATCH_LIMIT = 20


def _batch_limit() -> int:
    """本轮转移上限。配成 0 或负数视为不限量，交给用户自己承担 IO 代价。"""
    limit = getattr(get_settings(), "seed_transfer_batch_limit", BATCH_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return BATCH_LIMIT
    # 0 和负数表示不限；用一个大到不会触发的数代替，省得调用方分情况判断
    return limit if limit > 0 else 10**9


@dataclass
class TransferResult:
    """一轮转移的结果。"""
    transferred: list[str] = field(default_factory=list)   # 成功转移的 hash
    skipped: list[str] = field(default_factory=list)       # 不满足条件跳过的
    failed: list[dict] = field(default_factory=list)       # [{hash, reason}]

    @property
    def count(self) -> int:
        return len(self.transferred)

    def to_dict(self) -> dict:
        return {
            "transferred": self.transferred,
            "skipped": self.skipped,
            "failed": self.failed,
            "count": self.count,
        }


def is_available() -> tuple[bool, str]:
    """两端是否都已配置。返回 (可用, 原因)。"""
    settings = get_settings()
    if not settings.qbittorrent_url:
        return False, "未配置 qBittorrent，无法转移做种"
    if not settings.transmission_url:
        return False, "未配置 Transmission，无法转移做种"
    return True, ""


def _clients():
    """返回 (qb, tr)，任一不可用则为 None。"""
    from app.modules.downloadclient import get_download_client

    qb = get_download_client("qbittorrent")
    tr = get_download_client("transmission")
    return qb, tr


def _matches_filter(detail: dict, categories: list[str], tags: list[str]) -> bool:
    """按分类/标签白名单判断这个种子该不该转移。

    两个名单都为空表示不过滤，全部转移。任一命中即可 —— 用户配了分类又配了
    标签时，意图通常是「这些分类或这些标签的都转」，而不是要同时满足。
    """
    if not categories and not tags:
        return True

    if categories and (detail.get("category") or "").strip() in categories:
        return True

    if tags:
        # qb 的 tags 是逗号分隔的一个字符串
        own = {t.strip() for t in (detail.get("tags") or "").split(",") if t.strip()}
        if own & set(tags):
            return True

    return False


def _split_setting(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def transfer_hashes(hashes: list[str], delete_source: bool | None = None) -> TransferResult:
    """把指定的 qb 种子转移到 tr 做种。

    delete_source 为 None 时取配置 SEED_TRANSFER_DELETE_SOURCE。无论如何都
    只删任务不删文件 —— 文件正被 tr 用着。
    """
    result = TransferResult()
    if not hashes:
        return result

    ok, reason = is_available()
    if not ok:
        logger.warning(f"[转移做种] {reason}")
        result.failed = [{"hash": h, "reason": reason} for h in hashes]
        return result

    qb, tr = _clients()
    if qb is None or tr is None:
        result.failed = [{"hash": h, "reason": "下载器初始化失败"} for h in hashes]
        return result

    settings = get_settings()
    if delete_source is None:
        delete_source = settings.seed_transfer_delete_source

    for torrent_hash in hashes:
        detail = qb.get_torrent_detail(torrent_hash)
        if detail is None:
            result.failed.append({"hash": torrent_hash, "reason": "qBittorrent 中找不到该任务"})
            continue

        if detail.get("progress", 0) < 1.0:
            result.skipped.append(torrent_hash)
            logger.debug(f"[转移做种] {torrent_hash} 尚未下载完成，跳过")
            continue

        content = qb.export_torrent(torrent_hash)
        if not content:
            result.failed.append({
                "hash": torrent_hash,
                "reason": "导出种子失败，qBittorrent 需 4.5+",
            })
            continue

        save_path = _map_path(detail.get("save_path") or "")
        if not save_path:
            result.failed.append({"hash": torrent_hash, "reason": "取不到保存路径"})
            continue

        new_hash = tr.add_torrent_for_seeding(
            content,
            save_path=save_path,
            code=detail.get("name", "")[:40],
            labels=_transfer_labels(settings),
            verify=not getattr(settings, "seed_transfer_skip_verify", False),
        )
        if not new_hash:
            result.failed.append({"hash": torrent_hash, "reason": "Transmission 添加失败"})
            continue

        # tr 已接管，这时才动 qb。删文件恒为 False —— 那份文件正是 tr 在做种的
        if delete_source:
            try:
                qb.delete_torrent([torrent_hash], delete_files=False)
            except Exception as exc:
                # 转移本身已成功，qb 的残留任务不该让整条记录判为失败
                logger.warning(f"[转移做种] 从 qBittorrent 删除 {torrent_hash} 失败: {exc}")

        result.transferred.append(torrent_hash)

    if result.transferred:
        logger.info(
            f"[转移做种] 成功 {len(result.transferred)} 个"
            f"（源任务{'已删除' if delete_source else '保留'}），"
            f"跳过 {len(result.skipped)}，失败 {len(result.failed)}"
        )
    return result


def _transfer_labels(settings) -> list[str] | None:
    """转移进 tr 的种子打什么标签。

    单独给一个标签，便于在 tr 里一眼认出哪些是转移来的 —— 这批种子的文件
    不归 tr 管，误删会连累 qb 那边。留空则退回 TRANSMISSION_LABEL。
    """
    label = (settings.seed_transfer_label or "").strip()
    if label:
        return [label]
    return [settings.transmission_label] if settings.transmission_label else None


def _map_path(path: str) -> str:
    """把 qb 的保存路径换算成 tr 看到的路径。

    两个下载器常跑在不同容器里，同一份文件的挂载点不一样
    （qb 是 /downloads/x，tr 是 /data/downloads/x）。SEED_TRANSFER_PATH_MAP
    按 "qb前缀:tr前缀" 配置，多组用逗号分隔。没配就原样返回。
    """
    if not path:
        return ""

    raw = (get_settings().seed_transfer_path_map or "").strip()
    if not raw:
        return path

    for rule in raw.split(","):
        rule = rule.strip()
        if not rule or ":" not in rule:
            continue
        # Windows 路径自带盘符冒号，从右边切才不会把 D: 拆开
        source, _, target = rule.rpartition(":")
        source, target = source.strip(), target.strip()
        if source and path.startswith(source):
            mapped = target + path[len(source):]
            logger.debug(f"[转移做种] 路径映射 {path} → {mapped}")
            return mapped

    return path


def run_auto_transfer() -> int:
    """定时任务入口：扫 qb 里已完成的种子，按配置自动转移。返回转移数量。"""
    settings = get_settings()
    if not settings.seed_transfer_enabled:
        logger.debug("[转移做种] 未开启（SEED_TRANSFER_ENABLED=false），跳过")
        return 0

    ok, reason = is_available()
    if not ok:
        logger.warning(f"[转移做种] {reason}")
        return 0

    qb, _ = _clients()
    if qb is None:
        return 0

    try:
        rows = qb.monitor_torrent()
    except Exception as exc:
        logger.warning(f"[转移做种] 读取 qBittorrent 任务列表失败: {exc}")
        return 0

    categories = _split_setting(settings.seed_transfer_categories)
    tags = _split_setting(settings.seed_transfer_tags)
    limit = _batch_limit()

    candidates: list[str] = []
    for row in rows:
        if not row.get("completed"):
            continue
        torrent_hash = row.get("hash") or ""
        if not torrent_hash:
            continue

        # 过滤要看分类和标签，monitor_torrent 不返回这两项，只能逐个查详情。
        # 没配过滤条件时省掉这一步查询
        if categories or tags:
            detail = qb.get_torrent_detail(torrent_hash)
            if detail is None or not _matches_filter(detail, categories, tags):
                continue

        candidates.append(torrent_hash)
        if len(candidates) >= limit:
            break

    if not candidates:
        return 0

    logger.info(f"[转移做种] 本轮候选 {len(candidates)} 个已完成任务")
    result = transfer_hashes(candidates)
    return result.count


def list_candidates(limit: int | None = None) -> list[dict]:
    """列出可转移的 qb 已完成任务，供助手与前端展示。

    limit 留空取配置里的单轮上限。默认值不能写成 BATCH_LIMIT —— 那会在函数
    定义时就定死，用户改了配置也不生效。

    只看 qb 那一侧：种子是否已在 tr 里，转移时 add_torrent_for_seeding 会
    自行识别重复，这里不必先问一遍 tr —— 那要把 tr 全量拉一次，代价不小。
    """
    if limit is None:
        limit = _batch_limit()

    ok, reason = is_available()
    if not ok:
        return []

    qb, _ = _clients()
    if qb is None:
        return []

    settings = get_settings()
    categories = _split_setting(settings.seed_transfer_categories)
    tags = _split_setting(settings.seed_transfer_tags)

    try:
        rows = qb.monitor_torrent()
    except Exception as exc:
        logger.warning(f"[转移做种] 读取 qBittorrent 任务列表失败: {exc}")
        return []

    out: list[dict] = []
    for row in rows:
        if not row.get("completed"):
            continue
        torrent_hash = row.get("hash") or ""
        if not torrent_hash:
            continue
        if categories or tags:
            detail = qb.get_torrent_detail(torrent_hash)
            if detail is None or not _matches_filter(detail, categories, tags):
                continue
        out.append({
            "hash": torrent_hash,
            "name": row.get("name", ""),
            "save_path": row.get("save_path", ""),
        })
        if len(out) >= limit:
            break

    return out
