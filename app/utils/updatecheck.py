"""新版本检测。

以 Docker 镜像标签为准而非 git 提交——用户实际执行的是 docker compose pull，
镜像标签才是"能拉到什么"的真实来源。

只做提示，不碰更新：容器无法替换自身镜像，重建要在宿主机上做。
"""
from __future__ import annotations

import re

import httpx
from loguru import logger

from app.core.config import get_settings
from app.core.version import APP_VERSION

REGISTRY = "https://ghcr.io"
# 与 docker-compose.yml 里的镜像保持一致
IMAGE = "318182456/cinefold"

# 只认 x.y.z，忽略 latest 与 sha-xxxx 这类标签
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# 检测结果缓存一小时，避免每次开页面都打 registry
CACHE_TTL = 3600


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.match((text or "").strip())
    if not match:
        return None
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def _client() -> httpx.Client:
    return httpx.Client(timeout=15, proxy=get_settings().proxy or None)


def _fetch_token(client: httpx.Client) -> str:
    """取 registry 的 pull token。

    公开镜像匿名即可；私有镜像需要带 read:packages 的 GitHub Token，
    未配置时返回空串，由调用方静默跳过。
    """
    settings = get_settings()
    params = {"scope": f"repository:{IMAGE}:pull", "service": "ghcr.io"}
    auth = None
    if settings.github_token:
        # GHCR 的 basic auth 用户名任意，密码放 token
        auth = ("token", settings.github_token)

    response = client.get(f"{REGISTRY}/token", params=params, auth=auth)
    if response.status_code != 200:
        return ""
    return response.json().get("token", "") or ""


def fetch_latest_version() -> str:
    """取镜像仓库里最新的语义化版本标签，失败返回空串。"""
    try:
        with _client() as client:
            token = _fetch_token(client)
            if not token:
                logger.debug("未取到 registry token，跳过版本检测（私有镜像需配置 GITHUB_TOKEN）")
                return ""

            response = client.get(
                f"{REGISTRY}/v2/{IMAGE}/tags/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                logger.debug(f"读取镜像标签失败({response.status_code})")
                return ""

            tags = response.json().get("tags") or []
    except Exception as exc:
        logger.debug(f"版本检测失败: {exc}")
        return ""

    versions = [(parsed, tag) for tag in tags if (parsed := parse_version(tag))]
    if not versions:
        return ""
    return max(versions)[1]


def _read_cache() -> str:
    """读缓存。缓存不可用时返回空串，让调用方直接去查。"""
    try:
        from app import services
        return services.get_rank_cache("update", "latest", ttl=CACHE_TTL) or ""
    except Exception as exc:
        logger.debug(f"读取版本缓存失败: {exc}")
        return ""


def _write_cache(latest: str) -> None:
    """缓存写失败不能影响检测结果，只记日志。"""
    try:
        from app import services
        services.set_rank_cache("update", "latest", latest)
    except Exception as exc:
        logger.debug(f"写入版本缓存失败: {exc}")


def check_update(use_cache: bool = True) -> dict:
    """对比当前版本与镜像仓库最新版。

    返回 {current, latest, has_update, checked}。checked 为 False 表示
    这次没查到（未配置 token、网络不通等），前端据此不显示红点。
    """
    current = APP_VERSION

    cached = _read_cache() if use_cache else None
    latest = cached if cached else fetch_latest_version()
    if not cached and latest:
        _write_cache(latest)

    result = {
        "current": current,
        "latest": latest,
        "has_update": False,
        "checked": bool(latest),
    }

    current_parsed, latest_parsed = parse_version(current), parse_version(latest)
    if current_parsed and latest_parsed:
        result["has_update"] = latest_parsed > current_parsed
    return result
