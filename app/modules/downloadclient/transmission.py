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
            logger.info(f"[{code}] 已推送种子到 Transmission，hash={added.hashString}")
            return added.hashString or torrent_hash
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
            logger.info(f"[{code}] 已推送磁链到 Transmission，hash={added.hashString}")
            return added.hashString or get_magnet_hash(magnet)
        except Exception as exc:
            logger.error(f"[{code}] 推送磁链失败: {exc}")
            return None

    def add_torrent_for_seeding(
        self,
        content: bytes,
        save_path: str,
        code: str = "",
        labels: Sequence[str] | None = None,
        verify: bool = True,
    ) -> str | None:
        """接管一份已下载完成的文件继续做种。返回 hash，失败返回 None。

        与 add_torrent 的区别有两处，都是为了不重下已有的文件：
        - download_dir 必须是源下载器的保存路径，tr 才能在那里找到文件
        - 先暂停加入再触发校验，跳过「加进来就开下」的窗口。tr 校验完发现
          文件齐全就直接转做种，缺文件才会补下

        verify=False 则不触发校验，加完直接开。省掉整盘重读的 IO，代价是
        tr 未经核对就认定本地文件有效：文件真有缺损时会把坏块传给别人。
        源文件刚由 qb 校验过时这样最快，不确定时应保持开启。

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
        try:
            if verify:
                # 校验后 tr 才知道文件已经在本地，否则会从 0 开始下
                self.client.verify_torrent(ids=[new_hash])
            self.client.start_torrent(ids=[new_hash])
        except Exception as exc:
            # 种子已经加进去了，这一步没成不算整体失败，用户手动处理即可
            logger.warning(f"[{code}] Transmission 启动失败，请手动检查 {new_hash}: {exc}")

        logger.info(
            f"[{code}] 已转移做种到 Transmission，hash={new_hash}，目录={save_path}"
            f"{'' if verify else '（已跳过校验）'}"
        )
        return new_hash

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

    def find_torrents_by_path(self, paths: Sequence[str]) -> dict[str, list[str]]:
        """按文件路径反查种子 hash。语义同 qBittorrent 版。

        Transmission 也没有按路径查种子的 RPC，同样是全量拉取建索引。
        只取 hashString 与文件清单两个字段，减少 RPC 负载。
        """
        if not paths:
            return {}
        if not self._ensure_client():
            return {}

        wanted: dict[str, list[str]] = {}
        for raw in paths:
            if raw:
                wanted.setdefault(str(PurePath(raw)), []).append(raw)
        if not wanted:
            return {}

        out: dict[str, list[str]] = {}
        try:
            torrents = self.client.get_torrents()
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
                if key not in wanted:
                    continue
                for original in wanted[key]:
                    bucket = out.setdefault(original, [])
                    if t.hashString not in bucket:
                        bucket.append(t.hashString)

        if out:
            total = sum(len(v) for v in out.values())
            logger.info(
                f"Transmission 按路径反查到 {total} 个种子，覆盖 {len(out)} 个文件"
            )
        return out

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
