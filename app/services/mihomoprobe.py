"""Bounded, cancellable preflight through the actual local Mihomo listeners."""
import asyncio
import time
from urllib.parse import urlparse

import httpx
from loguru import logger

TTL = 120
TIMEOUT = 4.0
BATCH_SIZE = 3
_slots = None  # Created on the shared scraper I/O event loop, never per request.


def origin(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("代理预检需要有效的 HTTP/HTTPS 目标地址")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{':' + str(parsed.port) if parsed.port else ''}/"


async def race(manager, proxies, url, timeout):
    """Return the first usable candidate, closing every cancelled HTTP client."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(6)
    target = origin(url)

    async def check(index, proxy):
        # A queued health check does not consume a node's network timeout.
        async with _slots:
            parsed = urlparse(proxy)
            local = parsed._replace(netloc=f"{parsed.username}:{parsed.password}@127.0.0.1:{parsed.port}").geturl()
            started = time.monotonic()
            code = None
            reason = ""
            try:
                async def send():
                    async with httpx.AsyncClient(proxy=local, timeout=TIMEOUT, verify=False, trust_env=False) as client:
                        async with client.stream("HEAD", target, follow_redirects=False) as response:
                            return response.status_code
                code = await asyncio.wait_for(send(), TIMEOUT)
                # Authentication/Cloudflare/missing pages still prove the proxy
                # tunnel and target TLS work. Browser clearance is a separate step.
                ok = 200 <= code < 500 and code not in (407, 408, 429)
                reason = f"HTTP {code}"
            except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
                ok = False
                reason = type(exc).__name__
            elapsed = round((time.monotonic() - started) * 1000)
            if not ok:
                manager.note_result(url, proxy, False)
            with manager.lock:
                manager.probes[manager._probe_key(proxy, url)] = (ok, time.monotonic(), elapsed, code)
                if len(manager.probes) > 2048:
                    manager.probes = {key: value for key, value in manager.probes.items() if time.monotonic() - value[1] < TTL}
            logger.debug(f"代理预检 {urlparse(target).hostname} → {manager.proxy_name(proxy)}：{'可连接' if ok else '失败'}，{elapsed}ms，{reason}")
            return index if ok else None

    tasks = [asyncio.create_task(check(index, proxy)) for index, proxy in enumerate(proxies)]
    try:
        async def first():
            for future in asyncio.as_completed(tasks):
                result = await future
                if result is not None:
                    return result
            return None
        return await asyncio.wait_for(first(), timeout=max(0.001, timeout))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
