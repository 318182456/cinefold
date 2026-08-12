"""目录实时监听。

watchdog 捕获源目录与媒体库的文件增删，触发对应规则的同步。

事件不直接驱动状态变更，只作为「该对账了」的信号：收到事件后把规则 id 丢进
待处理集合，由后台线程延迟合并执行一次 sync_rule()。这样做的原因：

1. 事件不可靠。Docker 绑定挂载、NFS/SMB、网络存储上 inotify 事件会丢，
   容器重启期间的变化更是没人看见。只有全量对账能收敛到正确状态。
2. 事件太密。拷一个文件进目录会产生 created + 多次 modified + closed，
   一个个处理等于反复扫同一个目录。
3. 顺序不保证。移动文件在不同平台上表现为 moved 或 deleted+created，
   逐事件判断「这是新增还是删除」很容易出错，对账不用管顺序。

所以这里只做一件事：把事件翻译成「哪条规则脏了」，剩下交给 sync_rule。
定时任务里的全量对账仍然保留，作为事件丢失时的兜底。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import WatchDir
from app.database.session import session_scope

# 收到事件后等这么久再同步，把连续事件合并成一次。
# 也给文件写入留出时间 —— sync_rule 里还有一道稳定性检查
DEBOUNCE_SECONDS = 5.0

_observer = None
_lock = threading.Lock()
# 待同步的规则 id → 定时器。同一规则重复触发时重置计时，实现防抖
_pending: dict[int, threading.Timer] = {}
# 后台建监听的线程。is_watching 要用它区分「正在建」和「没开」
_starting: threading.Thread | None = None
# 建监听期间被 stop_watching 打断的标志。schedule 那几分钟里规则可能又变了，
# 建完直接扔掉，否则它会把刚 stop 掉的 observer 又装回去
_cancelled = threading.Event()


def _sync_later(rule_id: int) -> None:
    """安排一次延迟同步。已有待执行的就重置计时。"""
    with _lock:
        timer = _pending.get(rule_id)
        if timer is not None:
            timer.cancel()

        def run() -> None:
            with _lock:
                _pending.pop(rule_id, None)
            try:
                from app.services.watchdir import sync_rule
                sync_rule(rule_id)
            except Exception as exc:
                logger.exception(f"[监听] 规则 {rule_id} 同步异常: {exc}")

        timer = threading.Timer(DEBOUNCE_SECONDS, run)
        timer.daemon = True
        _pending[rule_id] = timer
        timer.start()


def _target_bases(rule_ids: list[int]) -> dict[int, Path]:
    """规则 id → 目标根目录，用于把媒体库事件归属到具体规则。

    复用 watchdir.target_base()，保证归属判定与建链接时用的是同一套路径口径
    （target_dir / target_subdir / 直通模式的差异都在那里处理）。

    算不出目标目录的规则不放进表里 —— 它在 _owners 里就永远命中不了，
    等于被排除在实时监听之外，只能靠定时全量对账兜底。这是对的：连目标
    目录都解析不出来，也没法判断哪些路径归它管。
    """
    from app.services.watchdir import target_base

    bases: dict[int, Path] = {}
    with session_scope() as session:
        for rule in session.scalars(
            select(WatchDir).where(WatchDir.id.in_(rule_ids))
        ).all():
            base = target_base(rule)
            if base is None:
                logger.warning(
                    f"[监听] 规则 {rule.name or rule.id} 算不出目标目录，"
                    f"媒体库事件将不会触发它，仅靠定时对账"
                )
                continue
            try:
                bases[rule.id] = base.resolve()
            except OSError:
                bases[rule.id] = base
    return bases


def _build_handler():
    """构造事件处理器。watchdog 未安装时返回 None。"""
    try:
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return None

    class _Handler(FileSystemEventHandler):
        """把事件归到规则上。rule_ids 是这个 handler 负责的规则。

        bases 给出「规则 id → 目标根目录」时，媒体库事件按路径前缀归属，
        只标脏真正管这个路径的规则；不给则退回全部标脏。
        """

        def __init__(
            self,
            rule_ids: list[int],
            label: str,
            bases: dict[int, Path] | None = None,
        ):
            self.rule_ids = rule_ids
            self.label = label
            self.bases = bases

        def _owners(self, path: str) -> list[int]:
            """哪些规则管这个路径。判不出来时退回全部，宁可多扫不可漏扫。"""
            if not self.bases:
                return self.rule_ids
            try:
                # bases 是 resolve 过的，这边不跟着解析的话，媒体库根目录只要
                # 是个软链（NAS 上很常见），两边路径就永远对不上、全都判成不归属。
                # 删除事件的路径此刻已经不存在，resolve 不能用 strict
                target = Path(path).resolve()
            except (OSError, ValueError):
                return self.rule_ids

            # 目标根目录之间可能嵌套（A=/lib、B=/lib/FC2），命中的全都要标脏：
            # 路径落在 B 里的同时也落在 A 里，只取最深的那个会漏掉 A
            hit = [
                rule_id for rule_id in self.rule_ids
                if (base := self.bases.get(rule_id)) is not None
                and (target == base or base in target.parents)
            ]
            # 一个都没命中说明这路径不归任何规则管（媒体库里的其他内容），
            # 直接丢掉 —— 这正是按前缀归属要省掉的那部分无谓扫描
            return hit

        def _touch(self, path: str, kind: str) -> None:
            # 目录事件也要处理：整个子目录被删时，里面的文件不一定各来一条事件
            owners = self._owners(path)
            if not owners:
                logger.debug(f"[监听] {self.label} {kind} 不属于任何规则，忽略: {path}")
                return
            logger.debug(f"[监听] {self.label} {kind}: {path}")
            for rule_id in owners:
                _sync_later(rule_id)

        def on_created(self, event):
            self._touch(event.src_path, "新增")

        def on_deleted(self, event):
            self._touch(event.src_path, "删除")

        def on_moved(self, event):
            # 移动 = 旧路径删 + 新路径增，两端都可能跨规则，全部标脏
            self._touch(getattr(event, "dest_path", event.src_path), "移动")

    return _Handler


def start_watching(background: bool = True) -> bool:
    """启动监听。

    inotify 不支持递归，watchdog 得把目录树整棵走一遍、给每个子目录单独
    注册 watch。媒体库大 + 挂在网络存储上时这一步能到几分钟，挡在启动
    路径上就是几分钟打不开页面。

    监听只是实时性优化，定时全量对账才是正确性保证（见模块头注释），
    所以默认丢后台线程去建，启动流程不等它。background=False 时同步执行，
    给需要拿到结果的调用方用（重启监听、测试）。

    返回值：background=True 时表示「是否已派出线程」，不代表监听建成。
    """
    global _starting

    if _observer is not None:
        return True

    # 这是一次新的建立请求，把上一轮 stop 留下的取消标志清掉
    _cancelled.clear()

    if not background:
        return _start_watching_sync()

    with _lock:
        # 已经有线程在建了就不重复派 —— 重复 schedule 同一批目录很浪费
        if _starting is not None and _starting.is_alive():
            return True

        thread = threading.Thread(
            target=_start_watching_guarded,
            name="watcher-start",
            daemon=True,
        )
        _starting = thread

    thread.start()
    logger.info("[监听] 正在后台建立，大目录或网络存储上可能需要几分钟")
    return True


def _start_watching_guarded() -> None:
    """后台线程入口。线程里抛出去的异常没人接，日志里也看不见，所以兜住。"""
    try:
        _start_watching_sync()
    except Exception as exc:
        logger.exception(f"[监听] 后台建立失败: {exc}")


def _start_watching_sync() -> bool:
    """真正建立监听。返回是否真的启动了。

    监听两侧：
      源目录   —— 捕获新增/删除，驱动正向同步
      媒体库   —— 捕获删除，驱动反向删除（删种 + 删源文件）

    watchdog 未安装、没有启用的规则、或媒体库未配置时静默跳过 ——
    定时全量对账仍然工作，功能只是失去实时性，不该因此启动失败。
    """
    global _observer

    if _observer is not None:
        return True

    started = time.perf_counter()

    # 自动同步总开关关掉时，实时监听也不起 —— 否则文件一动就同步了，
    # 「关掉自动同步」这个意图落不到实处
    if not get_settings().watchdir_auto_sync:
        logger.info("[监听] 自动同步已关闭（WATCHDIR_AUTO_SYNC=false），不启动监听")
        return False

    handler_cls = _build_handler()
    if handler_cls is None:
        logger.warning(
            "未安装 watchdog，目录实时监听不可用，仅依赖定时全量对账。"
            "如需实时同步请安装: pip install watchdog"
        )
        return False

    with session_scope() as session:
        rules = [
            {"id": r.id, "source_dir": r.source_dir, "name": r.name,
             "recursive": r.recursive, "reverse_delete": r.reverse_delete,
             "target_subdir": r.target_subdir}
            for r in session.scalars(
                select(WatchDir).where(WatchDir.enabled.is_(True))
            ).all()
        ]

    if not rules:
        logger.debug("[监听] 没有启用的监控目录，不启动监听")
        return False

    from watchdog.observers import Observer

    observer = Observer()
    watched = 0

    # --- 源目录 ---
    for rule in rules:
        source = Path(rule["source_dir"])
        if not source.is_dir():
            logger.warning(f"[监听] 源目录不存在，跳过: {source}")
            continue
        try:
            observer.schedule(
                handler_cls([rule["id"]], f"源[{rule['name'] or source.name}]"),
                str(source),
                recursive=rule["recursive"],
            )
            watched += 1
        except Exception as exc:
            logger.error(f"[监听] 无法监听源目录 {source}: {exc}")

    # --- 媒体库 ---
    # 只有存在开启了反向删除的规则时才监听，否则媒体库侧事件无人消费。
    # 整个库挂一个 handler，把全部反向规则标脏 —— 由 sync_rule 按路径归属
    # 判断该处理哪些，比在这里按子目录拆分更不容易错
    reverse_rules = [r["id"] for r in rules if r["reverse_delete"]]
    if reverse_rules:
        library = get_settings().medialink_library_path
        if library and Path(library).is_dir():
            try:
                observer.schedule(
                    handler_cls(reverse_rules, "媒体库", _target_bases(reverse_rules)),
                    library,
                    recursive=True,
                )
                watched += 1
            except Exception as exc:
                logger.error(f"[监听] 无法监听媒体库 {library}: {exc}")
        else:
            logger.warning("[监听] 已有规则开启反向删除，但媒体库根目录未配置或不存在")

    if not watched:
        logger.warning("[监听] 没有可监听的目录")
        return False

    # 目录树走完可能已经过去几分钟，这期间规则若被改过，stop_watching 会
    # 置上取消标志。此时这个 observer 已经过期，装上去只会盖掉新的那份
    if _cancelled.is_set():
        logger.info("[监听] 建立过程中被取消，丢弃本次结果")
        return False

    observer.daemon = True
    observer.start()
    _observer = observer
    logger.info(f"[监听] 已启动，监听 {watched} 个目录，耗时 {time.perf_counter() - started:.1f}s")
    return True


def is_watching() -> bool:
    """监听是否在运行。页面据此提示「实时监听未启用，仅定时对账」。"""
    return _observer is not None and _observer.is_alive()


def is_starting() -> bool:
    """监听是否正在后台建立。

    大目录上这个过程要几分钟，其间 is_watching 还是假。页面得把它和
    「没开」区分开，否则看着像功能坏了。
    """
    return _observer is None and _starting is not None and _starting.is_alive()


def stop_watching() -> None:
    """停止监听，并取消所有待执行的同步。"""
    global _observer

    # 先置标志再停 —— 后台线程可能正卡在 schedule 里，让它建完自己丢弃
    _cancelled.set()

    with _lock:
        for timer in _pending.values():
            timer.cancel()
        _pending.clear()

    if _observer is not None:
        try:
            _observer.stop()
            # join 给个超时，网络存储上 stop 可能卡住，不该拖住整个进程退出
            _observer.join(timeout=5)
        except Exception as exc:
            logger.warning(f"[监听] 停止异常: {exc}")
        _observer = None
        logger.info("[监听] 已停止")


def restart_watching() -> bool:
    """规则变更后重建监听。

    watchdog 的监听目录在 schedule 时固定，增删规则必须重建 observer。
    """
    stop_watching()
    return start_watching()
