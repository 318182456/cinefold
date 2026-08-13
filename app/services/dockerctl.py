"""Docker Engine API 的最小客户端，只做「重启容器」这一件事。

不引 docker SDK：需要的接口只有两个（inspect + restart），而 SDK 会连带
拉进一堆依赖。httpx 本来就在依赖里，且原生支持 unix socket 传输。

两种连法：
- unix:///var/run/docker.sock —— 需要把 sock 挂进容器（默认）
- tcp://host:2375 —— qb 在另一台机器上时用，注意 2375 是明文无鉴权端口，
  只能在可信内网开，且最好用防火墙限定来源 IP

容器名与容器 ID 都能用：Docker API 的 /containers/<id> 路径同时接受两者。
"""
from __future__ import annotations

from urllib.parse import quote

import httpx
from loguru import logger

from app.core.config import get_settings

# Docker API 版本前缀。1.41 对应 Docker 20.10，2020 年底的版本，
# 写死一个足够老的版本号，新 daemon 都向后兼容
API_VERSION = "v1.41"

# restart 会等容器停干净再拉起，qb 有存量种子时 stop 本身要几秒，
# 超时给宽一点。连接超时保持短 —— sock 不通应当立刻失败而不是干等
TIMEOUT = httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0)


def _unreachable_hint(host: str) -> str:
    """连不上时该检查什么。两种连法的排查方向完全不同。"""
    if host.startswith("unix://"):
        return "请确认已把 /var/run/docker.sock 挂进容器（不能加 :ro）"
    return "请确认目标机器已开启 Docker TCP 端口且网络可达"


def _client_for(host: str) -> tuple[httpx.Client, str]:
    """按 host 造一个 httpx.Client，返回 (client, base_url)。

    unix socket 走 transport 的 uds 参数，URL 里的主机名随便填一个
    （httpx 要求 URL 合法，但走 uds 时主机名不参与实际连接）。

    一律 trust_env=False：Docker daemon 只在本机 socket 或内网，配了
    HTTP_PROXY 之后 httpx 会把这些请求也往代理送，得到的是代理返回的
    502，而不是真实的连接错误 —— 报错长得像「Docker 有问题」，实际是
    代理转不过去，极难往代理上想。
    """
    host = (host or "").strip()
    if host.startswith("unix://"):
        path = host[len("unix://"):]
        transport = httpx.HTTPTransport(uds=path)
        return (
            httpx.Client(transport=transport, timeout=TIMEOUT, trust_env=False),
            "http://localhost",
        )

    # tcp:// 是 Docker 的写法，httpx 只认 http/https
    if host.startswith("tcp://"):
        host = "http://" + host[len("tcp://"):]
    elif not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return httpx.Client(timeout=TIMEOUT, trust_env=False), host.rstrip("/")


def restart_container(container: str = "", host: str = "") -> tuple[bool, str]:
    """重启容器。返回 (成功, 说明)。

    先 inspect 一次确认容器存在并拿到当前状态，再发 restart —— 直接 restart
    时容器名写错只会得到一个 404，分不清是「名字错了」还是「daemon 不通」，
    排查起来很费劲。
    """
    settings = get_settings()
    container = (container or settings.docker_container_qbittorrent or "").strip()
    host = (host or settings.docker_host or "").strip()

    if not container:
        return False, "未配置 qBittorrent 的容器名"
    if not host:
        return False, "未配置 Docker 地址"

    name = quote(container, safe="")
    try:
        client, base = _client_for(host)
    except Exception as exc:
        return False, f"Docker 地址 {host} 无法解析: {exc}"

    with client:
        try:
            resp = client.get(f"{base}/{API_VERSION}/containers/{name}/json")
        except Exception as exc:
            # 不只捕 httpx.HTTPError：uds 在 Windows 上不被支持，抛的是
            # 普通 RuntimeError，漏掉的话会变成一句没头没尾的堆栈
            return False, f"连不上 Docker（{host}）: {exc}。{_unreachable_hint(host)}"

        if resp.status_code == 404:
            return False, f"Docker 里没有名为 {container} 的容器"
        if resp.status_code >= 400:
            return False, f"读取容器 {container} 失败: HTTP {resp.status_code} {resp.text[:200]}"

        try:
            state = (resp.json().get("State") or {}).get("Status", "")
        except Exception:
            state = ""

        try:
            resp = client.post(f"{base}/{API_VERSION}/containers/{name}/restart")
        except Exception as exc:
            return False, f"重启容器 {container} 请求失败: {exc}"

        # 204 = 重启完成；304 = 已在重启中，也算达到目的
        if resp.status_code in (204, 304):
            return True, f"已重启容器 {container}（重启前状态 {state or '未知'}）"
        return False, f"重启容器 {container} 失败: HTTP {resp.status_code} {resp.text[:200]}"


def test_connection() -> tuple[bool, str]:
    """供配置页「测试连接」使用。只探测 daemon 与容器是否可见，不真的重启。"""
    settings = get_settings()
    host = (settings.docker_host or "").strip()
    container = (settings.docker_container_qbittorrent or "").strip()
    if not host:
        return False, "未配置 Docker 地址"

    try:
        client, base = _client_for(host)
    except Exception as exc:
        return False, f"Docker 地址 {host} 无法解析: {exc}"

    with client:
        try:
            resp = client.get(f"{base}/{API_VERSION}/version")
            resp.raise_for_status()
            version = (resp.json() or {}).get("Version", "")
        except Exception as exc:
            return False, f"连不上 Docker（{host}）：{exc}。{_unreachable_hint(host)}"

        if not container:
            return True, f"Docker {version} 已连通，但未填容器名"

        name = quote(container, safe="")
        try:
            resp = client.get(f"{base}/{API_VERSION}/containers/{name}/json")
        except Exception as exc:
            return False, f"读取容器 {container} 失败: {exc}"

        if resp.status_code == 404:
            return False, f"Docker {version} 已连通，但没有名为 {container} 的容器"
        if resp.status_code >= 400:
            return False, f"读取容器 {container} 失败: HTTP {resp.status_code}"

        try:
            state = (resp.json().get("State") or {}).get("Status", "")
        except Exception:
            state = ""
        return True, f"Docker {version} 已连通，容器 {container} 当前状态 {state or '未知'}"


def restart_qbittorrent() -> tuple[bool, str]:
    """重启 qBittorrent 容器。日志里记一笔，这是个有副作用的动作。"""
    ok, message = restart_container()
    if ok:
        logger.info(f"[qb 自愈] {message}")
    else:
        logger.error(f"[qb 自愈] 重启失败: {message}")
    return ok, message
