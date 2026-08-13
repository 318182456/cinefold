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

qb 偶发卡死（WebAPI 不响应但进程还在）时，所有请求都会 read timeout。
这里把连接结果报给 qbwatchdog，由它按「连续失败 N 次」判断要不要重启
qb 的容器 —— 见 app/services/qbwatchdog.py。
"""
from __future__ import annotations

import re
from pathlib import PurePath
from typing import Sequence

import qbittorrentapi
from loguru import logger

from app.core.config import get_settings
from app.utils import get_magnet_hash, get_protocol_and_domain, get_torrent_hash


def _report_ok() -> None:
    """qb 有正常响应，清零自愈计数。自愈模块出问题不能影响下载流程。"""
    try:
        from app.services import qbwatchdog
        qbwatchdog.report_success()
    except Exception:
        pass


def _report_error(exc: BaseException, context: str = "") -> None:
    """把异常交给自愈模块判断。只有连接类故障会被计入，业务错误会被忽略。"""
    try:
        from app.services import qbwatchdog
        qbwatchdog.report_failure(exc, context)
    except Exception:
        pass


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
            _report_ok()
            return True

        except qbittorrentapi.LoginFailed as exc:
            logger.error(
                f"qBittorrent 登录失败: {exc or '账号密码错误，或 HTTPS 证书校验未通过'}"
            )
            # 证书校验失败也会被库包装成 LoginFailed，但那不是「连不上」，
            # 交给 watchdog 自己判断
            _report_error(exc, "登录")
        except (qbittorrentapi.Forbidden403Error, qbittorrentapi.Unauthorized401Error) as exc:
            # 能返回 403/401 说明 qb 是活的，重启它解决不了鉴权配置问题
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
            _report_error(exc, "登录")
        except Exception as exc:
            logger.exception(f"qBittorrent 登录异常: {exc}")
            _report_error(exc, "登录")

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

    def _on_error(self, exc: BaseException, context: str) -> None:
        """操作失败时的统一处理。

        连接类故障要丢掉 client：qb 卡死或重启后，旧 session 里的 Cookie
        可能已失效，留着它会让 `_ensure_client` 一直复用坏连接，永远不重新
        登录。丢掉之后下一次调用会重新走 login_qb，qb 恢复了就能自动接上。
        """
        _report_error(exc, context)
        try:
            from app.services.qbwatchdog import is_connection_error
            if is_connection_error(exc):
                self.client = None
        except Exception:
            pass

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
            _report_ok()
            return torrent_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送种子失败: {exc}")
            self._on_error(exc, "推送种子")
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
            _report_ok()
            return torrent_hash
        except Exception as exc:
            logger.error(f"[{code}] 推送磁链失败: {exc}")
            self._on_error(exc, "推送磁链")
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
            _report_ok()
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
            self._on_error(exc, "查询任务状态")
            return []

    def list_torrent_files(self, hashes: Sequence[str]) -> list[str]:
        """列出这些种子包含的全部文件，返回绝对路径。

        用于联动删除：种子的文件清单是下载器给的权威信息，比按路径猜测
        「同目录下哪些文件属于这部片子」可靠得多。查不到就返回空，
        调用方据此退回到路径策略。
        """
        if not hashes:
            return []
        if not self._ensure_client():
            return []

        paths: list[str] = []
        for h in [x for x in hashes if x]:
            try:
                # save_path 是种子的根目录，files 里的 name 是相对它的路径
                info = self.client.torrents_info(torrent_hashes=[h])
                if not info:
                    continue
                root = info[0].save_path
                for f in self.client.torrents_files(torrent_hash=h):
                    paths.append(str(PurePath(root) / f.name))
                _report_ok()
            except Exception as exc:
                logger.warning(f"读取 qBittorrent 种子 {h} 的文件清单失败: {exc}")
                self._on_error(exc, "读取文件清单")
        return paths

    def export_torrent(self, torrent_hash: str) -> bytes | None:
        """导出种子的 .torrent 文件内容，失败返回 None。

        转移做种要把种子原样交给另一个下载器，磁链不够 —— 私有站的种子
        没有 DHT，靠磁链拿不到 metadata，必须是带 tracker 的完整 .torrent。

        qb 4.5+ 才有 /torrents/export；旧版返回 404，此时只能放弃转移。
        """
        if not torrent_hash or not self._ensure_client():
            return None

        try:
            content = self.client.torrents_export(torrent_hash=torrent_hash)
            _report_ok()
        except Exception as exc:
            logger.warning(f"导出 qBittorrent 种子 {torrent_hash} 失败: {exc}")
            self._on_error(exc, "导出种子")
            return None

        if not content:
            logger.warning(f"qBittorrent 导出的种子 {torrent_hash} 内容为空")
            return None
        return bytes(content)

    def get_torrent_detail(self, torrent_hash: str) -> dict | None:
        """单个种子的详情，转移做种要用到保存路径与内容路径。

        返回 {hash, name, save_path, content_path, category, tags, progress}，
        查不到返回 None。
        """
        if not torrent_hash or not self._ensure_client():
            return None

        try:
            rows = self.client.torrents_info(torrent_hashes=[torrent_hash])
            _report_ok()
        except Exception as exc:
            logger.warning(f"读取 qBittorrent 种子 {torrent_hash} 详情失败: {exc}")
            self._on_error(exc, "读取种子详情")
            return None

        if not rows:
            return None
        t = rows[0]
        return {
            "hash": t.hash,
            "name": t.name,
            # save_path 是种子内容的父目录，转移时 tr 的 download_dir 要对齐它，
            # 否则 tr 会重新下载而不是直接校验已有文件
            "save_path": t.save_path,
            "content_path": t.content_path or t.save_path,
            "category": getattr(t, "category", "") or "",
            "tags": getattr(t, "tags", "") or "",
            "progress": round(t.progress, 4),
            "state": t.state,
        }

    def find_torrents_by_path(self, paths: Sequence[str]) -> dict[str, list[str]]:
        """按文件路径反查种子 hash。返回 {路径: [hash, ...]}。

        做法是把 qb 里所有种子的文件清单拉一遍，建「绝对路径 → hash」索引，
        再拿待查路径去命中。一次全量拉取换 N 次精确匹配 —— qb 没有
        「按路径查种子」的 API，只能这样。

        路径比对前统一成 PurePath 再转字符串：qb 在 Windows 上返回的分隔符
        可能与传入路径不一致，直接比字符串会漏。大小写不做归一化 ——
        Linux 上路径大小写敏感，抹平会导致误匹配到别的文件。
        """
        if not paths:
            return {}
        if not self._ensure_client():
            return {}

        # 待查路径归一化，同时保留原始形式用于回填结果
        wanted: dict[str, list[str]] = {}
        for raw in paths:
            if raw:
                wanted.setdefault(str(PurePath(raw)), []).append(raw)
        if not wanted:
            return {}

        out: dict[str, list[str]] = {}
        try:
            torrents = self.client.torrents_info()
            _report_ok()
        except Exception as exc:
            logger.warning(f"读取 qBittorrent 种子列表失败: {exc}")
            self._on_error(exc, "读取种子列表")
            return {}

        for t in torrents:
            try:
                root = t.save_path
                files = self.client.torrents_files(torrent_hash=t.hash)
            except Exception as exc:
                # 单个种子读不到不影响其余种子
                logger.debug(f"读取 qBittorrent 种子 {t.hash} 文件清单失败: {exc}")
                # 循环中途 qb 挂了的话，后面每一轮都会失败。这里照样上报，
                # 否则整个方法只算作一次失败，攒不够阈值
                self._on_error(exc, "读取文件清单")
                continue

            for f in files:
                key = str(PurePath(root) / f.name)
                if key not in wanted:
                    continue
                for original in wanted[key]:
                    bucket = out.setdefault(original, [])
                    if t.hash not in bucket:
                        bucket.append(t.hash)

        if out:
            # 同 Transmission 版：辅种会让一个文件对应多个种子，去重后才是种子数
            pairs = sum(len(v) for v in out.values())
            uniq = len({h for hashes in out.values() for h in hashes})
            logger.info(
                f"qBittorrent 按路径反查：{len(out)} 个文件命中 {uniq} 个种子"
                f"（共 {pairs} 条对应关系）"
            )
        return out

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
            _report_ok()
            return hit
        except Exception as exc:
            logger.error(f"删除 qBittorrent 种子失败: {exc}")
            self._on_error(exc, "删除种子")
            return []

    def control_torrent(self, action: str, hashes: Sequence[str]) -> list[str]:
        """暂停/恢复/重新检查/强制汇报。返回实际操作到的 hash。

        qb 5.x 把 pause/resume 改名成 stop/start，旧名在新版被移除。
        库版本与服务端版本的组合太多，逐个探测方法名比判版本可靠。
        """
        if not hashes or not self._ensure_client():
            return []

        # 每个动作按优先级列出候选方法名，取第一个存在的
        candidates = {
            "pause": ("torrents_stop", "torrents_pause"),
            "resume": ("torrents_start", "torrents_resume"),
            "recheck": ("torrents_recheck",),
            "reannounce": ("torrents_reannounce",),
        }.get(action)
        if candidates is None:
            logger.warning(f"不支持的 qBittorrent 操作 {action}")
            return []

        wanted = [h for h in hashes if h]
        try:
            existing = {
                t.hash.lower()
                for t in self.client.torrents_info(torrent_hashes=list(wanted))
            }
            hit = [h for h in wanted if h.lower() in existing]
            if not hit:
                logger.info(f"qBittorrent 中已无这些种子，跳过 {action}")
                return []

            method = next(
                (getattr(self.client, name) for name in candidates
                 if hasattr(self.client, name)),
                None,
            )
            if method is None:
                logger.error(f"当前 qbittorrent-api 不支持 {action}")
                return []

            method(torrent_hashes=hit)
            logger.info(f"qBittorrent {action} {len(hit)} 个种子: {', '.join(hit)}")
            _report_ok()
            return hit
        except Exception as exc:
            logger.error(f"qBittorrent {action} 失败: {exc}")
            self._on_error(exc, action)
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
