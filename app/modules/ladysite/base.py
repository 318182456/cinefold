"""资源站抓取的公共基础。

统一 HTTP 客户端、请求头与限速，避免各站点重复实现。
"""
from __future__ import annotations

import random
import ssl
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
from loguru import logger

from app.core.config import get_settings

# bypass 服务类型探测结果缓存：{base_url: 是否为 FlareSolverr}
_BYPASS_KIND: dict[str, bool] = {}

# 解 Cloudflare 挑战要跑一个真实浏览器，给足时间（毫秒）
BYPASS_SOLVE_TIMEOUT_MS = 90000

# 连通性测试用的较短上限。测试是人在页面上等结果，且 _BYPASS_LOCK 会让
# 多个源串行，沿用 90s 会直接顶穿前端 60s 超时。
BYPASS_CHECK_TIMEOUT_MS = 20000

# 连接类异常（SSL EOF、连接重置）的额外重试次数
CONNECT_RETRIES = 1

# FlareSolverr 每个会话独占一个浏览器实例，并发请求会互相拖慢甚至超时，
# 因此同一时刻只允许一个请求进入。
_BYPASS_LOCK = threading.Lock()

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 站点普遍有频率限制，两次请求之间至少间隔这么久
MIN_INTERVAL_SECONDS = 1.0


@dataclass
class CodeInfo:
    """抓取到的番号信息。字段与 database.models.Code 对齐。"""
    code: str = ""
    title: str = ""
    release_date: str = ""
    duration: str = ""
    producer: str = ""
    publisher: str = ""
    series: str = ""
    genres: str = ""
    casts: str = ""
    star: float | None = None
    banner: str = ""
    poster: str = ""
    still_photo: str = ""
    preview_url: str = ""

    def to_dict(self, skip_empty: bool = True) -> dict:
        data = {
            "code": self.code,
            "title": self.title,
            "release_date": self.release_date,
            "duration": self.duration,
            "producer": self.producer,
            "publisher": self.publisher,
            "series": self.series,
            "genres": self.genres,
            "casts": self.casts,
            "star": self.star,
            "banner": self.banner,
            "poster": self.poster,
            "still_photo": self.still_photo,
            "preview_url": self.preview_url,
        }
        return {k: v for k, v in data.items() if v} if skip_empty else data


@dataclass
class ActorInfo:
    name: str = ""
    name_2: str = ""
    photo: str = ""

    def to_dict(self, skip_empty: bool = True) -> dict:
        data = {"name": self.name, "name_2": self.name_2, "photo": self.photo}
        return {k: v for k, v in data.items() if v} if skip_empty else data


class SiteClient:
    """带限速的 HTTP 客户端。

    节流状态按 host 共享：调用方普遍是 Avdb() / Bus() 这样用完即弃，
    状态放在实例上等于每次请求都从零计时，限速根本不会生效。
    """

    # host → (锁, 上次请求时间)。进程级共享，跨实例、跨线程都算同一份
    _throttle_state: dict[str, list] = {}
    _state_lock = threading.Lock()

    def __init__(
        self,
        host: str,
        cookie: str = "",
        interval: float = MIN_INTERVAL_SECONDS,
        bypass_first: bool = False,
    ):
        self.host = host.rstrip("/")
        self.cookie = cookie
        self.interval = interval
        # 已知常年过盾的站点置 True，省掉一次必然 403 的直连
        self.bypass_first = bypass_first

    @classmethod
    def from_source(cls, key: str) -> "SiteClient | None":
        """按数据源配置建客户端。源不存在或被停用时返回 None。

        地址、节流、cookie 都从库里读，页面上改完立刻生效。
        """
        from app.modules.ladysite.sources import get_source

        source = get_source(key)
        if source is None or not source.get("enabled", True):
            return None
        if not source.get("host"):
            return None

        return cls(
            source["host"],
            cookie=source.get("cookie", ""),
            interval=source.get("interval") or MIN_INTERVAL_SECONDS,
            bypass_first=source.get("bypass_first", False),
        )

    @classmethod
    def reset_throttle(cls, key: str = "") -> None:
        """清节流状态。改了地址或间隔后调用，避免沿用旧 host 的计时。"""
        with cls._state_lock:
            if not key:
                cls._throttle_state.clear()
                return
            from app.modules.ladysite.sources import get_source
            source = get_source(key)
            host = (source or {}).get("host", "").rstrip("/")
            cls._throttle_state.pop(host, None)

    def _state(self) -> list:
        """取本 host 的节流状态 [lock, last_request]。"""
        with SiteClient._state_lock:
            state = SiteClient._throttle_state.get(self.host)
            if state is None:
                state = [threading.Lock(), 0.0]
                SiteClient._throttle_state[self.host] = state
            return state

    def headers(self, extra: dict | None = None) -> dict:
        base = {
            "User-Agent": DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            "Referer": f"{self.host}/",
        }
        if self.cookie:
            base["Cookie"] = self.cookie
        base.update(extra or {})
        return base

    def _throttle(self) -> None:
        state = self._state()
        lock, _ = state[0], state[1]
        with lock:
            elapsed = time.time() - state[1]
            wait = self.interval - elapsed
            if wait > 0:
                # 加一点随机抖动，避免请求节奏过于规整
                time.sleep(wait + random.uniform(0, 0.3))
            state[1] = time.time()

    def get(self, path: str, params: dict | None = None, timeout: float = 15.0, **kwargs) -> str:
        """GET 请求，返回文本。失败返回空串。

        默认超时偏短：抓取由定时任务驱动，单站不通时应尽快让位给下一个站点。
        配置了 BYPASS_URL 时，直连遇到 403/503 会自动改走该服务重试一次；
        站点声明了 bypass_first 则跳过直连，直接走该服务。
        """
        settings = get_settings()
        url = path if path.startswith("http") else f"{self.host}{path}"

        # 直连必被拦的站点，配了 bypass 就不必先撞一次 403
        if self.bypass_first and settings.bypass_url:
            return fetch_via_bypass(url, params, timeout)

        headers = self.headers(kwargs.pop("headers", None))

        for attempt in range(CONNECT_RETRIES + 1):
            self._throttle()
            try:
                with httpx.Client(
                    timeout=timeout,
                    follow_redirects=True,
                    proxy=settings.proxy or None,
                    verify=False,
                    # 只认配置里的 PROXY，避免被宿主机环境变量里的代理劫持
                    trust_env=False,
                ) as client:
                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    return response.text
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # 403/503 是典型的挑战页；502/504 多半是站点前面的防护层
                # 掐了直连。两类都值得让 bypass 服务再试一次
                if status in (403, 503) or status >= 500:
                    logger.info(f"{url} 返回 {status}，尝试通过 bypass 服务获取")
                    return fetch_via_bypass(url, params, timeout)
                logger.warning(f"请求 {url} 失败: {status}")
                return ""
            except httpx.TimeoutException as exc:
                # 超时不重试直连：站点本来就慢，再等一轮只会拖住后面的站点。
                # 但部分站点是对直连静默丢包而非返回挑战页，配了 bypass 就交给它
                if settings.bypass_url:
                    logger.info(f"{url} 直连超时，尝试通过 bypass 服务获取")
                    return fetch_via_bypass(url, params, timeout)
                logger.warning(f"请求 {url} 超时: {exc}")
                return ""
            except (httpx.TransportError, ssl.SSLError) as exc:
                # SSL EOF、连接重置这类抖动重试一次多半就好，节流已保证不会打太密
                if attempt < CONNECT_RETRIES:
                    logger.debug(f"请求 {url} 连接异常，重试: {exc}")
                    continue
                logger.warning(f"请求 {url} 异常: {exc}")
            except Exception as exc:
                logger.warning(f"请求 {url} 异常: {exc}")
                return ""
        return ""


def fetch_via_bypass(
    url: str,
    params: dict | None = None,
    timeout: float = 60.0,
    quick: bool = False,
) -> str:
    """通过用户自建的 bypass 服务抓取页面。

    兼容两类常见服务：
    - FlareSolverr：POST /v1，body 为 {cmd, url, maxTimeout}
    - cloudflare-bypass-for-scraping：GET /html?url=...

    通过 BYPASS_URL 配置服务地址；未配置时直接返回空串。
    quick=True 用更短的过盾上限，供连通性测试这类有人在等的场景。
    """
    settings = get_settings()
    base = (settings.bypass_url or "").rstrip("/")
    if not base:
        return ""

    # 把 params 并进 URL，bypass 服务只接受完整地址
    if params:
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{query}"

    # 过盾需要启动浏览器并等待挑战完成，耗时远超普通请求，
    # 因此不沿用调用方的直连超时，改用独立的宽松上限。
    solver_timeout = BYPASS_CHECK_TIMEOUT_MS if quick else BYPASS_SOLVE_TIMEOUT_MS
    client_timeout = solver_timeout / 1000 + 30

    try:
        with _BYPASS_LOCK, httpx.Client(
            timeout=client_timeout, verify=False, trust_env=False
        ) as client:
            if base.endswith("/v1") or _is_flaresolverr(base):
                endpoint = base if base.endswith("/v1") else f"{base}/v1"
                response = client.post(
                    endpoint,
                    json={"cmd": "request.get", "url": url, "maxTimeout": solver_timeout},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") != "ok":
                    logger.warning(f"bypass 返回失败: {payload.get('message', '')[:120]}")
                    return ""
                return (payload.get("solution") or {}).get("response", "")

            # cloudflare-bypass-for-scraping 风格
            response = client.get(f"{base}/html", params={"url": url})
            response.raise_for_status()
            return response.text
    except Exception as exc:
        logger.warning(f"bypass 服务请求失败: {exc}")
        return ""


def _is_flaresolverr(base: str) -> bool:
    """探测服务类型，结果缓存避免重复请求。"""
    if base in _BYPASS_KIND:
        return _BYPASS_KIND[base]

    is_flare = False
    try:
        # bypass 服务通常在内网，不能走系统代理
        with httpx.Client(timeout=8, verify=False, trust_env=False) as client:
            root = base[: -len("/v1")] if base.endswith("/v1") else base
            response = client.get(root)
            body = (response.text or "").lower()
            is_flare = "flaresolverr" in body
    except Exception:
        pass

    _BYPASS_KIND[base] = is_flare
    return is_flare


def parse_star(text: str) -> float | None:
    """从 "4.5分, 由123人评价" 这类文本里取出评分。"""
    import re
    match = re.search(r"([\d.]+)\s*分", text or "")
    if not match:
        match = re.search(r"([\d.]+)", text or "")
    if not match:
        return None
    try:
        value = float(match.group(1))
        return value if 0 <= value <= 10 else None
    except ValueError:
        return None


def join_list(items) -> str:
    """列表转逗号分隔字符串，去重且保序。"""
    seen, out = set(), []
    for item in items or []:
        item = (item or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return ",".join(out)
