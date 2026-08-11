"""迅雷远程下载。

迅雷没有公开 API，这里对接的是迅雷 NAS/远程下载的 Web 接口，
凭证需要用户自行从浏览器抓取后填入 THUNDER_AUTHORIZATION。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Sequence

import httpx
from loguru import logger

from app.core.config import get_settings

PAN_API = "https://api-pan.xunlei.com/drive/v1"
CLIENT_ID = "Xqp0kJBXWhwaTpB6"


class Thunder:
    def __init__(self, url: str = "", file_id: str = "", authorization: str = ""):
        settings = get_settings()
        self.url = url or settings.thunder_url
        self.file_id = file_id or settings.thunder_file_id
        self.authorization = authorization or settings.thunder_authorization
        self.device_id = ""
        self.captcha_token = ""

    # ------------------------------------------------------------------
    def get_device_id(self) -> str:
        """设备 ID 由访问地址推导，保持稳定即可。"""
        if not self.device_id:
            seed = (self.url or "cinefold").encode()
            self.device_id = hashlib.md5(seed).hexdigest()
        return self.device_id

    def get_pan_auth(self) -> dict:
        """构造请求头。"""
        auth = self.authorization or ""
        if auth and not auth.lower().startswith("bearer "):
            auth = f"Bearer {auth}"
        return {
            "Authorization": auth,
            "x-client-id": CLIENT_ID,
            "x-device-id": self.get_device_id(),
            "x-captcha-token": self.captcha_token,
            "content-type": "application/json",
            "User-Agent": "Mozilla/5.0 cinefold",
        }

    @staticmethod
    def analyze_size(size: int | str) -> float:
        """字节数 → MB。"""
        try:
            return round(int(size) / 1024 / 1024, 2)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    def download(self, magnet: str, code: str = "", save_path: str = "") -> str | None:
        """提交离线下载任务。"""
        if not self.authorization:
            logger.warning("未配置迅雷授权参数")
            return None

        payload = {
            "kind": "drive#file",
            "name": "",
            "upload_type": "UPLOAD_TYPE_URL",
            "url": {"url": magnet},
            "params": {"target": self.file_id or ""},
            "parent_id": self.file_id or "",
        }

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.post(
                    f"{PAN_API}/files", headers=self.get_pan_auth(), json=payload
                )
                if response.status_code == 401:
                    logger.error("迅雷授权已过期，请重新抓取 THUNDER_AUTHORIZATION")
                    return None
                response.raise_for_status()
                data = response.json()

            task_id = (data.get("task") or {}).get("id") or data.get("file", {}).get("id")
            if task_id:
                logger.info(f"[{code}] 已提交迅雷离线任务 {task_id}")
                return str(task_id)

            logger.error(f"[{code}] 迅雷返回异常: {json.dumps(data, ensure_ascii=False)[:200]}")
            return None
        except Exception as exc:
            logger.error(f"[{code}] 提交迅雷任务失败: {exc}")
            return None

    # 与其他下载器保持一致的接口名
    def add_torrent_by_magnet(self, magnet: str, code: str = "", save_path: str = "") -> str | None:
        return self.download(magnet, code, save_path)

    def add_torrent(self, content: bytes, code: str = "", save_path: str = "") -> str | None:
        logger.warning("迅雷不支持直接上传种子文件，请改用磁力链接")
        return None

    def delete_torrent(
        self, hashes: Sequence[str], delete_files: bool = False
    ) -> list[str]:
        """迅雷是网盘离线下载，没有本地做种的概念，不参与联动删除。"""
        if hashes:
            logger.info("迅雷不支持删除种子，已跳过")
        return []

    def list_torrent_files(self, hashes: Sequence[str]) -> list[str]:
        """文件在网盘上，没有本地路径可删。"""
        return []

    def find_torrents_by_path(self, paths: Sequence[str]) -> dict[str, list[str]]:
        """文件在网盘上，本地路径反查无从谈起。"""
        return {}

    def monitor_torrent(self, hashes: Sequence[str] | None = None) -> list[dict]:
        if not self.authorization:
            return []
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    f"{PAN_API}/tasks",
                    headers=self.get_pan_auth(),
                    params={"space": "", "page_token": "", "filters": json.dumps(
                        {"phase": {"in": "PHASE_TYPE_RUNNING,PHASE_TYPE_COMPLETE"}}
                    )},
                )
                response.raise_for_status()
                tasks = response.json().get("tasks", [])

            return [
                {
                    "hash": t.get("id", ""),
                    "name": t.get("name", ""),
                    "progress": round(int(t.get("progress", 0)) / 100, 4),
                    "state": t.get("phase", ""),
                    "save_path": "",
                    "completed": t.get("phase") == "PHASE_TYPE_COMPLETE",
                }
                for t in tasks
            ]
        except Exception as exc:
            logger.error(f"查询迅雷任务失败: {exc}")
            return []
