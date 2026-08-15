"""Transmission 下载客户端。

接口与 QBitTorrentClient 保持一致，便于上层按配置切换。
"""
from __future__ import annotations

from pathlib import PurePath
from typing import Sequence
from urllib.parse import urlparse

from loguru import logger

from app.core.config import get_settings
from app.utils import get_magnet_hash, get_torrent_hash


class TransmissionClient:
    def __init__(
        self,
        url: str = "",
        username: str = "",
        password: str = "",
        download_path: str = "",
        label: str = "",
        verify_cert: bool | None = None,
    ):
        settings = get_settings()
        self.url = url or settings.transmission_url
        self.username = username or settings.transmission_username
        self.password = password or settings.transmission_password
        self.download_path = download_path or settings.transmission_download_path
        self.label = label or settings.transmission_label
        self.verify_cert = (
            settings.transmission_verify_cert if verify_cert is None else verify_cert
        )
        self.client = None

    # ------------------------------------------------------------------
    def login_transmission(self) -> bool:
        if not self.url:
            logger.warning("未配置 Transmission 地址")
            return False

        try:
            import urllib3
            from transmission_rpc import Client

            parsed = urlparse(self.url)
            is_https = parsed.scheme == "https"

            if is_https and not self.verify_cert:
                # 反代常用自签证书，关闭校验并抑制随之而来的告警
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            self.client = Client(
                protocol="https" if is_https else "http",
                host=parsed.hostname or "localhost",
                port=parsed.port or (443 if is_https else 9091),
                # Transmission 的 RPC 路径默认是 /transmission/rpc
                path=parsed.path if parsed.path not in ("", "/") else "/transmission/rpc",
                username=self.username or None,
                password=self.password or None,
                timeout=30,
            )
            # transmission_rpc 不暴露 verify 参数，需直接改底层 session
            if is_https and not self.verify_cert:
                session = getattr(self.client, "_http_session", None)
                if session is not None:
                    session.verify = False
            version = self.client.get_session().version
            logger.info(f"Transmission 登录成功，版本 {version}")
            return True
        except Exception as exc:
            logger.error(f"Transmission 登录失败: {exc}")
            self.client = None
            return False

    def _ensure_client(self) -> bool:
        return True if self.client is not None else self.login_transmission()

    # ------------------------------------------------------------------
    def add_torrent(self, content: bytes, code: str = "", save_path: str = "") -> str | None:
        if not self._ensure_client():
            return None

        torrent_hash = get_torrent_hash(content)
        try:
            # 直接传 bytes，库内部会做 base64。传 base64 字符串会被当成本地
            # 文件名，tr 找不到那个文件，报 invalid or corrupt torrent file
            added = self.client.add_torrent(
                content,
                download_dir=save_path or self.download_path or None,
                labels=[self.label] if self.label else None,
            )
            new_hash = added.hashString or torrent_hash
            self._apply_labels(new_hash, [self.label] if self.label else None, code)
            logger.info(f"[{code}] 已推送种子到 Transmission，hash={added.hashString}")
            return new_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送种子失败: {exc}")
            return None

    def add_torrent_by_magnet(self, magnet: str, code: str = "", save_path: str = "") -> str | None:
        if not self._ensure_client():
            return None

        try:
            added = self.client.add_torrent(
                magnet,
                download_dir=save_path or self.download_path or None,
                labels=[self.label] if self.label else None,
            )
            new_hash = added.hashString or get_magnet_hash(magnet)
            self._apply_labels(new_hash, [self.label] if self.label else None, code)
            logger.info(f"[{code}] 已推送磁链到 Transmission，hash={added.hashString}")
            return new_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送磁链失败: {exc}")
            return None

    def add_torrent_for_seeding(
        self, content: bytes, save_path: str, code: str = "", labels: Sequence[str] | None = None
    ) -> str | None:
        """接管一份已下载完成的文件继续做种。返回 hash，失败返回 None。

        与 add_torrent 的区别有两处，都是为了不重下已有的文件：
        - download_dir 必须是源下载器的保存路径，tr 才能在那里找到文件
        - 先暂停加入再触发校验，跳过「加进来就开下」的窗口。tr 校验完发现
          文件齐全就直接转做种，缺文件才会补下

        校验这一步省不掉：Transmission 做种前必定核对本地文件，RPC 里没有
        「信任文件、跳过校验」的开关（torrent-add 无此参数，session 也只有
        start-added-torrents）。不显式调 verify_torrent 也没用，start 时
        tr 会自己校验一遍。

        已存在同 hash 的种子时不重复添加，直接返回既有 hash —— transmission
        对重复添加会抛 DuplicateTorrent，那不是错误，是「已经转移过了」。
        """
        if not content or not save_path:
            logger.warning(f"[{code}] 转移做种缺少种子内容或保存路径")
            return None
        if not self._ensure_client():
            return None

        torrent_hash = get_torrent_hash(content)
        label_list = list(labels) if labels else ([self.label] if self.label else None)

        try:
            added = self.client.add_torrent(
                content,
                download_dir=save_path,
                labels=label_list,
                paused=True,
            )
        except Exception as exc:
            # transmission-rpc 对重复种子抛的是普通异常，只能按文案判别
            if "duplicate" in str(exc).lower():
                logger.info(f"[{code}] Transmission 中已存在该种子，跳过: {torrent_hash}")
                return torrent_hash
            logger.error(f"[{code}] 转移做种失败: {exc}")
            return None

        new_hash = added.hashString or torrent_hash
        self._apply_labels(new_hash, label_list, code)
        try:
            # 校验后 tr 才知道文件已经在本地，否则会从 0 开始下
            self.client.verify_torrent(ids=[new_hash])
            self.client.start_torrent(ids=[new_hash])
        except Exception as exc:
            # 种子已经加进去了，这一步没成不算整体失败，用户手动处理即可
            logger.warning(f"[{code}] Transmission 启动失败，请手动检查 {new_hash}: {exc}")

        logger.info(
            f"[{code}] 已转移做种到 Transmission，hash={new_hash}，目录={save_path}"
        )
        return new_hash

    def _apply_labels(
        self, torrent_hash: str, labels: Sequence[str] | None, code: str = ""
    ) -> None:
        """加完之后再补一次标签。

        RPC 17 以下（Transmission 3.x）的 torrent-add 不认 labels，参数被
        静默丢弃，种子加进去是没有标签的；torrent-set 则一直支持。多发这
        一次请求，新旧版本都能打上标签。

        标签只是给人看的标记，设不上不影响做种，异常吞掉即可。
        """
        if not labels or not torrent_hash:
            return
        try:
            self.client.change_torrent(ids=[torrent_hash], labels=list(labels))
        except Exception as exc:
            logger.warning(f"[{code}] Transmission 设置标签失败 {torrent_hash}: {exc}")

    # ------------------------------------------------------------------
    def monitor_torrent(self, hashes: Sequence[str] | None = None) -> list[dict]:
        if not self._ensure_client():
            return []

        try:
            # 只取用得上的字段。默认会把每个种子的 files 数组也拉回来，
            # 种子上千时 RPC 要跑一百多秒，指定字段后是秒级
            torrents = self.client.get_torrents(
                ids=list(hashes) if hashes else None,
                arguments=[
                    "hashString", "name", "percentDone", "status", "downloadDir",
                ],
            )
            return [
                {
                    "hash": t.hashString,
                    "name": t.name,
                    "progress": round((t.percent_done or 0), 4),
                    "state": str(t.status),
                    "save_path": t.download_dir,
                    "completed": (t.percent_done or 0) >= 1.0,
                }
                for t in torrents
            ]
        except Exception as exc:
            logger.error(f"查询 Transmission 任务状态失败: {exc}")
            return []

    def list_torrent_files(self, hashes: Sequence[str]) -> list[str]:
        """列出这些种子包含的全部文件，返回绝对路径。语义同 qBittorrent 版。"""
        if not hashes:
            return []
        if not self._ensure_client():
            return []

        wanted = [h for h in hashes if h]
        paths: list[str] = []
        try:
            for t in self.client.get_torrents(ids=wanted):
                # files() 里的 name 含种子根目录名，相对 download_dir
                root = t.download_dir
                for f in t.get_files():
                    paths.append(str(PurePath(root) / f.name))
        except Exception as exc:
            logger.warning(f"读取 Transmission 种子文件清单失败: {exc}")
        return paths

    def list_torrent_files_detailed(self, torrent_hash: str) -> list[dict]:
        """列出单个种子的文件，带绝对路径与字节数。语义同 qBittorrent 版。"""
        if not torrent_hash or not self._ensure_client():
            return []

        try:
            torrents = self.client.get_torrents(ids=[torrent_hash])
            if not torrents:
                return []
            root = torrents[0].download_dir
            return [
                {"path": str(PurePath(root) / f.name), "size": int(f.size or 0)}
                for f in torrents[0].get_files()
            ]
        except Exception as exc:
            logger.warning(f"读取 Transmission 种子 {torrent_hash} 的文件明细失败: {exc}")
            return []

    def find_torrents_by_path(
        self, paths: Sequence[str] | None
    ) -> dict[str, list[str]]:
        """按文件路径反查种子 hash。语义同 qBittorrent 版。

        Transmission 也没有按路径查种子的 RPC，同样是全量拉取建索引。
        只取 hashString 与文件清单两个字段，减少 RPC 负载。

        paths 传 None 同样表示不过滤、返回全部文件的映射。
        """
        want_all = paths is None
        if not want_all and not paths:
            return {}
        if not self._ensure_client():
            return {}

        wanted: dict[str, list[str]] = {}
        if not want_all:
            for raw in paths:
                if raw:
                    wanted.setdefault(str(PurePath(raw)), []).append(raw)
            if not wanted:
                return {}

        out: dict[str, list[str]] = {}
        try:
            # 显式列字段：不指定 arguments 会把 tracker、peer 等几十个字段一起
            # 拉回来，种子上千时白等一百多秒。
            # priorities 与 wanted 这里用不上，但 get_files() 内部要读它们，
            # 少一个就抛 KeyError，反查会静默返回空表
            torrents = self.client.get_torrents(
                arguments=[
                    "hashString", "downloadDir", "files", "priorities", "wanted",
                ],
            )
        except Exception as exc:
            logger.warning(f"读取 Transmission 种子列表失败: {exc}")
            return {}

        for t in torrents:
            try:
                root = t.download_dir
                files = t.get_files()
            except Exception as exc:
                logger.debug(f"读取 Transmission 种子 {t.hashString} 文件清单失败: {exc}")
                continue

            for f in files:
                key = str(PurePath(root) / f.name)
                # 全量模式下每个文件都要，键就用它自己的规范化路径
                for original in ([key] if want_all else wanted.get(key, [])):
                    bucket = out.setdefault(original, [])
                    if t.hashString not in bucket:
                        bucket.append(t.hashString)

        if out:
            # 文件与种子是多对多：一个文件可能被多个种子引用（辅种），一个种子
            # 也可能含多个文件（合集）。两者都会让对应关系数超过种子数，
            # 只报关系总数会被误读成种子数，所以两个都打出来
            pairs = sum(len(v) for v in out.values())
            uniq = len({h for hashes in out.values() for h in hashes})
            scope = "全量" if want_all else "按路径"
            logger.info(
                f"Transmission {scope}反查：{len(out)} 个文件命中 {uniq} 个种子"
                f"（共 {pairs} 条对应关系）"
            )
        return out

    def unwant_torrent_files(
        self, torrent_hash: str, paths: Sequence[str]
    ) -> tuple[int, int]:
        """把种子里指定的文件标记为「不需要」。语义同 qBittorrent 版。

        tr 用 files_unwanted 传文件序号，语义与 qb 的 priority=0 一致：
        该文件不再下载，种子继续为其余文件做种。

        返回 (标记成功数, 剩余仍需要的文件数)。
        """
        if not torrent_hash or not paths:
            return 0, 0
        if not self._ensure_client():
            return 0, 0

        targets = {str(PurePath(p)) for p in paths if p}
        if not targets:
            return 0, 0

        try:
            found = self.client.get_torrents(
                ids=[torrent_hash],
                arguments=["hashString", "downloadDir", "files", "priorities", "wanted"],
            )
            if not found:
                logger.info(f"Transmission 中已无种子 {torrent_hash}，无需标记文件")
                return 0, 0
            t = found[0]
            root = t.download_dir
            ids, remaining = [], 0
            for f in t.get_files():
                if str(PurePath(root) / f.name) in targets:
                    ids.append(f.id)
                elif f.selected:
                    # selected 即 tr 的 wanted。之前就没勾选的不算「仍需要」，
                    # 否则种子明明已经空了还会被判成有内容而留下空壳
                    remaining += 1
            if not ids:
                return 0, 0

            self.client.change_torrent(ids=[torrent_hash], files_unwanted=ids)
            logger.info(
                f"已在 Transmission 种子 {torrent_hash} 中把 {len(ids)} 个文件"
                f"标记为不需要，剩余 {remaining} 个文件仍在做种"
            )
            return len(ids), remaining
        except Exception as exc:
            logger.warning(f"标记 Transmission 文件为不需要失败 {torrent_hash}: {exc}")
            return 0, 0

    def delete_torrent(
        self, hashes: Sequence[str], delete_files: bool = False
    ) -> list[str]:
        """删除任务。返回实际提交删除的 hash 列表。语义同 qBittorrent 版。"""
        if not hashes:
            return []
        if not self._ensure_client():
            return []

        wanted = [h for h in hashes if h]
        try:
            # 传入不存在的 id，transmission-rpc 会直接抛异常，先过滤一遍
            existing = {
                t.hashString.lower() for t in self.client.get_torrents(ids=list(wanted))
            }
            hit = [h for h in wanted if h.lower() in existing]
            missing = [h for h in wanted if h.lower() not in existing]
            if missing:
                logger.info(f"Transmission 中已无这些种子，跳过: {', '.join(missing)}")
            if not hit:
                return []

            self.client.remove_torrent(ids=hit, delete_data=delete_files)
            logger.info(
                f"已从 Transmission 删除 {len(hit)} 个种子"
                f"（delete_files={delete_files}）: {', '.join(hit)}"
            )
            return hit
        except Exception as exc:
            logger.error(f"删除 Transmission 种子失败: {exc}")
            return []

    def control_torrent(self, action: str, hashes: Sequence[str]) -> list[str]:
        """暂停/恢复/重新检查/强制汇报。语义同 qBittorrent 版。"""
        if not hashes or not self._ensure_client():
            return []

        method_name = {
            "pause": "stop_torrent",
            "resume": "start_torrent",
            "recheck": "verify_torrent",
            "reannounce": "reannounce_torrent",
        }.get(action)
        if method_name is None:
            logger.warning(f"不支持的 Transmission 操作 {action}")
            return []

        wanted = [h for h in hashes if h]
        try:
            # 同 delete_torrent：不存在的 id 会让 transmission-rpc 抛异常
            existing = {
                t.hashString.lower() for t in self.client.get_torrents(ids=list(wanted))
            }
            hit = [h for h in wanted if h.lower() in existing]
            if not hit:
                logger.info(f"Transmission 中已无这些种子，跳过 {action}")
                return []

            getattr(self.client, method_name)(ids=hit)
            logger.info(f"Transmission {action} {len(hit)} 个种子: {', '.join(hit)}")
            return hit
        except Exception as exc:
            logger.error(f"Transmission {action} 失败: {exc}")
            return []

    def test_connection(self) -> tuple[bool, str]:
        if not self.login_transmission():
            return False, "连接失败，请检查地址与账号密码"
        try:
            return True, f"连接成功，Transmission {self.client.get_session().version}"
        except Exception as exc:
            return False, str(exc)
