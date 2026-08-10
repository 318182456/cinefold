"""图片代理。

资源站图片有防盗链，浏览器直连会 403，需要服务端带 Referer 转发。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Response
from loguru import logger

from app.core.config import get_settings

router = APIRouter(tags=["picproxy"])

# 只代理已知图源，避免被当成任意 URL 转发的开放代理
ALLOWED_HOSTS = (
    "javbus.com", "javdb.com", "dmm.co.jp", "avbase.net",
    "javlibrary.com", "jable.tv", "c0930.com", "mgstage.com",
    "s1s1s1.com", "moodyz.com", "ideapocket.com", "madonna-av.com",
    "wanz-factory.com", "attackers.net", "premium-beauty.com",
    "honnaka.jp", "dasdas.jp",
)

CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000"}


@router.get("/image-proxy")
def image_proxy(url: str):
    if not url:
        return Response(status_code=400, content=b"missing url")

    if not any(host in url for host in ALLOWED_HOSTS):
        logger.warning(f"拒绝代理非白名单地址: {url[:80]}")
        return Response(status_code=403, content=b"host not allowed")

    settings = get_settings()
    try:
        with httpx.Client(
            timeout=30, follow_redirects=True, proxy=settings.proxy or None
        ) as client:
            response = client.get(
                url,
                headers={
                    "Referer": "https://www.javbus.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                },
            )
            response.raise_for_status()

        return Response(
            content=response.content,
            media_type=response.headers.get("content-type", "image/jpeg"),
            headers=CACHE_HEADERS,
        )
    except Exception as exc:
        logger.debug(f"图片代理失败 {url[:60]}: {exc}")
        return Response(status_code=404, content=b"fetch failed")
