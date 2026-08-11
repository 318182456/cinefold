"""qBittorrent 下载客户端。

针对 qBittorrent 4.6+ / 5.x 的 WebAPI 变化做了适配：
- 5.0 起 WebAPI 强制校验 Referer/Origin，未带会 403
- `add_torrent` 的 `save_path` 在部分版本需与 category 的保存路径一致，否则被忽略
- 登录失败时 qb 返回 200 + "Fails."，不是 HTTP 错误码，必须显式检查

鉴权有两条路：

1. Web API Key（qb 5.2.0+ / WebAPI 2.14.1+，推荐）
   `Authorization: Bearer qbt_xxx`，无状态，不换 Cookie，也就没有会话过期
   和反复登录的问题。注意 API Key 打不通 /auth/login 与 /auth/logout，
   所以配了 key 就必须跳过登录，不能"先登录再带 key"。
2. 用户名 + 密码换 SID Cookie（4.x / 5.0 / 5.1 以及未开 key 的场景）
"""
from __future__ import annotations

import re
from typing import Sequence

import qbittorrentapi
from loguru import logger

from app.core.config import get_settings
from app.utils import get_magnet_hash, get_protocol_and_domain, get_torrent_hash


class QBitTorrentClient:
    def __init__(
        self,
        url: str = "",
        username: str = "",
        password: str = "",
        download_path: str = "",
        category: str = "",
        verify_cert: bool | None = None,
        apikey: str = "",
    ):
        settings = get_settings()
        self.url = url or settings.qbittorrent_url
        self.username = username or settings.qbittorrent_username
        self.password = password or settings.qbittorrent_password
        self.apikey = (apikey or settings.qbittorrent_apikey or "").strip()
        self.download_path = download_path or settings.qbittorrent_download_path
        self.category = category or settings.qbittorrent_category
        # 自签名证书或证书链不完整的反代很常见，默认不校验
        self.verify_cert = (
            settings.qbittorrent_verify_cert if verify_cert is None else verify_cert
        )
        self.client: qbittorrentapi.Client | None = None

    # ------------------------------------------------------------------
    def login_qb(self) -> bool:
        """建立到 qBittorrent 的连接。成功返回 True。

        配了 API Key 走无状态鉴权，不发登录请求；否则用账号密码换 Cookie。
        """
        if not self.url:
            logger.warning("未配置 qBittorrent 地址")
            return False

        use_apikey = bool(self.apikey)
        try:
            host = get_protocol_and_domain(self.url) or self.url
            # Bearer 头挂在 session 上，随后每个请求都会带
            extra_headers = (
                {"Authorization": f"Bearer {self.apikey}"} if use_apikey else {}
            )
            self.client = qbittorrentapi.Client(
                host=host,
                username="" if use_apikey else self.username,
                password="" if use_apikey else self.password,
                # 反代常用自签证书，校验失败会被库包装成 LoginFailed，难以排查
                VERIFY_WEBUI_CERTIFICATE=self.verify_cert,
                REQUESTS_ARGS={"timeout": (10, 30)},
                EXTRA_HEADERS=extra_headers,
                # 版本不在库的已知列表时不要直接抛异常，新版 qb 常触发
                RAISE_NOTIMPLEMENTEDERROR_FOR_UNIMPLEMENTED_API_ENDPOINTS=False,
                DISABLE_LOGGING_DEBUG_OUTPUT=True,
            )

            if use_apikey:
                # API Key 不能打 /auth/login，直接拿版本号验证 key 是否有效
                version = self.client.app.version
                logger.info(f"qBittorrent 已连接（API Key），版本 {version}")
            else:
                self._auth_log_in()
                logger.info(f"qBittorrent 登录成功，版本 {self.client.app.version}")
            return True

        except qbittorrentapi.LoginFailed as exc:
            logger.error(
                f"qBittorrent 登录失败: {exc or '账号密码错误，或 HTTPS 证书校验未通过'}"
            )
        except (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error) as exc:
            if use_apikey:
                logger.error(
                    f"qBittorrent 拒绝了 API Key，请确认 key 未被 Regenerate/Delete，"
                    f"且 qb 版本不低于 5.2.0: {exc}"
                )
            else:
                logger.error(
                    f"qBittorrent 返回 403。若为 5.x 版本，请在 WebUI 设置中关闭"
                    f"「对跨站请求伪造的保护」或把本机 IP 加入白名单: {exc}"
                )
        except qbittorrentapi.APIConnectionError as exc:
            logger.error(f"qBittorrent 连接失败，请检查地址是否可达: {exc}")
        except Exception as exc:
            logger.exception(f"qBittorrent 登录异常: {exc}")

        self.client = None
        return False

    def _auth_log_in(self) -> None:
        """登录并写入会话 Cookie。

        qbittorrent-api 判定登录成功的条件是响应体等于 "Ok."，但
        qBittorrent 5.2 起改为返回 204 空响应体，导致库误判为登录失败。
        这里直接发登录请求，只要拿到 SID Cookie 就算成功。
        """
        session = self.client._session
        base = (get_protocol_and_domain(self.url) or self.url).rstrip("/")
        response = session.post(
            f"{base}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            headers={"Referer": base, "Origin": base},
            verify=self.verify_cert,
            timeout=(10, 30),
        )

        # 明确的失败：qb 会返回 200 + "Fails."，或 403 表示 IP 被封
        if response.status_code == 403:
            raise qbittorrentapi.Forbidden403Error(
                "IP 已被 qBittorrent 封禁，请在 WebUI 中解除或稍后重试"
            )
        text = (response.text or "").strip()
        if text == "Fails.":
            raise qbittorrentapi.LoginFailed("用户名或密码错误")

        if not self._has_session_cookie(response, session):
            raise qbittorrentapi.LoginFailed(
                f"未获取到会话 Cookie（HTTP {response.status_code}）"
            )
        # 会话 Cookie 已在 session 中，后续请求由库直接复用

    @staticmethod
    def _has_session_cookie(response, session) -> bool:
        """判断是否拿到会话 Cookie。

        Cookie 名随版本变化：4.x 用 SID，5.x 改为 QBT_SID_<端口>，
        因此按前缀匹配而不是写死名字。
        """
        if any(
            name == "SID" or name.startswith("QBT_SID")
            for name in session.cookies.keys()
        ):
            return True

        raw = response.headers.get("set-cookie", "") or ""
        return bool(re.search(r"\b(QBT_SID[^=]*|SID)=", raw))

    def _ensure_client(self) -> bool:
        if self.client is not None:
            return True
        return self.login_qb()

    # ------------------------------------------------------------------
    def add_torrent(
        self, content: bytes, code: str = "", save_path: str = ""
    ) -> str | None:
        """推送 .torrent 文件内容。返回 info hash，失败返回 None。"""
        if not self._ensure_client():
            return None

        torrent_hash = get_torrent_hash(content)
        try:
            self.client.torrents_add(
                torrent_files=content,
                save_path=save_path or self.download_path or None,
                category=self.category or None,
                tags=code or None,
                is_paused=False,
            )
            logger.info(f"[{code}] 已推送种子到 qBittorrent，hash={torrent_hash}")
            return torrent_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送种子失败: {exc}")
            return None

    def add_torrent_by_magnet(
        self, magnet: str, code: str = "", save_path: str = ""
    ) -> str | None:
        """推送磁力链接。返回 info hash，失败返回 None。"""
        if not self._ensure_client():
            return None

        torrent_hash = get_magnet_hash(magnet)
        if not torrent_hash:
            logger.error(f"[{code}] 磁链格式异常，无法解析 hash: {magnet[:60]}")
            return None

        try:
            self.client.torrents_add(
                urls=magnet,
                save_path=save_path or self.download_path or None,
                category=self.category or None,
                tags=code or None,
                is_paused=False,
            )
            logger.info(f"[{code}] 已推送磁链到 qBittorrent，hash={torrent_hash}")
            return torrent_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送磁链失败: {exc}")
            return None

    # ------------------------------------------------------------------
    def monitor_torrent(self, hashes: Sequence[str] | None = None) -> list[dict]:
        """查询任务状态。

        返回 [{hash, name, progress, state, save_path, completed}]
        """
        if not self._ensure_client():
            return []

        try:
            filter_hashes = list(hashes) if hashes else None
            torrents = self.client.torrents_info(
                torrent_hashes=filter_hashes,
                category=self.category or None,
            )
            return [
                {
                    "hash": t.hash,
                    "name": t.name,
                    "progress": round(t.progress, 4),
                    "state": t.state,
                    "save_path": t.content_path or t.save_path,
                    "completed": t.progress >= 1.0,
                }
                for t in torrents
            ]
        except Exception as exc:
            logger.error(f"查询 qBittorrent 任务状态失败: {exc}")
            return []

    def delete_torrent(
        self, hashes: Sequence[str], delete_files: bool = False
    ) -> list[str]:
        """删除任务。返回实际提交删除的 hash 列表。

        delete_files 默认关闭：转种场景下同一文件被多个种子共用，交给下载器
        删文件容易在某个种子的保存路径不一致时误删或漏删，文件统一由调用方删。
        """
        if not hashes:
            return []
        if not self._ensure_client():
            return []

        wanted = [h for h in hashes if h]
        try:
            # qb 对不存在的 hash 静默忽略，先查一次才能报准删了哪些
            existing = {
                t.hash.lower()
                for t in self.client.torrents_info(torrent_hashes=list(wanted))
            }
            hit = [h for h in wanted if h.lower() in existing]
            missing = [h for h in wanted if h.lower() not in existing]
            if missing:
                logger.info(
                    f"qBittorrent 中已无这些种子，跳过: {', '.join(missing)}"
                )
            if not hit:
                return []

            self.client.torrents_delete(
                torrent_hashes=hit, delete_files=delete_files
            )
            logger.info(
                f"已从 qBittorrent 删除 {len(hit)} 个种子"
                f"（delete_files={delete_files}）: {', '.join(hit)}"
            )
            return hit
        except Exception as exc:
            logger.error(f"删除 qBittorrent 种子失败: {exc}")
            return []

    def test_connection(self) -> tuple[bool, str]:
        """供配置页「测试连接」使用。"""
        mode = "API Key" if self.apikey else "账号密码"
        if not self.login_qb():
            hint = (
                "请检查地址与 API Key（需 qb 5.2.0+）"
                if self.apikey
                else "请检查地址、账号密码与 CSRF 设置"
            )
            return False, f"连接失败（{mode}），{hint}"
        try:
            return True, f"连接成功（{mode}），qBittorrent {self.client.app.version}"
        except Exception as exc:
            return False, str(exc)
