"""JSON/RSS/论坛资源采集，按磁链 hash 去重并关联番号。采集不自动订阅。"""
from __future__ import annotations

import base64
import re
import threading
from html import unescape
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import Resource, ResourceFeed, ResourceMatch
from app.database.session import session_scope
from app.utils import find_serial_numbers, get_magnet_hash

_lock = threading.Lock()
_import_lock = threading.Lock()
_state = {"running": False, "added": 0, "errors": []}
MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"'\[\]]+", re.I)


def import_items(items: list[dict], source: str) -> int:
    with _import_lock:
        return _import_items(items, source)


def _import_items(items: list[dict], source: str) -> int:
    added = 0
    with session_scope() as session:
        for item in items:
            title = str(item.get("title") or "")[:2000]
            urls = item.get("magnet") or item.get("download_url") or []
            urls = [urls] if isinstance(urls, str) else urls
            if not isinstance(urls, list):
                continue
            for magnet in dict.fromkeys(str(x) for x in urls):
                if not magnet.lower().startswith("magnet:?"):
                    continue
                key = get_magnet_hash(magnet)
                if not key:
                    continue
                key = key.lower()
                if len(key) == 32:
                    key = base64.b32decode(key.upper()).hex()
                row = session.get(Resource, key)
                if row is None:
                    try:
                        size = max(0, float(item.get("size_mb") or 0))
                    except (TypeError, ValueError):
                        size = 0
                    row = Resource(hash=key, title=title, magnet=magnet, source=source,
                                   detail_url=str(item.get("detail_url") or "")[:4000], size_mb=size)
                    session.add(row)
                    session.flush()
                    added += 1
                codes = find_serial_numbers(title)
                for code in codes:
                    session.merge(ResourceMatch(hash=key, code=code))
                session.flush()
    return added


def parse_html(html: str, url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("#thread_subject, h1, #nav, title")
    title_text = title.get_text(" ", strip=True) if title else ""
    magnets = sorted(set(unescape(x).rstrip(".,;)") for x in MAGNET_RE.findall(unescape(html))))
    return [{"title": title_text, "magnet": magnets, "detail_url": url}] if magnets else []


def fetch_feed(feed: ResourceFeed) -> list[dict]:
    from app.modules.ladysite.base import SiteClient
    settings = get_settings()
    if feed.kind == "json":
        with httpx.Client(timeout=45, proxy=settings.proxy or None, trust_env=False) as client:
            response = client.get(feed.url, headers={"Cookie": feed.cookie} if feed.cookie else {})
            response.raise_for_status()
            data = response.json()
        items = data.get("data", data.get("items", [])) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("JSON 需要返回资源数组或 data/items 数组")
        return [item for item in items[:2000] if isinstance(item, dict)]
    site = SiteClient(f"{urlsplit(feed.url).scheme}://{urlsplit(feed.url).netloc}", cookie=feed.cookie)
    html = site.get(feed.url)
    if not html:
        raise ValueError("页面读取失败")
    if feed.kind == "rss":
        root = ElementTree.fromstring(html)
        result = []
        for item in root.findall(".//item")[:200]:
            title = item.findtext("title") or ""
            content = " ".join(item.itertext())
            magnets = MAGNET_RE.findall(unescape(content))
            magnets += [e.get("url", "") for e in item.findall("enclosure") if e.get("url", "").startswith("magnet:")]
            result.append({"title": title, "magnet": magnets, "detail_url": item.findtext("link") or ""})
        return result
    result = parse_html(html, feed.url)
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for anchor in soup.select('a[href*="thread-"], a[href*="mod=viewthread"]'):
        target = urljoin(feed.url, anchor.get("href", ""))
        if urlsplit(target).netloc == urlsplit(feed.url).netloc and target not in urls:
            urls.append(target)
    for target in urls[:30]:
        detail = site.get(target)
        if detail:
            result.extend(parse_html(detail, target))
    return result


def state() -> dict:
    with _lock:
        return {**_state, "errors": list(_state["errors"])}


def start() -> dict:
    with _lock:
        if _state["running"]:
            return {**_state}
        _state.update(running=True, added=0, errors=[])
    threading.Thread(target=_run, daemon=True, name="resource-feeds").start()
    return state()


def _run() -> None:
    try:
        with session_scope() as session:
            feeds = session.scalars(select(ResourceFeed).where(ResourceFeed.enabled.is_(True))).all()
        for feed in feeds:
            try:
                added = import_items(fetch_feed(feed), feed.name)
                with _lock:
                    _state["added"] += added
            except Exception as exc:
                with _lock:
                    _state["errors"].append(f"{feed.name}：{type(exc).__name__}，请检查地址、登录状态和响应格式")
    finally:
        with _lock:
            _state["running"] = False


def search(code: str) -> list:
    from app.modules.ptsite import convert_torrent
    with session_scope() as session:
        rows = session.scalars(select(Resource).join(ResourceMatch, ResourceMatch.hash == Resource.hash).where(ResourceMatch.code == code).order_by(Resource.create_time.desc()).limit(100)).all()
        results = []
        for row in rows:
            torrent = convert_torrent({"title": row.title, "size_mb": row.size_mb, "download_url": row.magnet, "detail_url": row.detail_url}, "BT", code)
            torrent.source_site = row.source
            results.append(torrent)
        return results
