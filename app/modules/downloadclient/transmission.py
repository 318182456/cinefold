"""Transmission 下载客户端。

接口与 QBitTorrentClient 保持一致，便于上层按配置切换。
"""
from __future__ import annotations

import base64
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
            added = self.client.add_torrent(
                base64.b64encode(content).decode(),
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

    # ------------------------------------------------------------------
    def monitor_torrent(self, hashes: Sequence[str] | None = None) -> list[dict]:
        if not self._ensure_client():
            return []

        try:
            torrents = self.client.get_torrents(ids=list(hashes) if hashes else None)
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

    def test_connection(self) -> tuple[bool, str]:
        if not self.login_transmission():
            return False, "连接失败，请检查地址与账号密码"
        try:
            return True, f"连接成功，Transmission {self.client.get_session().version}"
        except Exception as exc:
            return False, str(exc)
