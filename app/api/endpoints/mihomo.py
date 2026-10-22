"""Authenticated management of the in-container proxy core."""
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.endpoints import get_current_user
from app.schemas.reponse import ResponseEntity
from app.services.mihomo import get_manager

router = APIRouter(prefix="/mihomo", tags=["mihomo"], dependencies=[Depends(get_current_user)])


class ProxyConfig(BaseModel):
    enabled: bool = False
    subscription: str = Field(default="", max_length=8192)
    selected: str = Field(default="", max_length=256)
    trawl_host: str = Field(default="cinefold", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,252}$")
    source_nodes: dict[str, str] = Field(default_factory=dict, max_length=128)
    automatic: bool = True
    groups: dict[str, list[str]] = Field(default_factory=dict, max_length=32)
    source_groups: dict[str, str] = Field(default_factory=dict, max_length=128)


class ProxyTest(BaseModel):
    url: str = "https://www.example.com/"
    via_trawl: bool = False
    source_key: str = ""


@router.get("")
def status():
    return ResponseEntity.ok(get_manager().status())


@router.post("/config")
def configure(body: ProxyConfig):
    manager = get_manager()
    if body.enabled and manager.nodes and not body.selected:
        return ResponseEntity.fail("请选择使用节点", code=400)
    if body.selected and body.selected not in {node["name"] for node in manager.nodes}:
        return ResponseEntity.fail("选择的节点不存在，请更新订阅后重新选择", code=400)
    if body.subscription and body.subscription != "********" and urlparse(body.subscription).scheme not in ("http", "https"):
        return ResponseEntity.fail("订阅必须是 HTTP/HTTPS 地址", code=400)
    from app.modules.ladysite.sources import get_source
    from app.modules.ladysite.brands import BRANDS

    def source_info(key):
        if key.startswith("brand:"):
            host = BRANDS.get(key[6:])
            return {"host": host} if host else None
        return get_source(key)
    updates = body.model_dump()
    updates["source_nodes"] = {key: node for key, node in body.source_nodes.items() if node}
    updates["source_hosts"] = {}
    names = {node["name"] for node in manager.nodes}
    from app.services.mihomogroups import build_groups
    available_groups = build_groups(manager.nodes, body.groups)
    for name, members in body.groups.items():
        if not name.strip() or len(name) > 60 or not members or not set(members) <= names:
            return ResponseEntity.fail("自建代理组名称或节点无效，每组至少选择一个有效节点", code=400)
    updates["source_groups"] = {key: value for key, value in body.source_groups.items() if value}
    for key, group in updates["source_groups"].items():
        source = source_info(key)
        if group not in available_groups or not source:
            return ResponseEntity.fail("数据源或自建代理组不存在", code=400)
        host = urlparse(source.get("host", "")).hostname
        if host:
            updates["source_hosts"][key] = host
    for key, node in updates["source_nodes"].items():
        source = source_info(key)
        if not source or node not in names:
            return ResponseEntity.fail("数据源或指定的代理节点不存在，请重新选择", code=400)
        host = urlparse(source.get("host", "")).hostname
        if host:
            updates["source_hosts"][key] = host
    try:
        manager.submit("apply", updates)
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=409)
    return ResponseEntity.ok(message="已开始应用内置代理配置")


@router.post("/action/{action}")
def action(action: str):
    if action not in {"check", "install", "refresh"}:
        return ResponseEntity.fail("未知操作", code=400)
    try:
        get_manager().submit(action)
    except ValueError as exc:
        return ResponseEntity.fail(str(exc), code=409)
    return ResponseEntity.ok(message="任务已启动")


@router.post("/test")
def test_proxy(body: ProxyTest):
    if urlparse(body.url).scheme not in ("http", "https"):
        return ResponseEntity.fail("测试地址必须是 HTTP/HTTPS 地址", code=400)
    manager = get_manager()
    if not manager.status()["running"] or not manager.config["enabled"]:
        return ResponseEntity.fail("请先启用内置代理并等待内核就绪", code=400)
    try:
        if body.via_trawl:
            from app.modules.ladysite.base import fetch_via_bypass
            from app.services.mihomo import source_context
            with source_context(body.source_key):
                page = fetch_via_bypass(body.url, quick=True)
            if not page:
                return ResponseEntity.fail("TRAWL 测试失败，请检查日志、容器网络与出口节点", code=400)
        else:
            with manager.lease(source_key=body.source_key, url=body.url) as proxy, httpx.Client(proxy=proxy, timeout=20, follow_redirects=True, trust_env=False) as client:
                response = client.get(body.url)
                response.raise_for_status()
        return ResponseEntity.ok({"success": True, "message": "目标站连接成功"})
    except (httpx.HTTPError, RuntimeError, TimeoutError):
        return ResponseEntity.fail("目标站连接失败，请检查节点或切换测试地址", code=400)
