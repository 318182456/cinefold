"""Read nodes/providers only; subscription policy, groups and DNS are discarded."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
import yaml

MAX_NODES = 1024


class SubscriptionLoader:
    def __init__(self, client, cache=None):
        self.client = client
        self.cache = cache or {}
        self.next_cache = {}
        self.reports = []
        self.deadline = time.monotonic() + 180
        self.active = set()
        self.fetched = {}
        self.provider_count = 0

    def fetch(self, url, headers=None):
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("订阅必须使用 HTTP/HTTPS 地址")
        key = (url, json.dumps(headers or {}, sort_keys=True))
        if key in self.fetched:
            return self.fetched[key]
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("子订阅拉取达到总时限")
            try:
                with self.client.stream("GET", url, headers=headers, timeout=min(20, remaining)) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 5 * 1024 * 1024 or time.monotonic() > self.deadline:
                            raise ValueError("订阅超过大小或时间上限")
                break
            except httpx.TransportError:
                if attempt:
                    raise
        try:
            parsed = yaml.safe_load(bytes(data))
        except yaml.YAMLError:
            raise ValueError("订阅无法解析，需要 Clash/Mihomo YAML 格式") from None
        if not isinstance(parsed, dict):
            raise ValueError("订阅需要包含 proxies 或 proxy-providers 的 YAML")
        self.fetched[key] = parsed
        return parsed

    def load(self, url):
        nodes = self.visit(self.fetch(url), url, "", 0)
        if not nodes:
            raise ValueError("订阅及子订阅没有可导入节点")
        if len(nodes) > MAX_NODES:
            raise ValueError(f"合并后的节点超过 {MAX_NODES} 个，请先在订阅侧筛选")
        names = set()
        for node in nodes:
            if node["name"] in names:
                raise ValueError("合并后的节点名称重复，请为订阅节点设置不同名称")
            names.add(node["name"])
        return nodes

    def visit(self, document, url, label, depth):
        if depth > 3 or url in self.active:
            raise ValueError("子订阅存在循环引用或嵌套过深")
        self.active.add(url)
        try:
            nodes = self.normalize(document.get("proxies") or [], label)
            if nodes and not label:
                self.reports.append({"name": label or "主订阅", "status": "ok", "count": len(nodes)})
            providers = document.get("proxy-providers") or {}
            if not isinstance(providers, dict):
                raise ValueError("proxy-providers 格式无效")
            for name, provider in providers.items():
                self.provider_count += 1
                if self.provider_count > 32:
                    raise ValueError("子订阅数量超过 32 个")
                provider_label = f"{label} / {name}" if label else str(name)
                if not isinstance(provider, dict):
                    raise ValueError("子订阅配置格式无效")
                child_url = urljoin(url, provider.get("url") or "")
                cache_key = hashlib.sha256(f"{url}|{provider_label}|{child_url}".encode()).hexdigest()
                try:
                    if provider.get("type") == "inline":
                        child_nodes = self.normalize(provider.get("payload") or [], provider_label)
                    elif provider.get("url"):
                        headers = {str(key): ",".join(map(str, value)) if isinstance(value, list) else str(value)
                                   for key, value in (provider.get("header") or {}).items()}
                        child_nodes = self.visit(self.fetch(child_url, headers), child_url, provider_label, depth + 1)
                    else:
                        raise ValueError("子订阅没有可联网读取的 URL 或内联节点")
                    child_nodes = self.overrides(child_nodes, provider)
                    self.next_cache[cache_key] = child_nodes
                    self.reports.append({"name": provider_label, "status": "ok", "count": len(child_nodes)})
                except (httpx.HTTPError, ValueError, TypeError, re.error):
                    child_nodes = self.cache.get(cache_key, [])
                    if child_nodes:
                        self.next_cache[cache_key] = child_nodes
                    self.reports.append({"name": provider_label, "status": "cached" if child_nodes else "failed",
                                         "count": len(child_nodes)})
                nodes.extend(child_nodes)
                if len(nodes) > MAX_NODES:
                    raise ValueError(f"合并后的节点超过 {MAX_NODES} 个")
            return nodes
        finally:
            self.active.remove(url)

    @staticmethod
    def normalize(raw, label):
        if not isinstance(raw, list):
            raise ValueError("proxies 必须是节点列表")
        result, names = [], {}
        for node in raw:
            if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not isinstance(node.get("type"), str):
                raise ValueError("订阅节点缺少 name/type")
            name = node["name"]
            if name in names or name in ("DIRECT", "REJECT"):
                raise ValueError("订阅包含重复或保留的节点名称")
            names[name] = f"{label} / {name}" if label else name
        for node in raw:
            copy = {**node, "name": names[node["name"]]}
            if copy.get("dialer-proxy") in names:
                copy["dialer-proxy"] = names[copy["dialer-proxy"]]
            result.append(copy)
        return result

    @staticmethod
    def overrides(nodes, provider):
        include, exclude = provider.get("filter"), provider.get("exclude-filter")
        excluded_types = str(provider.get("exclude-type") or "").split("|")
        result = [dict(node) for node in nodes if (not include or re.search(include, node["name"]))
                  and (not exclude or not re.search(exclude, node["name"])) and node["type"] not in excluded_types]
        override = dict(provider.get("override") or {})
        prefix, suffix = str(override.pop("additional-prefix", "")), str(override.pop("additional-suffix", ""))
        # Expression-based rewrites cannot be safely approximated by Python.
        if "override-expr" in override or "proxy-name" in override:
            raise ValueError("子订阅使用了暂不支持的名称重写表达式")
        mapping = {node["name"]: f"{prefix}{node['name']}{suffix}" for node in result}
        for node in result:
            old_name = node["name"]
            node.update(override)
            node["name"] = mapping[old_name]
            if node.get("dialer-proxy") in mapping:
                node["dialer-proxy"] = mapping[node["dialer-proxy"]]
        return result
