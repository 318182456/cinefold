"""Search Xunlei's subtitle catalogue by code, then validate each download."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx
from loguru import logger

from app.core.config import get_settings
from app.modules.ladysite.base import SiteClient, text_contains_code
from app.modules.ladysite.transport import report_response
from app.modules.scrape.merge import base_code
from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES, MIN_SUBTITLE_BYTES, SUBTITLE_SUFFIXES,
    SubtitleItem, as_simplified_chinese, decode_subtitle, looks_like_subtitle,
)
from app.services.mihomo import get_manager, proxy_lease
from app.utils import get_true_code

HOST = "https://api-shoulei-ssl.xunlei.com"
MAX_CANDIDATES = 6


class XunleiSubtitle:
    name = "xunlei"

    def __init__(self, host=""):
        self.client = SiteClient(host, interval=1.0) if host else SiteClient.from_source(self.name)
        if self.client is not None:
            self.client.source_key = self.name

    def search(self, code: str) -> SubtitleItem | None:
        original = get_true_code(code)
        lookup = base_code(original) or original
        if not lookup or self.client is None:
            return None
        raw = self.client.get("/oracle/subtitle", params={"gcid": "", "cid": "", "name": lookup}, allow_bypass=False)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            report_response("")
            return None
        if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
            report_response("")
            return None

        candidates = []
        seen = set()
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            title = item.get("name")
            url = item.get("url")
            suffix = "." + str(item.get("ext") or "").lower().lstrip(".")
            if not isinstance(title, str) or not text_contains_code(title, lookup):
                continue
            if suffix not in SUBTITLE_SUFFIXES or not isinstance(url, str) or url in seen:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            seen.add(url)
            candidates.append((suffix, title, url))
        # Plain SRT before styled ASS: the sampled ASS included promotional overlays.
        candidates.sort(key=lambda entry: entry[0] != ".srt")
        for suffix, title, url in candidates[:MAX_CANDIDATES]:
            content = self._download(url)
            if content:
                # Use the real format, not a possibly misleading API extension.
                if re.search(r"(?im)^\[events\]", content):
                    suffix = ".ass" if suffix != ".ssa" else suffix
                elif content.lstrip().startswith("WEBVTT"):
                    suffix = ".vtt"
                else:
                    suffix = ".srt"
                return SubtitleItem(code=original, title=title, site=self.name, content=content, suffix=suffix)
        return None

    def _download(self, url):
        settings = get_settings()
        try:
            with proxy_lease(settings.proxy or None, self.client.proxy, self.name, url) as proxy, httpx.Client(
                timeout=20, follow_redirects=True, proxy=proxy, verify=False, trust_env=False,
            ) as http:
                with http.stream("GET", url, headers=self.client.headers({"Cookie": "", "Accept": "*/*"})) as response:
                    get_manager().note_result(url, proxy, response.status_code < 400)
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_SUBTITLE_BYTES:
                            return ""
            if len(raw) < MIN_SUBTITLE_BYTES:
                return ""
            text = decode_subtitle(bytes(raw))
            if not looks_like_subtitle(text):
                return ""
            # ASS headers alone are not a subtitle; require at least one timed cue.
            if not re.search(r"(?im)^Dialogue:\s*[^\n]*\d:\d{2}:\d{2}[.,]\d{2,3}|\d{1,2}:\d{2}:\d{2}[,.]\d{2,3}\s*-->", text):
                return ""
            return as_simplified_chinese(text)
        except Exception as exc:
            report_response("")
            # Avoid logging signed download URLs (which can contain credentials).
            logger.debug(f"[字幕] 迅雷候选下载失败: {type(exc).__name__}")
            return ""
