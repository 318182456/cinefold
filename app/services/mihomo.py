"""Managed Mihomo child process, persistent nodes and verified online updates.

Only node definitions are imported from subscriptions. Each node gets its own
authenticated HTTP listener so concurrent TRAWL requests cannot switch exits.
The controller stays on loopback; nothing needs another container.
"""
from __future__ import annotations

import atexit
import gzip
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from loguru import logger
from app.services.mihomosubscription import MAX_NODES, SubscriptionLoader
from app.services.mihomogroups import JAPAN_SOURCES, build_groups, default_group

FIRST_PORT = 17890
CONTROLLER = "http://127.0.0.1:19090"
RELEASE_ROOT = "https://github.com/MetaCubeX/mihomo/releases"
MAX_DOWNLOAD = 100 * 1024 * 1024
_source_key = ContextVar("mihomo_source_key", default="")


@contextmanager
def source_context(key):
    token = _source_key.set(key)
    try:
        yield
    finally:
        _source_key.reset(token)


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _read(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _error(exc):
    # HTTP exceptions include full subscription URLs (often bearer credentials).
    if isinstance(exc, httpx.HTTPStatusError):
        return f"远端返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"网络请求失败（{type(exc).__name__}）"
    if isinstance(exc, (ValueError, RuntimeError)):
        return str(exc)[:200]
    return f"操作失败（{type(exc).__name__}）"


def release_asset(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "amd64-compatible", "amd64": "amd64-compatible",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "windows", "darwin") or not arch:
        raise ValueError("内核在线安装暂支持 Linux、Windows、macOS 的 amd64/arm64")
    return f"mihomo-{system}-{arch}", ".zip" if system == "windows" else ".gz"


def discover_release(client):
    # The official HTML contains SHA256 digests too, and avoids GitHub API's
    # very small unauthenticated rate limit shared by many NAS deployments.
    response = client.get(f"{RELEASE_ROOT}/latest")
    response.raise_for_status()
    tag = str(response.url).rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("无法识别官方稳定版版本号")
    prefix, extension = release_asset()
    name = f"{prefix}-{tag}{extension}"
    response = client.get(f"{RELEASE_ROOT}/expanded_assets/{tag}")
    response.raise_for_status()
    from pyquery import PyQuery
    for item in PyQuery(response.text)("clipboard-copy").items():
        if item.attr("aria-label") == f"Copy to clipboard digest for {name}":
            digest = item.attr("value") or ""
            if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
                return {"version": tag, "sha256": digest[7:].lower(), "name": name,
                        "url": f"{RELEASE_ROOT}/download/{tag}/{name}"}
    raise ValueError("官方发布页没有此平台内核的 SHA256，未开始安装")


def traffic_update(method):
    @wraps(method)
    def apply(self, *args, **kwargs):
        with self.pause_traffic():
            return method(self, *args, **kwargs)
    return apply


class Mihomo:
    def __init__(self, root: Path):
        self.root = root
        self.binary = root / ("mihomo.exe" if os.name == "nt" else "mihomo")
        self.config = {"enabled": False, "subscription": "", "selected": "",
                       "trawl_host": "cinefold", "source_nodes": {}, "source_hosts": {},
                       "automatic": True,
                       "groups": {}, "source_groups": {},
                       **_read(root / "settings.json", {})}
        self.health = {}
        self.winners = {}
        self.credentials = _read(root / "credentials.json", {})
        self.nodes = _read(root / "nodes.json", [])
        self.providers = _read(root / "providers.json", [])
        self.latest = None
        self.process = None
        self.version = _read(root / "installed.json", {}).get("version", "")
        self.state = _read(root / "task.json", {})
        if self.state.get("running"):
            self.state.update(running=False, stage="interrupted", message="上次操作被中断，请重试")
        self.lock = threading.RLock()
        self.allocated = set()
        self.recent_requests = {}
        self.available = threading.Condition(self.lock)
        self.draining = False
        self.operation = threading.Lock()
        self.stopping = threading.Event()
        self.monitor = None
        # A crash between binary replacement and health verification must
        # restore the previous executable before any automatic start.
        marker, backup = root / "installing.json", root / "previous"
        if marker.exists():
            if backup.exists():
                shutil.copy2(backup, self.binary)
            else:
                self.binary.unlink(missing_ok=True)
            old = _read(marker, {}).get("version", "")
            self.version = old
            _write(root / "installed.json", {"version": old})
            marker.unlink()
            self.state = {"running": False, "stage": "interrupted", "message": "内核更新中断，已恢复原状态"}

    def _credentials(self):
        if not self.credentials:
            self.credentials = {"password": secrets.token_urlsafe(32), "secret": secrets.token_urlsafe(32)}
            _write(self.root / "credentials.json", self.credentials)
        return self.credentials

    def _progress(self, stage, message, percent=0, running=True, **extra):
        with self.lock:
            self.state = {"running": running, "stage": stage, "message": message,
                          "percent": percent, "updated_at": time.time(), **extra}
            _write(self.root / "task.json", self.state)

    def status(self):
        with self.lock:
            groups = build_groups(self.nodes, self.config["groups"])
            from app.modules.ladysite.brands import BRANDS, BRAND_LABELS
            brands = [{"key": f"brand:{key}", "name": f"厂牌 {BRAND_LABELS.get(key, key)}", "host": host} for key, host in BRANDS.items()]
            return {"enabled": self.config["enabled"], "subscription": "********" if self.config["subscription"] else "",
                    "selected": self.config["selected"], "trawl_host": self.config["trawl_host"],
                    "source_nodes": dict(self.config["source_nodes"]),
                    "groups": dict(self.config["groups"]), "source_groups": dict(self.config["source_groups"]),
                    "default_groups": build_groups(self.nodes),
                    "source_defaults": {key: default_group(key, groups) for key in (*JAPAN_SOURCES, *(item['key'] for item in brands))},
                    "brand_sources": brands,
                    "providers": list(self.providers), "active_requests": len(self.allocated),
                    "automatic": self.config["automatic"],
                    "routes": [{"host": key, "node": value[0]} for key, value in self.winners.items()
                               if time.monotonic() - value[1] < 600],
                    "installed": self.binary.exists(), "version": self.version,
                    "running": self.process is not None and self.process.poll() is None,
                    "nodes": [{"name": node["name"], "type": node["type"]} for node in self.nodes],
                    "latest": self.latest and self.latest["version"], "task": dict(self.state)}

    def _client(self):
        from app.core.config import get_settings
        # Bootstrap and updates must not depend on the kernel being installed.
        proxy = get_settings().proxy or None
        if not proxy and self.config["enabled"] and self.status()["running"]:
            proxy = self.proxy()
        return httpx.Client(timeout=httpx.Timeout(20, read=60), follow_redirects=True,
                            trust_env=False, proxy=proxy,
                            headers={"User-Agent": "cinefold-mihomo"})

    def _runtime(self, nodes=None):
        nodes = self.nodes if nodes is None else nodes
        credentials = self._credentials()
        names = {node["name"] for node in nodes}
        groups = [{"name": "cinefold-group-" + hashlib.sha256(name.encode()).hexdigest()[:12],
                   "type": "select", "proxies": [member for member in members if member in names]}
                  for name, members in build_groups(nodes, self.config["groups"]).items()]
        groups = [group for group in groups if group["proxies"]]
        return {"mode": "rule", "log-level": "warning", "ipv6": False,
                "external-controller": "127.0.0.1:19090", "secret": credentials["secret"],
                "allow-lan": True, "bind-address": "*", "geoip-maxmind": False,
                "profile": {"store-selected": False},
                "proxies": nodes, "proxy-groups": groups, "rules": ["MATCH,REJECT"],
                "listeners": [{"name": f"cinefold-{index}", "type": "http", "listen": "0.0.0.0",
                               "port": FIRST_PORT + index, "proxy": node["name"],
                               "users": [{"username": "cinefold", "password": credentials["password"]}]}
                              for index, node in enumerate(nodes)]}

    def _validate(self, binary=None, nodes=None):
        candidate = self.root / "check.json"
        _write(candidate, self._runtime(nodes))
        try:
            result = subprocess.run([str(binary or self.binary), "-t", "-d", str(self.root), "-f", str(candidate)],
                                    cwd=self.root, capture_output=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ValueError("内核配置校验失败，请检查订阅节点格式或节点间的依赖")
        finally:
            candidate.unlink(missing_ok=True)

    def _start(self):
        if self.stopping.is_set():
            raise RuntimeError("应用正在退出，已取消内核启动")
        if not self.binary.exists():
            raise ValueError("请先在线安装 Mihomo 内核")
        if not self.nodes:
            raise ValueError("请先填写订阅地址并更新订阅（需 Clash/Mihomo YAML 格式）")
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate()
        _write(self.root / "config.json", self._runtime())
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            command = [str(self.binary), "-d", str(self.root), "-f", str(self.root / "config.json")]
            if sys.platform.startswith("linux"):
                command = [sys.executable, str(Path(__file__).with_name("mihomorunner.py")), str(os.getpid()), *command]
            self.process = subprocess.Popen(command,
                                            cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Mihomo 启动失败，请检查代理端口是否已被占用")
            try:
                with httpx.Client(timeout=0.5, trust_env=False) as client:
                    response = client.get(f"{CONTROLLER}/version", headers={"Authorization": f"Bearer {self._credentials()['secret']}"})
                    if response.status_code == 200:
                        self.version = response.json().get("version", self.version)
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self._stop()
        raise RuntimeError("Mihomo 启动后未就绪")

    def _stop(self):
        with self.lock:
            process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _subscription(self):
        url = self.config["subscription"]
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("请填写有效的 HTTP/HTTPS Clash 订阅地址")
        with self._client() as client:
            loader = SubscriptionLoader(client, _read(self.root / "provider-cache.json", {}))
            nodes = loader.load(url)
        # Validate before replacing the working subscription or stopping traffic.
        if self.binary.exists():
            self._validate(nodes=nodes)
        self._activate_nodes(nodes)
        self.providers = loader.reports
        _write(self.root / "providers.json", self.providers)
        _write(self.root / "provider-cache.json", loader.next_cache)
        with self.lock:
            self.health.clear()
            self.winners.clear()

    @traffic_update
    def _activate_nodes(self, nodes):
        names = {node["name"] for node in nodes}
        previous_nodes, previous_selected = self.nodes, self.config["selected"]
        was_running = self.status()["running"]
        self._stop()
        self.nodes = nodes
        if self.config["selected"] not in names:
            self.config["selected"] = next((node["name"] for node in nodes if node["type"] not in ("direct", "reject")), nodes[0]["name"])
        try:
            if self.config["enabled"] and self.binary.exists():
                self._start()
        except Exception:
            self._stop()
            self.nodes, self.config["selected"] = previous_nodes, previous_selected
            if was_running:
                self._start()
            raise
        _write(self.root / "nodes.json", nodes)
        _write(self.root / "settings.json", self.config)

    def _install(self):
        self._progress("check", "检查官方稳定版", 5)
        with self._client() as client:
            release = discover_release(client)
            self.latest = release
            archive = self.root / ("download" + release["name"][release["name"].rfind("."):])
            candidate = self.root / ("candidate.exe" if os.name == "nt" else "candidate")
            try:
                self._progress("download", f"下载 {release['version']}", 10)
                digest, size = hashlib.sha256(), 0
                with client.stream("GET", release["url"]) as response, archive.open("wb") as output:
                    response.raise_for_status()
                    reported = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise ValueError("内核下载超过 100 MB 上限")
                        digest.update(chunk)
                        output.write(chunk)
                        if size - reported >= 1024 * 1024:
                            self._progress("download", f"下载 {release['version']}", 10, downloaded_bytes=size)
                            reported = size
                self._progress("verify", "校验官方 SHA256", 65)
                if digest.hexdigest() != release["sha256"]:
                    raise ValueError("内核 SHA256 不匹配，已取消安装")
                if archive.suffix == ".zip":
                    with zipfile.ZipFile(archive) as package:
                        entries = [item for item in package.infolist()
                                   if Path(item.filename).name.lower().startswith("mihomo")
                                   and Path(item.filename).suffix.lower() == ".exe"]
                        if len(entries) != 1 or entries[0].file_size > MAX_DOWNLOAD:
                            raise ValueError("内核压缩包内容无效")
                        with package.open(entries[0]) as source, candidate.open("wb") as output:
                            shutil.copyfileobj(source, output)
                else:
                    with gzip.open(archive, "rb") as source, candidate.open("wb") as output:
                        total = 0
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_DOWNLOAD:
                                raise ValueError("内核解压超过 100 MB 上限")
                            output.write(chunk)
                candidate.chmod(0o755)
                check = subprocess.run([str(candidate), "-v"], capture_output=True, timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if check.returncode or "mihomo" not in check.stdout.decode(errors="replace").lower():
                    raise ValueError("下载的内核无法在当前平台运行")
                self._validate(binary=candidate)
                self._progress("install", "安装内核，失败时自动恢复旧版", 85)
                self._replace(candidate, release["version"])
            finally:
                archive.unlink(missing_ok=True)
                candidate.unlink(missing_ok=True)

    @traffic_update
    def _replace(self, candidate, version):
        backup, marker = self.root / "previous", self.root / "installing.json"
        old_version = self.version
        self._stop()
        if self.binary.exists():
            shutil.copy2(self.binary, backup)
        else:
            backup.unlink(missing_ok=True)
        _write(marker, {"version": old_version})
        try:
            candidate.replace(self.binary)
            self.version = version
            if self.config["enabled"] and self.nodes:
                self._start()
            _write(self.root / "installed.json", {"version": self.version})
        except Exception:
            self._stop()
            if backup.exists():
                shutil.copy2(backup, self.binary)
            else:
                self.binary.unlink(missing_ok=True)
            self.version = old_version
            if self.config["enabled"] and self.nodes and self.binary.exists():
                self._start()
            raise
        finally:
            marker.unlink(missing_ok=True)

    def submit(self, action, updates=None):
        if not self.operation.acquire(blocking=False):
            raise ValueError("Mihomo 正在执行操作，请稍后重试")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._progress(action, "正在处理", 0)
        except BaseException:
            self.operation.release()
            raise

        def work():
            try:
                subscription_changed = False
                if updates is not None:
                    config = {**self.config, **updates}
                    if config.get("subscription") == "********":
                        config["subscription"] = self.config["subscription"]
                    subscription_changed = config["subscription"] != self.config["subscription"]
                    with self.lock:
                        self.config = config
                        self.health.clear()
                        self.winners.clear()
                    _write(self.root / "settings.json", self.config)
                if action == "check":
                    with self._client() as client:
                        self.latest = discover_release(client)
                elif action == "install":
                    self._install()
                elif action == "refresh":
                    self._subscription()
                elif action == "apply":
                    if self.config["enabled"]:
                        if not self.binary.exists():
                            self._install()
                        if not self.nodes or subscription_changed:
                            self._subscription()
                        self._start()
                    else:
                        self._stop()
                warnings = [report for report in self.providers if report["status"] != "ok"]
                if action in ("refresh", "apply") and warnings:
                    self._progress("partial", f"已保留可用节点，{len(warnings)} 个子订阅读取失败或使用缓存，请检查子订阅明细", 100, False)
                else:
                    self._progress("done", "操作完成", 100, False)
            except Exception as exc:
                message = _error(exc)
                logger.warning(f"[Mihomo] {message}")
                self._progress("failed", message, 0, False)
            finally:
                self.operation.release()

        threading.Thread(target=work, name="mihomo-operation", daemon=True).start()

    def _source(self, source_key, url):
        source_key = source_key or _source_key.get()
        host = urlparse(url).hostname or "default"
        if not source_key:
            for key, source_host in sorted(self.config["source_hosts"].items(), key=lambda item: len(item[1]), reverse=True):
                if host == source_host or host.endswith("." + source_host):
                    source_key = key
                    break
        return source_key, host

    def proxy(self, remote=False, source_key="", url="", exclude=()):
        with self.lock:
            if not self.config["enabled"]:
                return None
            bindings = self.config["source_nodes"]
            source_key, route = self._source(source_key, url)
            selected = bindings.get(source_key) or self.config["selected"]
            groups = build_groups(self.nodes, self.config["groups"])
            group = self.config["source_groups"].get(source_key) or (
                default_group(source_key, groups) if self.config["automatic"] and source_key not in bindings else "自动选择")
            allowed = groups.get(group, [])
            if allowed is not None and selected not in allowed:
                selected = next((node["name"] for node in self.nodes if node["name"] in allowed), "")
            if self.config["automatic"] and url:
                now = time.monotonic()
                winner = self.winners.get(route)
                if winner and now - winner[1] < 600:
                    selected = winner[0]
                names = [node["name"] for node in self.nodes if node["type"] not in ("direct", "reject")]
                if allowed is not None:
                    names = [name for name in names if name in allowed]
                # Try different sub-subscriptions before exhausting one source
                # whose many aliases can all share the same exit.
                pools = {}
                for name in names:
                    origin = name.split(" / ", 1)[0] if " / " in name else ""
                    pools.setdefault(origin, []).append(name)
                ordered = [pool[index] for index in range(max(map(len, pools.values()), default=0))
                           for pool in pools.values() if index < len(pool)]
                candidates = ([selected] if selected in names else []) + [name for name in ordered if name != selected]
                identities = {node["name"]: self._identity(node) for node in self.nodes}
                candidates = [name for name in candidates if name not in exclude
                              and now - self.health.get((route, identities[name]), -1e9) >= 120]
                if not candidates:
                    raise RuntimeError("此数据源的候选节点近期均失败，请稍后重试或更换订阅")
                selected = candidates[0]
            index = next((i for i, node in enumerate(self.nodes) if node["name"] == selected), None)
            if index is None:
                raise RuntimeError("内置代理的默认节点或数据源指定节点已失效，请在设置中重新选择")
            password = self._credentials()["password"]
            host = self.config["trawl_host"] if remote else "127.0.0.1"
            return f"http://cinefold:{quote(password, safe='')}@{host}:{FIRST_PORT + index}"

    @staticmethod
    def _identity(node):
        return hashlib.sha256(json.dumps({key: value for key, value in node.items() if key != "name"}, sort_keys=True).encode()).hexdigest()

    @contextmanager
    def pause_traffic(self):
        deadline = time.monotonic() + 120
        try:
            with self.available:
                self.draining = True
                while self.allocated:
                    if time.monotonic() >= deadline or self.stopping.is_set():
                        raise RuntimeError("代理请求尚未结束，已取消替换，请稍后重试")
                    self.available.wait(0.1)
            yield
        finally:
            with self.available:
                self.draining = False
                self.available.notify_all()

    @contextmanager
    def lease(self, remote=False, source_key="", url="", timeout=20):
        """Spread bursts: distinct active nodes, at most two starts per 10 seconds."""
        from app.modules.ladysite.transport import current_budget
        deadline = time.monotonic() + timeout
        name = None
        with self.available:
            while True:
                budget = current_budget()
                if budget:
                    budget.check()
                if not self.config["enabled"]:
                    proxy = None
                    break
                now = time.monotonic()
                if now >= deadline or self.stopping.is_set():
                    raise TimeoutError("代理节点繁忙或达到短时请求上限，排队超时")
                if self.draining:
                    self.available.wait(0.05)
                    continue
                busy = {self._identity(node) for node in self.nodes if node["name"] in self.allocated}
                excluded = set()
                for node in self.nodes:
                    identity = self._identity(node)
                    recent = [stamp for stamp in self.recent_requests.get(identity, []) if now - stamp < 10]
                    self.recent_requests[identity] = recent
                    if identity in busy or len(recent) >= 2:
                        excluded.add(node["name"])
                try:
                    proxy = self.proxy(remote, source_key, url, exclude=excluded)
                except RuntimeError:
                    # An otherwise healthy pool may only be busy/rate-limited.
                    self.proxy(remote, source_key, url)
                    proxy = None
                candidate = self.nodes[urlparse(proxy).port - FIRST_PORT] if proxy else None
                if candidate is not None and candidate["name"] not in excluded:
                    name = candidate["name"]
                    self.allocated.add(name)
                    self.recent_requests[self._identity(candidate)].append(now)
                    break
                self.available.wait(0.05)
        try:
            yield proxy
        finally:
            if name is not None:
                with self.available:
                    self.allocated.discard(name)
                    self.available.notify_all()

    def proxy_name(self, proxy):
        """A log-safe label: never return the authenticated listener URL."""
        if not proxy:
            return "默认代理"
        index = (urlparse(proxy).port or 0) - FIRST_PORT
        with self.lock:
            return self.nodes[index]["name"] if 0 <= index < len(self.nodes) else "未知节点"

    def note_result(self, url, proxy, success):
        if not proxy or not self.config["enabled"]:
            return
        parsed = urlparse(proxy)
        if parsed.username != "cinefold" or parsed.password != self.credentials.get("password"):
            return
        index = (parsed.port or 0) - FIRST_PORT
        with self.lock:
            if not 0 <= index < len(self.nodes):
                return
            name = self.nodes[index]["name"]
            identity = self._identity(self.nodes[index])
            route = self._source("", url)[1]
            if success:
                self.winners[route] = (name, time.monotonic())
                self.health.pop((route, identity), None)
            else:
                self.health[(route, identity)] = time.monotonic()
                winner = self.winners.get(route, (None,))[0]
                if any(node["name"] == winner and self._identity(node) == identity for node in self.nodes):
                    self.winners.pop(route, None)
            # Bound runtime history for long-lived installations.
            if len(self.health) > 2048:
                self.health = {key: value for key, value in self.health.items() if time.monotonic() - value < 120}
            if len(self.winners) > 512:
                self.winners = dict(sorted(self.winners.items(), key=lambda item: item[1][1])[-512:])

    def start_monitor(self):
        if self.monitor is not None:
            return
        self.stopping.clear()

        def watch():
            attempts = 0
            while not self.stopping.wait(5):
                if not self.config["enabled"] or self.status()["running"]:
                    attempts = 0
                    continue
                if self.operation.locked() or not self.binary.exists() or not self.nodes or attempts >= 3:
                    continue
                if not self.operation.acquire(blocking=False):
                    continue
                try:
                    self._start()
                    attempts = 0
                except Exception as exc:
                    attempts += 1
                    self._progress("failed", _error(exc), running=False)
                finally:
                    self.operation.release()

        self.monitor = threading.Thread(target=watch, name="mihomo-monitor", daemon=True)
        self.monitor.start()

    def shutdown(self):
        self.stopping.set()
        if self.monitor is not None:
            self.monitor.join(timeout=15)
            self.monitor = None
        self._stop()


_instance = None
_instance_lock = threading.Lock()


def get_manager():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Mihomo(Path(os.getenv("DATA_DIR", "/data")) / "mihomo")
            atexit.register(_instance.shutdown)
        return _instance


def local_proxy(fallback=None, source_key="", url=""):
    return get_manager().proxy(source_key=source_key, url=url) or fallback


@contextmanager
def proxy_lease(fallback=None, explicit=None, source_key="", url=""):
    if explicit is not None:
        yield explicit
    else:
        with get_manager().lease(source_key=source_key, url=url) as proxy:
            yield proxy or fallback
