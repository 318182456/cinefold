"""热更新：下载 zip、校验、安装、重启。

产物来自 GitHub Releases，每个版本挂三个文件：

    backend-<version>.zip    app/ + main.py + VERSION + requirements.txt
    frontend-<version>.zip   web/dist 的内容
    manifest.json            两个包的 sha256 与体积

安装落到 ${DATA_DIR}/updates/ 而不是覆盖 /app：那里是挂载卷，重建容器
不会丢；镜像反超时 overlay 会被自动判废（见 app.core.overlay）。

后端重启靠进程自杀 —— supervisord 配了 autorestart=true，退出即被拉起，
新进程启动时 overlay 已经就位。前端不需要重启，nginx 每次请求都读磁盘。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx
from loguru import logger

from app.core import overlay
from app.core.config import get_settings
from app.core.version import APP_VERSION

# GitHub Releases API。与镜像仓库同源，换自建分发时只改这里
GITHUB_REPO = "318182456/cinefold"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 单包体积上限，防止下到异常大的文件把磁盘写满
MAX_PACKAGE_BYTES = 300 * 1024 * 1024

# 解压后的总大小上限，挡 zip bomb
MAX_EXTRACT_BYTES = 600 * 1024 * 1024

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)

# 版本检测结果缓存，避免每次开页面都打 API
CHECK_CACHE_TTL = 3600


# ======================================================================
# 进度状态
# ======================================================================
@dataclass
class UpgradeState:
    """一次升级的实时状态，前端轮询这个。

    进程重启后内存状态归零，前端据此判断"重启完成了"—— 拿不到
    running 的状态且版本号已经变了，就是成功。
    """
    running: bool = False
    stage: str = ""          # download / verify / install / deps / restart / done / failed
    message: str = ""
    percent: int = 0
    target: str = ""         # 正在装的版本
    error: str = ""
    started_at: float = 0.0
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_state = UpgradeState()
_state_lock = threading.Lock()


def _set_state(stage: str, message: str, percent: int, **kwargs) -> None:
    with _state_lock:
        _state.stage = stage
        _state.message = message
        _state.percent = percent
        for key, value in kwargs.items():
            setattr(_state, key, value)
        # 只留最近 50 条，长跑任务不会把内存撑爆
        _state.logs.append(f"{time.strftime('%H:%M:%S')} {message}")
        del _state.logs[:-50]
    logger.info(f"[升级] {message}")


def get_state() -> dict:
    with _state_lock:
        return _state.to_dict()


# ======================================================================
# 版本检测
# ======================================================================
def _client() -> httpx.Client:
    return httpx.Client(
        timeout=DOWNLOAD_TIMEOUT,
        proxy=get_settings().proxy or None,
        follow_redirects=True,
    )


def _api_headers() -> dict:
    """GitHub API 请求头。带 token 能把匿名的 60 次/小时抬到 5000。"""
    headers = {"Accept": "application/vnd.github+json"}
    token = get_settings().github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_latest_release() -> dict:
    """取最新 release 的版本号与产物列表，失败返回空 dict。"""
    try:
        with _client() as client:
            response = client.get(RELEASE_API, headers=_api_headers())
            if response.status_code != 200:
                logger.debug(f"读取 release 失败({response.status_code})")
                return {}
            data = response.json()
    except Exception as exc:
        logger.debug(f"版本检测失败: {exc}")
        return {}

    tag = (data.get("tag_name") or "").strip()
    if not overlay.parse_version(tag):
        logger.debug(f"release 标签 {tag!r} 不是语义化版本，忽略")
        return {}

    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or []}
    return {
        "version": tag.lstrip("v"),
        "assets": assets,
        "notes": (data.get("body") or "")[:4000],
        "published_at": data.get("published_at") or "",
    }


def _read_cache() -> dict:
    try:
        from app import services
        raw = services.get_rank_cache("update", "release", ttl=CHECK_CACHE_TTL)
        return json.loads(raw) if raw else {}
    except Exception as exc:
        logger.debug(f"读取版本缓存失败: {exc}")
        return {}


def _write_cache(payload: dict) -> None:
    try:
        from app import services
        services.set_rank_cache("update", "release", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        logger.debug(f"写入版本缓存失败: {exc}")


def check_update(use_cache: bool = True) -> dict:
    """对比当前版本与最新 release。

    返回 {current, latest, has_update, checked, can_upgrade, notes, assets}。
    checked 为 False 表示这次没查到（网络不通、限流等），前端不显示红点。
    can_upgrade 额外要求两个 zip 都挂上去了 —— 只发了镜像没发 zip 的版本
    能提示但不能一键装。
    """
    release = _read_cache() if use_cache else {}
    if not release:
        release = fetch_latest_release()
        if release:
            _write_cache(release)

    latest = release.get("version", "")
    assets = release.get("assets") or {}

    result = {
        "current": APP_VERSION,
        "latest": latest,
        "has_update": False,
        "checked": bool(latest),
        "can_upgrade": False,
        "notes": release.get("notes", ""),
        "published_at": release.get("published_at", ""),
    }

    current_parsed = overlay.parse_version(APP_VERSION)
    latest_parsed = overlay.parse_version(latest)
    if current_parsed and latest_parsed and latest_parsed > current_parsed:
        result["has_update"] = True
        result["can_upgrade"] = bool(
            assets.get(f"backend-{latest}.zip") and assets.get(f"frontend-{latest}.zip")
        )
    return result


# ======================================================================
# 下载与校验
# ======================================================================
def _download(client: httpx.Client, url: str, dest: Path, label: str, base_percent: int, span: int) -> None:
    """流式下载并实时报进度。超过体积上限直接中断。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    with client.stream("GET", url, headers=_api_headers()) as response:
        if response.status_code != 200:
            raise RuntimeError(f"{label} 下载失败，HTTP {response.status_code}")

        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_PACKAGE_BYTES:
            raise RuntimeError(f"{label} 体积 {total} 超过上限")

        with dest.open("wb") as fh:
            for chunk in response.iter_bytes(64 * 1024):
                fh.write(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_PACKAGE_BYTES:
                    raise RuntimeError(f"{label} 体积超过上限，已中断")
                if total:
                    percent = base_percent + int(span * downloaded / total)
                    _set_state("download", f"下载 {label} {downloaded * 100 // total}%", percent)

    _set_state("download", f"{label} 下载完成（{downloaded // 1024} KB）", base_percent + span)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected: str, label: str) -> None:
    """校验 sha256。manifest 里没给就跳过，只记日志。"""
    if not expected:
        logger.warning(f"manifest 未提供 {label} 的 sha256，跳过校验")
        return
    actual = _sha256(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(f"{label} 校验失败：期望 {expected[:12]}…，实际 {actual[:12]}…")
    _set_state("verify", f"{label} 校验通过", _state.percent)


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压到 dest。

    zip 里的路径全部当相对路径处理，任何越界（.. 或绝对路径）直接拒绝——
    包是从网络下来的，不能假设它规矩。同时累计解压后体积挡 zip bomb。
    """
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    extracted = 0

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue

            target = (dest / name).resolve()
            if not str(target).startswith(str(resolved_dest) + os.sep) and target != resolved_dest:
                raise RuntimeError(f"压缩包内路径越界：{name}")

            extracted += info.file_size
            if extracted > MAX_EXTRACT_BYTES:
                raise RuntimeError("解压体积超过上限，包可能被篡改")

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _replace_dir(staging: Path, current: Path, backup: Path | None) -> None:
    """用 staging 替换 current，旧的挪去 backup。

    先挪后放，中间有个极短的窗口 current 不存在。这个窗口只影响
    "启动时读 overlay"，正在跑的进程已经把代码 import 进内存了，不受影响。
    """
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)

    current.parent.mkdir(parents=True, exist_ok=True)
    if current.exists():
        if backup is not None:
            shutil.move(str(current), str(backup))
        else:
            shutil.rmtree(current, ignore_errors=True)

    shutil.move(str(staging), str(current))


# ======================================================================
# 依赖安装
# ======================================================================
def _install_deps(root: Path) -> None:
    """按新版的 requirements.txt 补依赖。

    新版本引入新依赖时，光换代码会直接 ImportError。这里跑一次 pip install，
    已装的会被跳过，只有新增/升级的才真下载。

    离线环境装不上是常态，所以失败只警告不中断 —— 真缺依赖的话下面
    的导入自检会拦住，那才是该回滚的时候。
    """
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        _set_state("deps", "更新包未带 requirements.txt，跳过依赖安装", 75)
        return

    _set_state("deps", "检查 Python 依赖…", 72)
    command = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(requirements)]

    env = dict(os.environ)
    proxy = get_settings().proxy
    if proxy and proxy.startswith("http"):
        # pip 不认 socks5 之外的自定义格式，只在 http(s) 代理时透传
        env.setdefault("HTTP_PROXY", proxy)
        env.setdefault("HTTPS_PROXY", proxy)

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=600, env=env
        )
    except Exception as exc:
        _set_state("deps", f"依赖安装未能执行（{exc}），继续", 75)
        return

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        _set_state("deps", f"依赖安装失败，继续尝试启动：{' / '.join(tail)}", 75)
        return

    _set_state("deps", "依赖已就绪", 75)


def _smoke_test(root: Path) -> None:
    """拿新代码起一个子进程试导入，导不动就别装。

    这是回滚的唯一判据。放子进程里跑是因为当前进程已经 import 过 app.*，
    再导一次拿到的是缓存，测不出新代码的问题。
    """
    _set_state("install", "校验新版本可加载…", 82)
    script = (
        "import sys; sys.path.insert(0, r'%s');"
        "import app.api;"
        "print('ok')" % str(root)
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=180, env=env,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("新版本加载超时，已回滚")

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError(f"新版本无法加载：{' / '.join(tail) or '未知错误'}")


# ======================================================================
# 升级主流程
# ======================================================================
def _fetch_manifest(client: httpx.Client, assets: dict, version: str) -> dict:
    """取 manifest.json 里的校验信息。取不到返回空 dict，走跳过校验的分支。"""
    url = assets.get("manifest.json")
    if not url:
        return {}
    try:
        response = client.get(url, headers=_api_headers())
        if response.status_code != 200:
            return {}
        data = response.json()
    except Exception as exc:
        logger.debug(f"读取 manifest 失败: {exc}")
        return {}

    if (data.get("version") or "").lstrip("v") != version:
        logger.warning("manifest 版本号与 release 不符，跳过校验")
        return {}
    return data.get("files") or {}


def _do_upgrade(target: str, assets: dict) -> None:
    """真正干活的部分，跑在后台线程里。"""
    workspace = overlay.update_root() / "staging"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)

    backend_zip = workspace / f"backend-{target}.zip"
    frontend_zip = workspace / f"frontend-{target}.zip"
    backend_dir = workspace / "backend"
    frontend_dir = workspace / "web"

    try:
        with _client() as client:
            manifest = _fetch_manifest(client, assets, target)

            _set_state("download", f"开始下载 {target}", 5, target=target)
            _download(client, assets[f"backend-{target}.zip"], backend_zip, "后端包", 5, 30)
            _download(client, assets[f"frontend-{target}.zip"], frontend_zip, "前端包", 35, 25)

        _set_state("verify", "校验安装包…", 62)
        _verify(backend_zip, (manifest.get(f"backend-{target}.zip") or {}).get("sha256", ""), "后端包")
        _verify(frontend_zip, (manifest.get(f"frontend-{target}.zip") or {}).get("sha256", ""), "前端包")

        _set_state("install", "解压安装包…", 66)
        _safe_extract(backend_zip, backend_dir)
        _safe_extract(frontend_zip, frontend_dir)

        # 包里必须有这两样，否则装上去就是个坏 overlay
        if not (backend_dir / "app").is_dir():
            raise RuntimeError("后端包结构异常：缺少 app 目录")
        unpacked = overlay.read_version(backend_dir)
        if unpacked != target:
            raise RuntimeError(f"后端包版本不符：期望 {target}，实际 {unpacked or '空'}")
        if not any(frontend_dir.glob("index.html")):
            raise RuntimeError("前端包结构异常：缺少 index.html")

        _install_deps(backend_dir)
        _smoke_test(backend_dir)

        # 两个包都验过了才动生效目录，尽量缩短前后端版本不一致的窗口
        _set_state("install", "写入新版本…", 88)
        _replace_dir(backend_dir, overlay.backend_current(), overlay.backend_backup())
        _replace_dir(frontend_dir, overlay.web_current(), None)

        _set_state("restart", f"{target} 已就位，正在重启…", 95)
    except Exception as exc:
        logger.exception("升级失败")
        with _state_lock:
            _state.running = False
            _state.error = str(exc)
        _set_state("failed", f"升级失败：{exc}", 0)
        shutil.rmtree(workspace, ignore_errors=True)
        return

    shutil.rmtree(workspace, ignore_errors=True)
    _set_state("done", f"已更新到 {target}", 100)

    # 留点时间让前端把"重启中"这一帧拿到，再自杀等 supervisord 拉起
    threading.Timer(2.0, _restart_process).start()


def _restart_process() -> None:
    """退出进程，交给 supervisord 重启。

    直接 os._exit 而不是 sys.exit：uvicorn 在自己的线程里跑，普通退出
    会被它的信号处理拦下来，进程能挂在那儿不动。
    """
    logger.info("[升级] 重启进程以加载新版本")
    try:
        from app.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def start_upgrade(target: str = "") -> tuple[bool, str]:
    """启动升级，立即返回。真正的活在后台线程里做。

    返回 (是否已启动, 提示)。
    """
    with _state_lock:
        if _state.running:
            return False, "已有升级任务在进行中"

    release = fetch_latest_release()
    if not release:
        return False, "查询新版本失败，检查网络或代理配置"

    latest = release["version"]
    if target and target != latest:
        return False, f"只能升级到最新版 {latest}"

    current_parsed = overlay.parse_version(APP_VERSION)
    latest_parsed = overlay.parse_version(latest)
    if not latest_parsed:
        return False, "最新版本号格式异常"
    if current_parsed and latest_parsed <= current_parsed:
        return False, f"当前已是 {APP_VERSION}，无需更新"

    assets = release.get("assets") or {}
    missing = [
        name for name in (f"backend-{latest}.zip", f"frontend-{latest}.zip")
        if not assets.get(name)
    ]
    if missing:
        return False, f"该版本未提供更新包（缺 {', '.join(missing)}），请用 docker compose pull"

    with _state_lock:
        _state.running = True
        _state.error = ""
        _state.target = latest
        _state.started_at = time.time()
        _state.logs.clear()

    threading.Thread(
        target=_do_upgrade, args=(latest, assets), name="upgrade", daemon=True
    ).start()
    return True, f"已开始升级到 {latest}"


def rollback() -> tuple[bool, str]:
    """回退到上一版。

    backup 里是升级前那一版的 overlay。它可能不存在 —— 第一次热更新之前
    跑的是镜像自带的代码，此时"回退"就是删掉 overlay 让镜像重新生效。
    """
    with _state_lock:
        if _state.running:
            return False, "升级进行中，无法回退"

    current, backup = overlay.backend_current(), overlay.backend_backup()
    if not current.exists():
        return False, "当前跑的就是镜像自带版本，没有可回退的更新"

    try:
        if backup.exists():
            target_version = overlay.read_version(backup) or "上一版本"
            shutil.rmtree(current, ignore_errors=True)
            shutil.move(str(backup), str(current))
        else:
            target_version = f"镜像版本 {overlay.image_version()}"
            shutil.rmtree(current, ignore_errors=True)
            # 后端退回镜像了，前端 overlay 也得跟着退，否则前后端版本对不上
            shutil.rmtree(overlay.web_current(), ignore_errors=True)
    except Exception as exc:
        logger.exception("回退失败")
        return False, f"回退失败：{exc}"

    _set_state("restart", f"已回退到 {target_version}，正在重启…", 100)
    threading.Timer(2.0, _restart_process).start()
    return True, f"已回退到 {target_version}，正在重启"


def upgrade_info() -> dict:
    """当前安装状态，给设置页展示用。"""
    current, backup = overlay.backend_current(), overlay.backend_backup()
    return {
        "running_version": APP_VERSION,
        "image_version": overlay.image_version(),
        "overlay_version": overlay.read_version(current) if current.exists() else "",
        "backup_version": overlay.read_version(backup) if backup.exists() else "",
        "can_rollback": current.exists(),
        "update_dir": str(overlay.update_root()),
    }


# ======================================================================
# 自动更新
# ======================================================================
def auto_upgrade() -> int:
    """定时任务入口。开关关着就什么都不做。

    返回值只为了和其他定时任务的签名保持一致（调度器会记录条数）。
    """
    if not get_settings().auto_update_enabled:
        return 0

    result = check_update(use_cache=False)
    if not result.get("has_update"):
        return 0
    if not result.get("can_upgrade"):
        logger.info(f"检测到新版本 {result['latest']}，但未提供更新包，跳过自动更新")
        return 0

    logger.info(f"自动更新：检测到 {result['latest']}，开始升级")
    started, message = start_upgrade()
    if not started:
        logger.warning(f"自动更新未启动：{message}")
        return 0
    return 1
