"""热更新代码的挂载。

程序有两份代码：镜像里烧死的 /app，和热更新解压出来的 overlay
（${DATA_DIR}/updates/backend/current）。overlay 版本比镜像新时把它插到
sys.path 最前面，import app.* 就会优先命中新代码。

放 /data 而不是覆盖 /app 是有意的：/data 是挂载卷，docker compose pull
换掉镜像之后 overlay 还在。此时镜像可能已经追上或超过 overlay，
_should_use 会把它判定为过期，交给调用方清理，避免新镜像被旧代码盖住。

这个模块必须零依赖、只用标准库 —— 它在 app 包完成导入之前就要跑，
此时任何 app.* 的导入都可能拿到"应该被 overlay 覆盖掉"的那一份。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+))?$")

# 相对项目根的路径，overlay 与镜像共用这套布局
_VERSION_FILE = "VERSION"


def parse_version(text: str) -> tuple[int, int, int, int] | None:
    """解析 x.y.z 或 x.y.z-n，不合法返回 None。

    `-n` 是修订号，语义是"x.y.z 的第 n 次修订"，比 x.y.z 本身新：

        0.0.8 < 0.0.8-1 < 0.0.8-2 < 0.0.9

    注意这与标准 semver 相反 —— 那里 `-n` 是 prerelease，排在正式版
    之前。这里按修订号处理是刻意的：装了 0.0.8-2 的机器不该被镜像里的
    0.0.8 判成"有更新"而回退。

    返回四元组，不带后缀时第四位是 0，直接用元组比较即可。
    """
    match = _SEMVER_RE.match((text or "").strip())
    if not match:
        return None
    major, minor, patch, revision = match.groups()
    return (int(major), int(minor), int(patch), int(revision or 0))


def data_dir() -> Path:
    """数据目录。与 config 里的取法保持一致，但不导入 config。"""
    return Path(os.getenv("DATA_DIR") or "./data")


def update_root() -> Path:
    return data_dir() / "updates"


def backend_current() -> Path:
    """后端 overlay 的生效目录。"""
    return update_root() / "backend" / "current"


def backend_backup() -> Path:
    """上一版后端，回滚用。"""
    return update_root() / "backend" / "backup"


def web_current() -> Path:
    """前端 overlay 目录。nginx 直接读这里，不经过 Python。"""
    return update_root() / "web" / "current"


def web_version() -> str:
    """已装前端 overlay 的版本号，没装过返回空。

    前后端分开发包后这个值必须单独记：只更新前端的那些版本不会动后端
    overlay，APP_VERSION 停在原地，光看它会把同一次前端更新反复判成待更新。

    版本号写在 overlay 目录内的 VERSION 里。前端包本身不带这个文件
    （里面是 Vite 产物），由安装流程写入 —— 放目录里而不是另起一个文件，
    是为了让 _replace_dir 的整体替换把它一起换掉，不会出现目录换了、
    版本号还是旧的这种错位。
    """
    return read_version(web_current())


def read_version(root: Path) -> str:
    try:
        return (root / _VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def image_root() -> Path:
    """镜像内代码的根目录（app/core/overlay.py → 上两级）。"""
    return Path(__file__).resolve().parents[2]


def image_version() -> str:
    """镜像自带的版本号。

    注意不能用 app.core.version.APP_VERSION —— 那个模块可能已经是
    overlay 里的了，读出来是 overlay 的版本而不是镜像的。
    """
    return read_version(image_root())


def _should_use(overlay: Path, image: Path) -> tuple[bool, str]:
    """overlay 是否该生效。返回 (是否生效, 原因)。"""
    if not (overlay / "app").is_dir():
        return False, "overlay 不存在"

    overlay_parsed = parse_version(read_version(overlay))
    if not overlay_parsed:
        return False, "overlay 版本号不可读"

    image_parsed = parse_version(read_version(image))
    if not image_parsed:
        # 镜像版本读不出来时保守放行，否则热更新在这种环境下永远失效
        return True, "镜像版本号不可读，按 overlay 生效处理"

    if overlay_parsed <= image_parsed:
        return False, "镜像版本不低于 overlay"
    return True, ""


def activate() -> str:
    """挂载 overlay，返回实际生效的版本号。

    overlay 过期（镜像已经追上）时顺手删掉，免得每次启动都判一遍，
    也省下挂载卷的空间。删失败不影响启动。
    """
    image = image_root()
    overlay = backend_current()

    use, reason = _should_use(overlay, image)
    if not use:
        if reason == "镜像版本不低于 overlay":
            shutil.rmtree(overlay, ignore_errors=True)
            shutil.rmtree(backend_backup(), ignore_errors=True)
        return read_version(image)

    path = str(overlay)
    # 已经在最前面就不重复插
    if sys.path and sys.path[0] == path:
        return read_version(overlay)
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    return read_version(overlay)


def active_root() -> Path:
    """当前实际在跑的代码根目录。"""
    overlay = backend_current()
    if _should_use(overlay, image_root())[0]:
        return overlay
    return image_root()
