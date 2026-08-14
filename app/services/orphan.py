"""孤儿关联：下载侧已经没了，媒体库侧还留着的那些片子。

要解决的问题：qb/tr 里删掉的东西，Emby 里往往还挂着。两侧各删各的，
中间没有任何人对账，久了就攒下一批「点进去播不了」的条目 —— 文件早没了，
刮削元数据还在，Emby 照样把它排在海报墙上。

为什么删除联动不管这种情况：那条链路是**反向**的（Emby 删片 → 删种 + 删源
文件），方向反过来就没人接。正向的对账（watchdir 第 2 步）确实会发现源文件
消失，但它只在两种前提下才动手删链接 —— 文件归某条监控规则管，且那条规则
开着反向删除。不满足的一律走 `result.skipped`，留在库里没人再看一眼：

- 刮削工具自己建的链接，从没走过 watchdir，不归任何规则管
- 关掉了反向删除的规则（这是默认值，很多人就没开过）
- 规则删掉了，但它建的 media_link 记录还在

所以这里只**报告**，不删任何东西。判定口径两条，分开标注不合并：

    源文件消失   media_link.source_path 在磁盘上没了 —— 纯本地 stat，最可靠
    种子没了     该关联对应的 hash 已不在下载器的种子列表里

两条是独立的：只删种不删文件，源文件还在（占着空间）；带文件删种，两条同时
成立；手工删了文件却留着种子，下载器会把它重新下回来。分开标注，用户才看得
出该做什么。合并成一个「已失效」反而丢掉了要动手的那部分信息。

「Emby 中仍存在」不去问 Emby，看的是硬链接文件还在不在。媒体库里那个文件
就是 Emby 扫到并提供播放的东西，它在 = Emby 里有这个条目。走 Emby API 反而
更不准：搜番号会命中相近编号，路径查询要遍历整库，而且 Emby 没配或没扫描时
整个功能就废了。文件系统才是这里的权威。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.database.models import History, MediaLink
from app.database.session import session_scope


@dataclass
class TorrentView:
    """下载器当前持有的种子快照。

    `answered` 分开记是必须的：下载器全都连不上时 monitor_torrent 返回空
    列表，与「下载器里一个种子都没有」在返回值上完全一样。把前者当后者用，
    会把全库的关联一次性标成「种子已删除」—— 正是这个功能最不该出的错。
    """
    hashes: set[str] = field(default_factory=set)
    # 种子内文件路径。用于判断「源文件仍被某个种子持有」
    files: set[str] = field(default_factory=set)
    # 至少一个下载器答上了话
    answered: bool = False


def _torrent_view() -> TorrentView:
    """拉一次下载器全量种子快照，整轮共用。

    逐条关联去问下载器是不行的：find_torrents_by_path 的成本与种子总数成
    正比而与查询路径数无关（它要把每个种子的文件清单拉一遍才能建索引），
    几百条关联各查一次就是几百次全量拉取。整轮拉一次，之后全在内存里比对。
    """
    from app.modules.downloadclient import get_download_client, list_configured_clients

    view = TorrentView()
    for name in list_configured_clients():
        client = get_download_client(name)
        if client is None:
            continue
        monitor = getattr(client, "monitor_torrent", None)
        if monitor is None:
            continue
        try:
            torrents = monitor()
        except Exception as exc:
            logger.warning(f"{name} 读取种子列表异常: {exc}")
            continue

        # 能走到这里说明这个下载器答上了话。空列表此时是可信的「确实没种子」
        view.answered = True
        hashes = [t.get("hash", "") for t in torrents if t.get("hash")]
        # hash 大小写在不同下载器/不同接口间不统一，统一小写再比
        view.hashes.update(h.lower() for h in hashes)

        lister = getattr(client, "list_torrent_files", None)
        if lister is None:
            continue
        try:
            view.files.update(p for p in lister(hashes) if p)
        except Exception as exc:
            # 文件清单拉不到不影响 hash 维度的判定，降级继续
            logger.warning(f"{name} 读取种子文件清单异常: {exc}")

    if view.answered:
        logger.info(
            f"下载器快照 —— 种子 {len(view.hashes)} 个，种子内文件 {len(view.files)} 个"
        )
    else:
        logger.warning("下载器均无响应，本轮不做「种子已删除」判定")
    return view


def _norm(path: str) -> str:
    """路径归一化，仅用于比对是否同一个文件。

    与 watchdir._norm_path 同一口径：登记时的写法与下载器返回的写法可能在
    正反斜杠、盘符大小写上不一致，直接比字符串会把还在的种子判成没了。
    """
    return str(Path(path)).replace("\\", "/").casefold()


def scan_orphans(refresh_gone_time: bool = True) -> list[dict]:
    """扫出「下载侧已删、媒体库侧仍在」的关联。

    refresh_gone_time 为真时顺手维护 source_gone_time：首次发现消失就记下
    时间，文件回来了就清空。页面查询与定时对账都会调到，时间戳因此不依赖
    watchdir 那条链路 —— 不归任何规则管的关联同样能拿到删除时间。

    只读磁盘与下载器，不删任何东西。
    """
    with session_scope() as session:
        rows = [
            {
                "link_path": r.link_path,
                "code": r.code,
                "source_path": r.source_path,
                "inode": r.inode,
                "create_time": r.create_time,
                "source_gone_time": r.source_gone_time,
            }
            for r in session.scalars(select(MediaLink)).all()
        ]

    if not rows:
        return []

    view = _torrent_view()
    torrent_files = {_norm(p) for p in view.files}

    # 番号 → 该番号登记过的种子 hash。History 是「cinefold 下载过什么」的账本，
    # 种子从下载器消失后这行仍在，正好用来对比
    codes = {r["code"] for r in rows}
    hashes_by_code: dict[str, list[str]] = {}
    if codes:
        with session_scope() as session:
            for start in range(0, len(codes), 500):  # SQLite 参数上限 999
                chunk = list(codes)[start:start + 500]
                for code, h in session.execute(
                    select(History.code, History.hash).where(History.code.in_(chunk))
                ).all():
                    hashes_by_code.setdefault(code, []).append(h)

    now = datetime.now()
    orphans: list[dict] = []
    gone_now: list[str] = []      # 本轮首次发现消失，要补时间戳
    recovered: list[str] = []     # 文件回来了，要清掉时间戳

    for row in rows:
        link_path = row["link_path"]
        source_path = row["source_path"]

        # 媒体库侧必须还在 —— 这正是「Emby 里还看得到」的含义。
        # 两侧都没了是普通的失效记录，已有 prune 管，不属于这个一览
        try:
            link_alive = Path(link_path).exists()
        except OSError:
            # 探测不了（挂载掉了、权限问题）就当它还在：漏报好过误报，
            # 这个列表是给人看着去动手删的
            link_alive = True
        if not link_alive:
            continue

        try:
            source_gone = not Path(source_path).exists()
        except OSError:
            source_gone = False

        # 直通模式下 link_path 就是 source_path，同一个文件不可能一个在一个没。
        # 这类记录永远不会是孤儿，跳过免得自相矛盾
        if _norm(link_path) == _norm(source_path):
            if row["source_gone_time"]:
                recovered.append(link_path)
            continue

        # 种子维度：登记过 hash，且这些 hash 一个都不在下载器里。
        #
        # 下载器没答话时整个维度不判（torrent_gone 一律为假）—— 见 TorrentView。
        # 从没登记过 hash 的关联也不判：手工拷进来的文件本就没有种子，
        # 把它算成「种子被删了」是无中生有
        known = [h.lower() for h in hashes_by_code.get(row["code"], []) if h]
        if not view.answered or not known:
            torrent_gone = False
        else:
            torrent_gone = not any(h in view.hashes for h in known)
            # hash 对不上，但源文件仍在某个种子的清单里 —— 说明这文件其实
            # 还被下载器持有着（转种后 History 里的旧 hash 已失效）。
            # 这种不算种子没了，否则转过种的片子会全体误报
            if torrent_gone and _norm(source_path) in torrent_files:
                torrent_gone = False

        if not source_gone and not torrent_gone:
            if row["source_gone_time"]:
                recovered.append(link_path)
            continue

        # 删除时间：库里已有就沿用（首次发现的那一刻），没有则以本轮为准。
        # 只有源文件真的消失了才记时间 —— 只删种不删文件时源文件还在，
        # 那不是「文件被删除」，没有删除时刻可言
        gone_time = row["source_gone_time"]
        if source_gone and gone_time is None:
            gone_time = now
            gone_now.append(link_path)
        elif not source_gone and gone_time is not None:
            recovered.append(link_path)
            gone_time = None

        orphans.append({
            "link_path": link_path,
            "code": row["code"],
            "source_path": source_path,
            "inode": row["inode"],
            "source_gone": source_gone,
            "torrent_gone": torrent_gone,
            "torrent_hashes": known,
            "create_time": row["create_time"].isoformat() if row["create_time"] else "",
            # 源文件仍在时为空 —— 没被删过，就没有删除时间
            "delete_time": gone_time.isoformat() if gone_time else "",
        })

    if refresh_gone_time and (gone_now or recovered):
        _persist_gone_time(gone_now, recovered, now)

    if orphans:
        logger.info(
            f"孤儿关联 {len(orphans)} 条 —— "
            f"源文件已删 {sum(1 for o in orphans if o['source_gone'])}，"
            f"种子已删 {sum(1 for o in orphans if o['torrent_gone'])}"
        )
    return orphans


def _persist_gone_time(
    gone: list[str], recovered: list[str], now: datetime
) -> None:
    """落盘 source_gone_time。首次消失记时间，恢复了清空。

    分片是必须的而非优化：SQLite 的 IN 参数上限是 999，一次大扫描很容易超。
    """
    with session_scope() as session:
        for start in range(0, len(gone), 500):
            for row in session.scalars(
                select(MediaLink).where(
                    MediaLink.link_path.in_(gone[start:start + 500])
                )
            ).all():
                # 并发的另一轮扫描可能已经写过，先到者的时间才是「首次」
                if row.source_gone_time is None:
                    row.source_gone_time = now

        for start in range(0, len(recovered), 500):
            for row in session.scalars(
                select(MediaLink).where(
                    MediaLink.link_path.in_(recovered[start:start + 500])
                )
            ).all():
                row.source_gone_time = None

    if gone:
        logger.info(f"新发现 {len(gone)} 个源文件消失，已记录删除时间")
    if recovered:
        logger.info(f"{len(recovered)} 个源文件已恢复，清除删除时间")
