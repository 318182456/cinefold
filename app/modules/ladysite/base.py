"""资源站抓取的公共基础。

统一 HTTP 客户端、请求头与限速，避免各站点重复实现。
"""
from __future__ import annotations

import random
import re
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
from loguru import logger

from app.core.config import get_settings
from app.utils import clean_header_value
from app.modules.ladysite.transport import BrowserQueue, FetchCancelled, current_budget, http_client, report_response
from app.services.mihomo import source_context, proxy_lease

# bypass 服务类型探测结果缓存：{base_url: 是否讲 FlareSolverr 协议}
# 覆盖 FlareSolverr / Byparr / TRAWL，三家都是 POST /v1
_BYPASS_KIND: dict[str, bool] = {}

# 解 Cloudflare 挑战要跑一个真实浏览器，给足时间（毫秒）
BYPASS_SOLVE_TIMEOUT_MS = 90000

# 连通性测试用的较短上限。测试是人在页面上等结果，且 _BYPASS_LOCK 会让
# 多个源串行，沿用 90s 会直接顶穿前端 60s 超时。
BYPASS_CHECK_TIMEOUT_MS = 20000

# Browser navigation can itself consume 30 seconds. Give a normal attempt room
# for acquisition/challenges rather than splitting 90 seconds into three 30s jobs.
BYPASS_NODE_WINDOW_MS = 40000

# 连接类异常（SSL EOF、连接重置）的额外重试次数
CONNECT_RETRIES = 1

# 这类服务每个会话独占一个浏览器实例，并发请求会互相拖慢甚至超时，
# 因此同一时刻只允许一个请求进入。
_BYPASS_LOCK = threading.Lock()
_BYPASS_QUEUES: dict[str, BrowserQueue] = {}
_BYPASS_QUEUE_LOCK = threading.Lock()
BYPASS_QUEUE_TIMEOUT = 20.0
BYPASS_MANAGED_QUEUE_TIMEOUT = 60.0
BYPASS_RECOVERY_SECONDS = 30.0
_BYPASS_RECOVERY: dict[str, float] = {}


@contextmanager
def _bypass_slot(base: str, quick: bool):
    """Bound queue time even for callers without a scrape budget."""
    budget = current_budget()
    from app.services.mihomo import get_manager
    managed = get_manager().config["enabled"]
    if _BYPASS_RECOVERY.get(base, 0) > time.perf_counter():
        raise FetchCancelled("bypass 服务连接中断，恢复等待中")
    timeout = 2.0 if quick else BYPASS_MANAGED_QUEUE_TIMEOUT if managed else BYPASS_QUEUE_TIMEOUT
    deadline = time.perf_counter() + timeout
    if managed:
        with _BYPASS_QUEUE_LOCK:
            queue = _BYPASS_QUEUES.setdefault(base, BrowserQueue(2))
        with queue.acquire(timeout, budget):
            if _BYPASS_RECOVERY.get(base, 0) > time.perf_counter():
                raise FetchCancelled("bypass 服务连接中断，恢复等待中")
            try:
                yield
            except httpx.RemoteProtocolError:
                _BYPASS_RECOVERY[base] = time.perf_counter() + BYPASS_RECOVERY_SECONDS
                raise
        return
    slot = _BYPASS_LOCK
    while True:
        if budget:
            budget.check()
        if _BYPASS_RECOVERY.get(base, 0) > time.perf_counter():
            raise FetchCancelled("bypass 服务连接中断，恢复等待中")
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("bypass 排队超时，服务繁忙")
        if slot.acquire(timeout=min(0.05, remaining)):
            break
    try:
        if budget:
            budget.check()
        if _BYPASS_RECOVERY.get(base, 0) > time.perf_counter():
            raise FetchCancelled("bypass 服务连接中断，恢复等待中")
        yield
    except httpx.RemoteProtocolError:
        # Do not send the entire waiting queue into a restarting service.
        _BYPASS_RECOVERY[base] = time.perf_counter() + BYPASS_RECOVERY_SECONDS
        raise
    finally:
        slot.release()

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 站点普遍有频率限制，两次请求之间至少间隔这么久
MIN_INTERVAL_SECONDS = 1.0


def is_tls_error(exc: BaseException) -> bool:
    """识别 SSL 异常及 httpx/httpcore 包装后的 TLS 失败。"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLError) or "[SSL:" in str(exc).upper():
            return True
        exc = exc.__cause__ or exc.__context__
    return False


@dataclass
class CodeInfo:
    """抓取到的番号信息。字段与 database.models.Code 对齐。"""
    code: str = ""
    title: str = ""
    # 官方剧情简介。绝大多数站不给，目前只有 airav 的 JSON 接口有
    # （字段名 description）。写进 NFO 的 <plot>，AI 看点拼在它前面
    outline: str = ""
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
            "outline": self.outline,
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
        self.last_page = ''
        self.last_url = ''
        self.proxy = None
        self.source_key = ""

    def _remember(self, html: str, url: str) -> str:
        self.last_page, self.last_url = html, str(getattr(html, 'url', url))
        return html

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

        client = cls(
            source["host"],
            cookie=source.get("cookie", ""),
            interval=source.get("interval") or MIN_INTERVAL_SECONDS,
            bypass_first=source.get("bypass_first", False),
        )
        client.proxy = source.get('proxy') or None
        client.source_key = key
        return client

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
        cookie = clean_header_value(self.cookie)
        if cookie:
            base["Cookie"] = cookie
        base.update(extra or {})
        # 显式传空值表示"别发这个头"。theporndb 的 cookie 栏存的是 API Token，
        # 原样当 Cookie 发出去会让接口把请求当异常流量
        return {k: v for k, v in base.items() if v}

    def _throttle(self) -> None:
        state = self._state()
        lock, _ = state[0], state[1]
        budget = current_budget()
        with budget.acquire(lock) if budget else lock:
            elapsed = time.time() - state[1]
            wait = self.interval - elapsed
            if wait > 0:
                # 加一点随机抖动，避免请求节奏过于规整
                delay = wait + random.uniform(0, 0.3)
                budget.wait(delay) if budget else time.sleep(delay)
            state[1] = time.time()

    def get(self, path: str, params: dict | None = None, timeout: float = 15.0, bypass_first: bool | None = None, allow_bypass: bool = True, **kwargs) -> str:
        with source_context(self.source_key):
            page = self._get(path, params, timeout, bypass_first, allow_bypass, **kwargs)
            report_response(page)
            return page

    def _get(self, path: str, params: dict | None = None, timeout: float = 15.0, bypass_first: bool | None = None, allow_bypass: bool = True, **kwargs) -> str:
        """GET 请求，返回文本。失败返回空串。

        默认超时偏短：抓取由定时任务驱动，单站不通时应尽快让位给下一个站点。
        配置了 BYPASS_URL 时，直连遇到 403/503 会自动改走该服务重试一次；
        站点声明了 bypass_first 则跳过直连，直接走该服务。
        需要自定义鉴权头的 API 应设 allow_bypass=False：浏览器服务不支持传递该头。
        """
        settings = get_settings()
        # 补斜杠而不是直接拼：少一个斜杠拼出来的是 example.comsubs/x，
        # 域名整个变了，却只会报一个看着与 URL 无关的 DNS/SSL 错误
        url = path if path.startswith("http") else absolute_url(path, self.host)

        if current_budget() is not None:
            return self._get_budgeted(url, params, timeout, bypass_first, allow_bypass=allow_bypass, **kwargs)

        # 直连必被拦的站点，配了 bypass 就不必先撞一次 403。
        # Cookie 必须一并带过去：mgstage 的 adc、dmm 的 age_check_done
        # 都是年龄门，丢了的话过盾拿回来的永远是确认页
        if allow_bypass and (self.bypass_first if bypass_first is None else bypass_first) and settings.bypass_url:
            return self._remember(fetch_via_bypass(url, params, timeout, cookie=self.cookie), url)

        headers = self.headers(kwargs.pop("headers", None))

        for attempt in range(CONNECT_RETRIES + 1):
            self._throttle()
            request_proxy = None
            try:
                with proxy_lease(settings.proxy or None, self.proxy, self.source_key, url) as request_proxy, httpx.Client(
                    timeout=timeout,
                    follow_redirects=True,
                    proxy=request_proxy,
                    verify=False,
                    # 只认配置里的 PROXY，避免被宿主机环境变量里的代理劫持
                    trust_env=False,
                ) as client:
                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    _note_proxy_result(url, request_proxy, True)
                    return self._remember(FetchedPage(response.text, str(response.url), cookies=[
                        {'name':cookie.name,'value':cookie.value,'domain':cookie.domain} for cookie in getattr(getattr(response,'cookies',None),'jar',[])],
                        proxy=request_proxy), str(response.url))
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    _note_proxy_result(url, request_proxy, False)
                    if attempt < CONNECT_RETRIES:
                        continue
                # 421 需要重建连接，不能在同一次失败后立即判为无此作品。
                if status == 421:
                    if attempt < CONNECT_RETRIES:
                        continue
                    if allow_bypass and settings.bypass_url:
                        return self._remember(fetch_via_bypass(url, params, timeout, cookie=self.cookie), url)
                # 403/503 是典型的挑战页；502/504 多半是站点前面的防护层
                # 掐了直连。两类都值得让 bypass 服务再试一次
                if allow_bypass and (status in (403, 503) or status >= 500):
                    logger.info(f"{url} 返回 {status}，尝试通过 bypass 服务获取")
                    return self._remember(fetch_via_bypass(url, params, timeout, cookie=self.cookie), url)
                if not allow_bypass and (status == 429 or status >= 500) and attempt < CONNECT_RETRIES:
                    continue
                logger.warning(f"请求 {url} 失败: {status}")
                return FetchedPage("", url, status=status)
            except httpx.TimeoutException as exc:
                _note_proxy_result(url, request_proxy, False)
                # 超时不重试直连：站点本来就慢，再等一轮只会拖住后面的站点。
                # 但部分站点是对直连静默丢包而非返回挑战页，配了 bypass 就交给它
                if allow_bypass and settings.bypass_url:
                    logger.info(f"{url} 直连超时，尝试通过 bypass 服务获取")
                    return self._remember(fetch_via_bypass(url, params, timeout, cookie=self.cookie), url)
                logger.warning(f"请求 {url} 超时: {exc}")
                return ""
            except (httpx.TransportError, ssl.SSLError) as exc:
                _note_proxy_result(url, request_proxy, False)
                # SSL EOF、连接重置这类抖动重试一次多半就好，节流已保证不会打太密
                if attempt < CONNECT_RETRIES:
                    logger.debug(f"请求 {url} 连接异常，重试: {exc}")
                    continue
                if allow_bypass and settings.bypass_url and is_tls_error(exc):
                    logger.info(f"{url} TLS 重试失败，尝试通过 bypass 服务获取")
                    return self._remember(fetch_via_bypass(url, params, timeout, cookie=self.cookie), url)
                logger.warning(f"请求 {url} 异常: {exc}")
            except Exception as exc:
                logger.warning(f"请求 {url} 异常: {exc}")
                return ""
        return ""

    def _get_budgeted(self, url, params, timeout, bypass_first, allow_bypass=True, **kwargs):
        """Use one deadline for direct requests, retry waits and browser fallback."""
        budget = current_budget()
        settings = get_settings()
        headers = self.headers(kwargs.pop("headers", None))

        def bypass():
            budget.check()
            return self._remember(fetch_via_bypass(
                url, params, timeout, cookie=self.cookie,
            ), url)

        try:
            budget.check()
            if allow_bypass and (self.bypass_first if bypass_first is None else bypass_first) and settings.bypass_url:
                return bypass()
            for attempt in range(CONNECT_RETRIES + 1):
                self._throttle()
                retry_after = None
                request_proxy = None
                try:
                    with proxy_lease(settings.proxy or None, self.proxy, self.source_key, url) as request_proxy, http_client(
                        timeout=timeout, follow_redirects=True,
                        proxy=request_proxy,
                        verify=False, trust_env=False,
                    ) as client:
                        response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    _note_proxy_result(url, request_proxy, True)
                    return self._remember(response.text, str(response.url))
                except FetchCancelled:
                    return ""
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status == 429:
                        _note_proxy_result(url, request_proxy, False)
                    if status == 421 and attempt >= CONNECT_RETRIES:
                        return bypass() if allow_bypass and settings.bypass_url else ""
                    if allow_bypass and settings.bypass_url and (status == 403 or status >= 500):
                        return bypass()
                    if status not in (421, 429) and status < 500:
                        return FetchedPage("", url, status=status)
                    retry_after = exc.response.headers.get("Retry-After")
                except (httpx.TransportError, ssl.SSLError) as exc:
                    _note_proxy_result(url, request_proxy, False)
                    if attempt >= CONNECT_RETRIES and allow_bypass and settings.bypass_url and is_tls_error(exc):
                        logger.info(f"{url} TLS 重试失败，尝试通过 bypass 服务获取")
                        return bypass()
                if attempt >= CONNECT_RETRIES:
                    return ""
                delay = 0.5 * (2 ** attempt) + random.uniform(0, 0.2)
                if retry_after:
                    try:
                        delay = max(delay, min(5.0, max(0.0, float(retry_after))))
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = max(delay, min(5.0, max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())))
                        except (ValueError, TypeError, OverflowError):
                            pass
                # Leave room for useful I/O rather than consuming the entire
                # remaining window waiting to start an already doomed retry.
                if budget.remaining() <= delay + 0.05:
                    return ""
                budget.wait(delay)
        except FetchCancelled:
            pass
        except Exception as exc:
            logger.warning(f"请求 {url} 异常: {exc}")
        return ""


def _note_proxy_result(url, proxy, success):
    from app.services.mihomo import get_manager
    get_manager().note_result(url, proxy, success)


def _cookie_pairs(cookie: str) -> list[dict]:
    """把 "a=1; b=2" 形式的 Cookie 串拆成 FlareSolverr 的 cookies 数组。"""
    out = []
    for chunk in (cookie or "").split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name.strip():
            out.append({"name": name.strip(), "value": value.strip()})
    return out


class FetchedPage(str):
    def __new__(cls, text: str, url: str, status: int = 200, *, cookies=None, user_agent='', proxy=None):
        value = super().__new__(cls, text)
        value.url = url
        value.status = status
        value.cookies = cookies or []
        value.user_agent = user_agent
        value.proxy = proxy
        return value


def fetch_via_bypass(
    url: str,
    params: dict | None = None,
    timeout: float = 60.0,
    quick: bool = False,
    cookie: str = "",
) -> str:
    """通过用户自建的 bypass 服务抓取页面。

    兼容两类常见服务：
    - FlareSolverr 及其兼容实现（Byparr、TRAWL）：POST /v1，
      body 为 {cmd, url, maxTimeout}，返回 {status, solution: {response}}
    - cloudflare-bypass-for-scraping：GET /html?url=...

    通过 BYPASS_URL 配置服务地址；未配置时直接返回空串。
    quick=True 用更短的过盾上限，供连通性测试这类有人在等的场景。
    默认由 bypass 自行管理代理；启用内置 Mihomo 时显式传递其节点入口。
    全局及数据源配置中的外部代理不会传递。
    cookie 会转交给 FlareSolverr（mgstage 的年龄门就靠它）；
    GET /html 风格的服务没有传 Cookie 的口子，只能丢弃。
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
        budget = current_budget()
        if budget:
            solver_timeout = min(solver_timeout, max(1, int(budget.remaining() * 1000)))
            client_timeout = min(client_timeout, budget.remaining())
        with http_client(
            timeout=client_timeout, verify=False, trust_env=False
        ) as client:
            return _fetch_bypass_response(client, base, url, cookie, solver_timeout, quick=quick)
    except Exception as exc:
        if not isinstance(exc, FetchCancelled):
            reason = str(exc) or type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    error = exc.response.json()
                    reason = f"HTTP {exc.response.status_code}: {error.get('message') or error.get('msg') or reason}"
                except (ValueError, AttributeError):
                    pass
            logger.warning(f"bypass 抓取 {url.split('?')[0]} 失败: {type(exc).__name__}: {reason[:240]}")
        return ""


def _solver_request(client, endpoint, body, quick=False):
    from app.services.mihomo import get_manager
    manager = get_manager()
    automatic = manager.config["enabled"] and manager.config["automatic"]
    attempts = max(1, min(3, len(manager.nodes), max(1, body["maxTimeout"] // BYPASS_NODE_WINDOW_MS))) if automatic else 1
    deadline = time.perf_counter() + body["maxTimeout"] / 1000
    for attempt in range(max(1, attempts)):
        # Re-enter the FIFO queue for each exit attempt, so a slow site's
        # complete retry chain cannot monopolize a browser slot.
        with manager.lease(remote=True, url=body["url"], timeout=min(20, max(0, deadline-time.perf_counter()))) as proxy, _bypass_slot(endpoint, quick):
            node_name = manager.proxy_name(proxy)
            request = dict(body)
            if proxy:
                request["proxy"] = {"url": proxy}
            remaining = deadline - time.perf_counter()
            budget = current_budget()
            if budget:
                remaining = min(remaining, budget.remaining())
            if remaining <= 0:
                raise FetchCancelled("bypass 排队及节点重试达到总时限")
            remaining_attempts = min(attempts - attempt, max(1, int(remaining * 1000 // BYPASS_NODE_WINDOW_MS))) if automatic else 1
            request["maxTimeout"] = max(1, int(remaining * 1000 / remaining_attempts))
            # A service disconnection is not a failure of the target's exit.
            response = client.post(endpoint, json=request)
            if isinstance(getattr(response,'extensions',None),dict):
                response.extensions['cinefold_proxy'] = proxy
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            solution = payload.get("solution") or {}
            status = solution.get("status")
            message = str(payload.get("message") or payload.get("msg") or "")
            failed_exit = status in (403, 429) or isinstance(status, int) and status >= 500 or bool(
                re.search(r"timeout|deadline|blocked|challenge|http-403|http-429|proxy-connection|ECONNRESET", message, re.I))
            if failed_exit:
                manager.note_result(body["url"], proxy, False)
            elif response.is_success and payload.get("status") in ("ok", None):
                manager.note_result(body["url"], proxy, True)
        if automatic and failed_exit and attempt + 1 < attempts:
            logger.info(f"bypass 抓取 {body['url'].split('?')[0]} 节点「{node_name}」失败（本次上限 {request['maxTimeout'] / 1000:.1f}s），尝试其他节点")
            continue
        return response


def _fetch_bypass_response(client, base, url, cookie, solver_timeout, quick=False):
    if base.endswith("/v1") or _is_flaresolverr(base):
        endpoint = base if base.endswith("/v1") else f"{base}/v1"
        budget = current_budget()
        if budget:
            budget.check()
            solver_timeout = min(solver_timeout, max(1, int(budget.remaining() * 1000)))
        body: dict = {"cmd": "request.get", "url": url, "maxTimeout": solver_timeout}
        # Byparr 的 LinkRequest 没有 cookies 字段，多给不会报错（被忽略），
        # 代价只是它过不了 mgstage 那种靠 Cookie 的年龄门。
        pairs = _cookie_pairs(clean_header_value(cookie))
        if pairs:
            body["cookies"] = pairs
        response = _solver_request(client, endpoint, body, quick=quick)
        response.raise_for_status()
        payload = response.json()
        solution = payload.get("solution") or {}
        html = solution.get("response") or ""
        # Byparr 老版本可能漏掉 status；兼容缺失值，但拒绝明确的失败响应。
        if payload.get("status") not in ("ok", None):
            message = payload.get("message") or payload.get("msg") or ""
            logger.warning(f"bypass 抓取 {url.split('?')[0]} 返回失败: {str(message)[:120]}")
            return ""
        status = solution.get("status")
        if isinstance(status, int) and status >= 400:
            logger.warning(f"bypass 抓取 {url.split('?')[0]} 目标站返回 HTTP {status}")
            return FetchedPage("", url, status=status)
        # 浏览器访问 JSON API 时会包成 HTML 的 <pre>；还原给 JSON 解析器。
        if html.lstrip().startswith("<"):
            from pyquery import PyQuery
            import json
            try:
                text = PyQuery(html)("body > pre").text()
                data = json.loads(text)
                if isinstance(data, (dict, list)):
                    html = json.dumps(data, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return FetchedPage(html, solution.get('url') or url, cookies=solution.get('cookies'),
            user_agent=solution.get('userAgent') or '', proxy=getattr(response,'extensions',{}).get('cinefold_proxy'))

    # cloudflare-bypass-for-scraping 风格
    with _bypass_slot(base, quick):
        response = client.get(f"{base}/html", params={"url": url})
    response.raise_for_status()
    return FetchedPage(response.text, url)


def _is_flaresolverr(base: str) -> bool:
    """探测服务是否讲 FlareSolverr 协议（POST /v1），结果缓存避免重复请求。

    FlareSolverr / Byparr / TRAWL 三家都认 POST /v1，但根路径长得毫不相干：
    FlareSolverr 返回自报家门的 HTML，TRAWL 返回 {"msg": "TRAWL is ready!"}，
    Byparr 则 301 跳 /docs。按名字认必然漏，所以直接问 /v1 在不在——
    带一个空 url 打过去，返回什么内容不重要，只看端点是否存在。
    """
    if base in _BYPASS_KIND:
        return _BYPASS_KIND[base]

    is_flare = False
    try:
        # bypass 服务通常在内网，不能走系统代理
        with http_client(timeout=8, verify=False, trust_env=False) as client:
            endpoint = base if base.endswith("/v1") else f"{base}/v1"
            response = client.post(endpoint, json={"cmd": "request.get", "url": ""})
            # 404/405 说明没有这个端点，其余（含参数校验失败的 4xx、
            # 解不开挑战的 500）都证明端点确实在
            is_flare = response.status_code not in (404, 405)
    except FetchCancelled:
        raise
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


def absolute_url(url: str, host: str) -> str:
    """相对地址转绝对地址。

    host 必须传配置里的（client.host），不能用模块常量：这些站换域名很
    频繁，用户在页面上改了地址后，用常量拼出来的图片链接全指向死域名。

    不带前导斜杠的相对路径（subs/1407/x.html）同样要补全 —— 原样返回的话
    调用方拼出来的是 host+path 连在一起的 subtitlecat.comsubs/...，
    域名都变了，报出来却是一个看着与 URL 无关的 SSL 错误。
    """
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{host.rstrip('/')}{url}"
    # 已经是绝对地址（含 data: 这类 scheme）就别动
    if "://" in url or url.startswith("data:"):
        return url
    return f"{host.rstrip('/')}/{url}"


# 各式日期文本：2021-02-19 / 2021/2/19 / 2021年2月19日
_ANY_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def normalize_date(text: str) -> str:
    """从任意文本里取日期并统一成 YYYY-MM-DD。取不出返回空串。"""
    match = _ANY_DATE_RE.search(text or "")
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def text_contains_code(text: str, code: str) -> bool:
    """文本（标题等）里是否含目标番号，按词比对、写法不敏感。

    不能用整串拍平后的子串包含：MD-0180 会命中 "MD-0180-1 下集"，
    "MDX-0123 1080P" 拍平后又会因 1080 贴在番号后面而漏判。
    按空白切词后逐词拍平，词内要求整词相等、或番号后紧跟非数字
    （-C 这类版本后缀算同一部片，-1 这类分集不算）。
    """
    target = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if not target or not text:
        return False
    for token in re.split(r"\s+", text):
        flat = re.sub(r"[^A-Za-z0-9]", "", token).upper()
        if flat == target:
            return True
        if flat.startswith(target) and not flat[len(target):][:1].isdigit():
            return True
    return False
