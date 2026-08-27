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

候选范围刻意不受 QBITTORRENT_CATEGORY 约束：那个配置是给下载与状态同步用的，
拿它当转移范围会把「早期没设分类、手动加进 qb、分类名大小写不同」的种子
静默挡在候选之外。该转哪些一律由 SEED_TRANSFER_CATEGORIES / _TAGS 决定。

只转 100% 且文件已就位的种子，见 _is_transferable 与 UNSAFE_STATES。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from loguru import logger

from app.core.config import get_settings

# 一轮最多转移多少个的默认值。每个都要导出种子 + 触发校验，tr 校验是磁盘 IO
# 密集操作，一次涌进去几百个会把磁盘打满，反而拖慢正在下载的任务。
# 实际取 SEED_TRANSFER_BATCH_LIMIT，这里只是配置缺失时的兜底
BATCH_LIMIT = 20


# 进度到了 100% 也不代表可以交给 tr —— 这些状态下文件不在最终位置或压根不全，
# tr 接过去只会校验失败然后从头下载：
#   moving      qb 正在挪文件（改保存路径/完成后移动），路径是临时的
#   checkingUP/checkingDL/checkingResumeData  qb 自己还在校验，别插一脚
#   missingFiles  文件已被外部删掉，qb 的 progress 仍报 1.0
#   allocating  刚分配空间，内容还没落盘
# 状态名按 qb 的 WebAPI 取值，全小写比对以兼容不同版本的大小写差异
UNSAFE_STATES = frozenset({
    "moving",
    "checkingup",
    "checkingdl",
    "checkingresumedata",
    "missingfiles",
    "allocating",
})


def _is_transferable(detail: dict) -> tuple[bool, str]:
    """这个种子现在能不能转移。返回 (可以, 不可以的原因)。

    只转 100% 完成的：下了一半就交给 tr，tr 校验后会把缺的部分自己补下，
    等于把下载任务也一起搬过去了 —— 那不是转移做种的本意，而且两个下载器
    会同时往同一份文件里写。
    """
    progress = detail.get("progress") or 0
    if progress < 1.0:
        return False, f"进度 {progress:.1%}，未下载完成"

    state = (detail.get("state") or "").strip().lower()
    if state in UNSAFE_STATES:
        return False, f"状态 {detail.get('state')}，文件尚未就位"

    unsafe, why = _export_would_hang(detail)
    if unsafe:
        return False, why

    return True, ""


def _export_would_hang(detail: dict) -> tuple[bool, str]:
    """导出这个种子会不会把 qb 的 WebAPI 锁死。返回 (会, 原因)。

    实测 qb 5.2.3：某些 BitTorrent v2 种子一导出就无限挂起 —— 不是慢，是
    不返回。期间连 /app/version（只回六个字节）都超时，而 WebUI 首页仍是
    302 正常。进程活着，锁被占住，整个 WebAPI 陪着一起冻住。

    判据是「qb 汇报的 hash 其实是 v2 infohash 的前 40 位」：

        列表 hash   = 732b39f8c4ed974b3712eaf7dea7ed79206af7a9
        infohash_v1 = 06b913089397c11fe27f09c0e709e1736cfd367c   ← 不是它
        infohash_v2 = 732b39f8...206af7a9252eda8b05fc049ee748202e
                      └─ 前 40 位正好是列表 hash

    /torrents/export 只按 v1 索引找种子，拿这个截断的 v2 值去查必然落空；
    qb 没有返回 404，而是卡在那里不放锁。实测拿真正的 v1 hash 去导会得到
    404（0.06 秒），说明这个种子的 v1 结构压根没注册进去 —— 两条路都走不通，
    只能不导。

    对照实验确认这不是普遍问题：同一台 qb 上纯 v1 的种子导出 200、180KB、
    0.30 秒。所以只挡这一类，不要扩大到全部 v2 / hybrid 种子。

    字段取不到时返回 False：qb 4.4 以下没有 infohash_v1/v2，那些版本也
    没有 v2 支持 —— 宁可放行也不误挡正常种子。
    """
    v2 = (detail.get("infohash_v2") or "").strip().lower()
    if not v2 or not v2.strip("0"):
        return False, ""

    reported = (detail.get("hash") or "").strip().lower()
    v1 = (detail.get("infohash_v1") or "").strip().lower()

    # 汇报的 hash 就是 v1 时，导出走得通，放行
    if reported and reported == v1:
        return False, ""

    if reported and reported == v2[:len(reported)]:
        return True, (
            "BitTorrent v2 种子（qb 汇报的 hash 是 v2 截断值），"
            "qBittorrent 导出会挂起并锁死 WebAPI，已跳过"
        )

    return False, ""


# 导出失败过的种子在这段时间内不再进自动候选（秒）。
#
# 已知会稳定失败的是 v2-only 种子，那个由 _is_v2_only 事先挡掉。这个冷却
# 兜的是剩下那些事先认不出来的：导出超时后不记一笔，每轮扫描都会再撞一次，
# 日志刷满而结果不会变。
#
# 跟种子体积无关 —— 实测 180KB 的正常种子 0.3 秒就导完了，卡死的是锁，
# 不是计算量。
#
# 只挡自动扫描，手动指定 hash 转移照旧放行：用户明确要转、或换了
# 环境想再试一次，不该被这个缓存拦住。
EXPORT_FAIL_COOLDOWN = 6 * 3600

# {hash: 失败时刻}。只存内存 —— 进程重启后本该重新试一次，
# 说不定 qb 已经升级或种子已被删
_export_failed: dict[str, float] = {}
_export_lock = Lock()


def _note_export_failure(torrent_hash: str) -> None:
    with _export_lock:
        _export_failed[torrent_hash] = time.monotonic()


def _export_recently_failed(torrent_hash: str) -> bool:
    """这个种子最近导出失败过吗（在冷却期内）。"""
    with _export_lock:
        at = _export_failed.get(torrent_hash)
        if at is None:
            return False
        if time.monotonic() - at >= EXPORT_FAIL_COOLDOWN:
            # 冷却期已过，清掉记录让它重新试一次
            del _export_failed[torrent_hash]
            return False
        return True


def reset_export_failures() -> int:
    """清空导出失败记录，让它们立刻能重试。返回清掉的条数。"""
    with _export_lock:
        count = len(_export_failed)
        _export_failed.clear()
    return count


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


def _all_completed(qb) -> list[dict]:
    """qb 里全部已完成的任务，不受 QBITTORRENT_CATEGORY 限制。

    必须绕开那个分类过滤：它是 qb 服务端做的精确匹配，早期没设分类时下的、
    手动加进 qb 的、分类名大小写不同的种子会被直接挡在候选之外 —— 既不算
    成功也不算失败，静默消失，看起来就是「这些种子怎么一直不转移」。

    该转哪些由 SEED_TRANSFER_CATEGORIES / SEED_TRANSFER_TAGS 决定（白名单，
    留空=全转），这才是本该管这件事的开关。

    老下载器或测试替身的 monitor_torrent 可能没有 all_categories 形参，
    TypeError 时退回无参调用，至少还能按原分类转移。

    只留 100% 且状态就位的：completed 就是 progress >= 1.0，下了一半的进不来。
    正在挪文件/校验中的这里也一并挡掉（见 UNSAFE_STATES），用行里现成的
    state 判断，不额外打接口。transfer_hashes 还会按详情再核一次 ——
    扫描到真正转移之间隔着几十个种子的处理时间，状态可能已经变了。
    """
    try:
        rows = qb.monitor_torrent(all_categories=True)
    except TypeError:
        logger.debug("[转移做种] 下载器不支持 all_categories，退回默认分类范围")
        rows = qb.monitor_torrent()

    out = []
    for row in rows:
        if not row.get("completed"):
            continue
        state = (row.get("state") or "").strip().lower()
        if state in UNSAFE_STATES:
            logger.debug(
                f"[转移做种] {row.get('hash')} 状态 {row.get('state')}，文件未就位，暂不转移"
            )
            continue
        out.append(row)
    return out


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

        transferable, why = _is_transferable(detail)
        if not transferable:
            result.skipped.append(torrent_hash)
            logger.debug(f"[转移做种] {torrent_hash} 跳过：{why}")
            continue

        content = qb.export_torrent(torrent_hash)
        if not content:
            # 别一律报「需 4.5+」：qb 卡死时导出也会失败，那句会把人引到
            # 升级版本上去，其实等下一轮重试就行。客户端已经把原因分好类
            reason = getattr(qb, "last_export_error", "") or "导出种子失败"
            # 记一笔，别让自动扫描每轮都拿它去把 qb 打满
            _note_export_failure(torrent_hash)
            result.failed.append({"hash": torrent_hash, "reason": reason})
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
        rows = _all_completed(qb)
    except Exception as exc:
        logger.warning(f"[转移做种] 读取 qBittorrent 任务列表失败: {exc}")
        return 0

    categories = _split_setting(settings.seed_transfer_categories)
    tags = _split_setting(settings.seed_transfer_tags)
    limit = _batch_limit()

    candidates: list[str] = []
    for row in rows:
        torrent_hash = row.get("hash") or ""
        if not torrent_hash:
            continue

        # 导出失败过的先放一放。手动转移不走这里，仍可随时重试
        if _export_recently_failed(torrent_hash):
            logger.debug(f"[转移做种] {torrent_hash} 近期导出失败，冷却期内跳过")
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
        rows = _all_completed(qb)
    except Exception as exc:
        logger.warning(f"[转移做种] 读取 qBittorrent 任务列表失败: {exc}")
        return []

    out: list[dict] = []
    for row in rows:
        torrent_hash = row.get("hash") or ""
        if not torrent_hash:
            continue

        # 这里必须逐个查详情，哪怕没配分类/标签过滤：判断「导出会不会锁死
        # WebAPI」要看 infohash_v1/v2，而列表接口不返回这两项。列表里留着
        # 一个点下去就会卡死 qb 的种子，比多打几次查询糟得多
        detail = qb.get_torrent_detail(torrent_hash)
        if detail is None:
            continue
        if (categories or tags) and not _matches_filter(detail, categories, tags):
            continue

        transferable, why = _is_transferable(detail)
        if not transferable:
            logger.debug(f"[转移做种] {torrent_hash} 不列入候选：{why}")
            continue

        out.append({
            "hash": torrent_hash,
            "name": row.get("name", ""),
            "save_path": row.get("save_path", ""),
        })
        if len(out) >= limit:
            break

    return out
