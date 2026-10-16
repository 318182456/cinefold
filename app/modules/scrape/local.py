"""读取影片旁的 NFO/JSON 和封面。参考 AVM 的本地优先流程，适配 Code 字段。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from app.modules.scrape.merge import base_code
from app.utils import get_true_code, mediafile

MAX_SIDECAR_BYTES = 2 * 1024 * 1024
VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".iso"}


def _same_code(left: str, right: str) -> bool:
    left = get_true_code(left) or left.upper().strip()
    right = get_true_code(right) or right.upper().strip()
    return (base_code(left) or left) == (base_code(right) or right)


def _joined(values) -> str:
    if not isinstance(values, list):
        values = re.split(r"[,，、|\n]+", str(values or ""))
    names = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("name", "")
        value = str(value or "").strip()
        if value and value not in names:
            names.append(value)
    return ",".join(names)


def _xml(raw: bytes) -> dict:
    declaration = re.search(br'<\?xml[^>]*encoding\s*=\s*[\"\x27]([a-zA-Z0-9_-]+)', raw[:256], re.I)
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else declaration.group(1).decode('ascii') if declaration else "utf-8-sig"
    text = raw.decode(encoding).lstrip('\ufeff')
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return {}
    root = ET.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', text))
    if root.tag != "movie":
        return {}
    def first(*paths):
        return next(((root.findtext(p) or "").strip() for p in paths if (root.findtext(p) or "").strip()), "")
    # uniqueid 可能是 TMDB 数字，只把可识别的番号用于身份校验。
    identifiers = [first("num"), first("id"), *(x.text or "" for x in root.findall("uniqueid"))]
    code = next((get_true_code(x) for x in identifiers if get_true_code(x)), "")
    duration = first("runtime")
    if not duration:
        seconds = first("durationinseconds", "fileinfo/streamdetails/video/durationinseconds")
        if seconds.isdigit():
            duration = str(int(seconds) // 60)
    return {"code": code, "title": first("title", "localtitle"), "originaltitle": first("originaltitle"),
            "outline": first("plot", "outline"), "release_date": first("premiered", "releasedate", "release_date"),
            "duration": duration, "producer": first("studio", "maker"), "publisher": first("publisher", "label"),
            "series": first("set/name", "set", "series"), "casts": _joined([x.findtext("name") for x in root.findall("actor")]),
            "genres": _joined([x.text for x in [*root.findall("genre"), *root.findall("tag")]]),
            "star": first("rating", "ratings/rating/value"),
            "artwork": first("art/fanart", "fanart/thumb", "art/poster", "poster", "thumb")}


def read(path: Path, code: str) -> dict:
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            return {}
        raw = path.read_bytes()[:MAX_SIDECAR_BYTES + 1]
        if len(raw) > MAX_SIDECAR_BYTES:
            return {}
        if path.suffix.lower() == ".json":
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                return {}
            aliases = {"code": ("code", "number", "num"), "originaltitle": ("originaltitle", "original_title", "jp_title"),
                "outline": ("outline", "plot", "description"), "duration": ("duration", "runtime"),
                "producer": ("producer", "studio", "maker"), "publisher": ("publisher", "label"),
                "casts": ("casts", "actors", "actresses"), "star": ("star", "rating"), "artwork": ("banner", "fanart", "cover", "poster")}
            data = {**data, **{key: next((data[x] for x in keys if data.get(x) not in (None, "", [])), "") for key, keys in aliases.items()}}
        else:
            data = _xml(raw)
        if not data or data.get("code") and not _same_code(str(data["code"]), code):
            return {}
        title = str(data.get("title") or "").strip()
        title = re.sub(rf"^{re.escape(code)}(?:\s+|\s*[-:]\s*)", "", title, flags=re.I)
        original = str(data.get("originaltitle") or "").strip()
        detail = {key: data.get(key) for key in ("outline", "release_date", "duration", "producer", "publisher", "series") if data.get(key)}
        if title or original:
            detail["title"] = original or title
        if isinstance(data.get("cn_title"), str) and data["cn_title"].strip():
            detail["cn_title"] = data["cn_title"].strip()
        elif original and title and original != title:
            detail["cn_title"] = title
        for key in ("casts", "genres"):
            if data.get(key):
                detail[key] = _joined(data[key])
        if data.get("tags"):
            detail["genres"] = _joined(f"{detail.get('genres', '')},{_joined(data['tags'])}")
        try:
            score = float(data.get("star"))
            if 0 <= score <= 10:
                detail["star"] = score
        except (TypeError, ValueError):
            pass
        artwork = str(data.get("artwork") or "")
        if artwork.startswith(("http://", "https://")):
            detail["banner"] = artwork
        elif artwork:
            candidate = (path.parent / artwork).resolve()
            if candidate.is_relative_to(path.parent.resolve()) and candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and candidate.is_file():
                detail["_local_cover"] = str(candidate)
        return detail
    except (OSError, ValueError, TypeError, LookupError, ET.ParseError):
        return {}


def _shared_allowed(video: Path, code: str) -> bool:
    """混放多部影片的目录不采用 movie.nfo / poster.jpg 等共享文件。"""
    try:
        videos = [x for x in video.parent.iterdir() if x.is_file() and x.suffix.lower() in VIDEO_SUFFIXES]
        return all(x == video or _same_code(mediafile.parse(x).code, code) for x in videos)
    except OSError:
        return False


def read_group(code: str, paths: list[Path]) -> dict:
    merged = {}
    for video in paths:
        try:
            existing = {(f.stem.strip() + f.suffix.strip()).casefold(): f for f in video.parent.iterdir() if f.is_file()}
        except OSError:
            continue
        shared = _shared_allowed(video, code)
        names = list(dict.fromkeys([video.stem.strip(), code, base_code(code)]))
        if shared:
            names += ["movie", "index"]
        for name in filter(None, names):
            for suffix in (".nfo", ".json"):
                sidecar = existing.get((name + suffix).casefold())
                if sidecar:
                    for key, value in read(sidecar, code).items():
                        merged.setdefault(key, value)
        if not merged.get("_local_cover"):
            bases = [f"{video.stem}-fanart", f"{video.stem}-poster", video.stem]
            if shared:
                bases += ["fanart", "poster", "folder", "cover"]
            covers = [name + suffix for name in bases for suffix in ('.jpg', '.jpeg', '.png', '.webp')]
            for name in covers:
                image = existing.get(name.casefold())
                if image and image.resolve().is_relative_to(video.parent.resolve()):
                    merged["_local_cover"] = str(image.resolve())
                    break
        if merged.get("_local_cover"):
            merged["_local_video"] = str(video)
    return merged
