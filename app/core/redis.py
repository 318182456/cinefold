"""Redis 连接管理。

REDIS_URL 未配置或连接失败时，所有接口返回"不可用"，
由调用方回落到数据库缓存 —— 单容器部署不装 Redis 也能正常跑。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

_client: Any = None
_checked = False


def _resolve_url() -> str:
    raw = os.getenv("REDIS_URL", "")
    if not raw:
        try:
            from app.core.config import get_settings
            raw = get_settings().redis_url
        except Exception:
            raw = ""
    return (raw or "").strip()


def get_client() -> Any:
    """返回 Redis 客户端，不可用时返回 None。

    连接只探测一次，失败后不再重试，避免每次缓存读写都卡在超时上。
    """
    global _client, _checked

    if _checked:
        return _client

    _checked = True
    url = _resolve_url()
    if not url:
        return None

    try:
        import redis
    except ImportError:
        logger.warning("已配置 REDIS_URL 但未安装 redis 包，缓存回落到数据库")
        return None

    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
        client.ping()
    except Exception as exc:
        logger.warning(f"Redis 连接失败，缓存回落到数据库: {exc}")
        return None

    _client = client
    logger.info("Redis 已连接")
    return _client


def is_available() -> bool:
    return get_client() is not None


def reset() -> None:
    """配置变更后重新探测连接。"""
    global _client, _checked
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _checked = False


def get(key: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception as exc:
        logger.warning(f"Redis 读取 {key} 失败: {exc}")
        return None


def set(key: str, value: str, ttl: int | None = None) -> bool:
    """写入并设置过期时间。返回是否写入成功。"""
    client = get_client()
    if client is None:
        return False

    if ttl is None:
        try:
            from app.core.config import get_settings
            ttl = get_settings().redis_cache_ttl
        except Exception:
            ttl = 86400

    try:
        # ttl <= 0 视为永不过期
        client.set(key, value, ex=ttl if ttl and ttl > 0 else None)
        return True
    except Exception as exc:
        logger.warning(f"Redis 写入 {key} 失败: {exc}")
        return False


def delete(*keys: str) -> int:
    client = get_client()
    if client is None or not keys:
        return 0
    try:
        return client.delete(*keys)
    except Exception as exc:
        logger.warning(f"Redis 删除失败: {exc}")
        return 0
