"""图片代理。

资源站图片有防盗链，浏览器直连会 403，需要服务端带 Referer 转发。
图源在墙外，每次回源都要重新握手，所以先查本地缓存，只有没命中才出网。
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

from app.api.endpoints import get_current_user
from app.core.config import get_settings
from app.database.models import Code
from app.database.session import session_scope
from app.schemas.reponse import ResponseEntity
from app.utils import get_true_code, imagecache, imgcrop

router = APIRouter(tags=["picproxy"])

# 只代理已知图源，避免被当成任意 URL 转发的开放代理
ALLOWED_HOSTS = (
    # jdbstatic 是 javdb 的图片 CDN，封面/预览图都在这个域名下
    "javbus.com", "javdb.com", "jdbstatic.com", "dmm.co.jp", "avbase.net",
    "javlibrary.com", "jable.tv", "c0930.com", "mgstage.com",
    "s1s1s1.com", "moodyz.com", "ideapocket.com", "madonna-av.com",
    "wanz-factory.com", "attackers.net", "premium-beauty.com",
    "honnaka.jp", "dasdas.jp",
)

CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000"}

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """复用一个 AsyncClient。

    列表页会并发几十张图，每张都新建客户端等于每张都重做 TLS 握手，
    共用连接池后同一个 CDN 的后续请求直接走已建好的连接。
    """
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            proxy=settings.proxy or None,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "Referer": "https://www.javbus.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
            },
        )
    return _client


async def close_client() -> None:
    """应用关闭时释放连接池。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _is_allowed(url: str, extra: str = "") -> bool:
    """按主机名匹配白名单，允许子域名。

    只比对 hostname，避免 javdb.com.evil.com 这类地址靠子串匹配蒙混过关。
    """
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not hostname:
        return False

    allowed = list(ALLOWED_HOSTS)
    allowed += [h.strip().lower() for h in extra.split(",") if h.strip()]
    return any(
        hostname == host or hostname.endswith(f".{host}") for host in allowed
    )


def _cached_response(path, request: Request) -> Response:
    """返回缓存文件，带 ETag 协商。"""
    etag = imagecache.etag_for(path)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={**CACHE_HEADERS, "ETag": etag})

    return FileResponse(
        path,
        media_type=imagecache.content_type(path),
        headers={**CACHE_HEADERS, "ETag": etag},
    )


@router.get("/image-proxy")
async def image_proxy(
    request: Request,
    url: str,
    code: str = "",
    kind: str = "banner",
):
    """代理一张图片。

    code 传番号时按 pics/<番号>/banner.jpg 缓存，与旧版目录布局一致，
    历史缓存不用迁移就能直接命中。
    """
    if not url:
        return Response(status_code=400, content=b"missing url")

    cached = imagecache.find_cached(url, code, kind)
    if cached is not None:
        return _cached_response(cached, request)

    settings = get_settings()
    if not _is_allowed(url, settings.image_proxy_hosts):
        logger.warning(f"拒绝代理非白名单地址: {url[:80]}")
        return Response(status_code=403, content=b"host not allowed")

    try:
        response = await _get_client().get(url)
        response.raise_for_status()
        content = response.content
    except Exception as exc:
        logger.debug(f"图片代理失败 {url[:60]}: {exc}")
        return Response(status_code=404, content=b"fetch failed")

    stored = imagecache.store(content, url, code, kind)
    headers = dict(CACHE_HEADERS)
    if stored is not None:
        headers["ETag"] = imagecache.etag_for(stored)

    return Response(
        content=content,
        media_type=response.headers.get("content-type", "image/jpeg"),
        headers=headers,
    )


@router.get("/image-local")
def image_local(request: Request, path: str):
    """直接返回已缓存的图片。

    path 是库里 local_banner 存的相对路径（如 JUR-119/banner.jpg），
    老库迁进来后这一列就有值，走这里读盘比再拼一次源站 URL 省事。
    """
    target = imagecache.resolve_relative(path)
    if target is None:
        return Response(status_code=404, content=b"not found")
    return _cached_response(target, request)


@router.get("/image-cache/stats")
def image_cache_stats(current_user: str = Depends(get_current_user)):
    """缓存概览，用于确认历史图片是否被识别到。"""
    return ResponseEntity.ok(imagecache.stats())


class RefetchRequest(BaseModel):
    code: str


@router.post("/image-cache/refetch")
async def refetch_cover(
    payload: RefetchRequest,
    current_user: str = Depends(get_current_user),
):
    """重抓某个番号的封面。

    先删缓存再回源，拿到新图后重新判断人像在哪半边，回写 local_banner
    与 portrait_side。源站换图或人像面判错时用它刷新。
    """
    code = get_true_code(payload.code or "")
    if not code:
        return ResponseEntity.fail("番号无效")

    with session_scope() as session:
        item = session.get(Code, code)
        if item is None:
            return ResponseEntity.fail("番号不存在")
        url = (item.banner or item.poster or "").strip()

    if not url:
        return ResponseEntity.fail("该番号没有可用的封面地址")

    settings = get_settings()
    if not _is_allowed(url, settings.image_proxy_hosts):
        logger.warning(f"拒绝重抓非白名单地址: {url[:80]}")
        return ResponseEntity.fail("封面地址不在白名单内")

    imagecache.drop_cached(url, code, "banner")

    try:
        response = await _get_client().get(url)
        response.raise_for_status()
        content = response.content
    except Exception as exc:
        logger.warning(f"重抓封面失败 {code}: {exc}")
        return ResponseEntity.fail("下载封面失败")

    stored = imagecache.store(content, url, code, "banner")
    if stored is None:
        return ResponseEntity.fail("封面无效或写入失败")

    relative = imagecache.relative_of(stored)
    side = imgcrop.detect_portrait_side(content)
    with session_scope() as session:
        item = session.get(Code, code)
        if item is not None:
            item.local_banner = relative
            item.portrait_side = side

    logger.info(f"重抓封面完成 {code} -> {relative} (人像面: {side})")
    return ResponseEntity.ok(
        {"code": code, "local_banner": relative, "portrait_side": side}
    )
