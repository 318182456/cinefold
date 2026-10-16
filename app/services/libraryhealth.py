"""后台检查登记文件：硬链接只算一份，分卷单独标记，不自动删除。"""
from __future__ import annotations

import copy
import hashlib
import re
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.database.models import Code, MediaLink
from app.database.session import session_scope

_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "result": None, "error": ""}
# 大文件哈希只按 stat 身份缓存；任何内容/大小/时间变化都重新读取。
_hashes: dict[tuple, str] = {}
_PART = re.compile(r"(?:[-_. ](?:cd|part|disc|disk|vol)[-_. ]?\d+|[ ]?\(\d+\)|[-_. ][abde])$", re.I)
_SAMPLE = re.compile(r"(?:\bsample\b|\btrailer\b|广告|预告)", re.I)


def snapshot() -> dict:
    with _lock:
        return copy.deepcopy(_state)


def start(*, hashes: bool = False) -> dict:
    with _lock:
        if _state["running"]:
            return copy.deepcopy(_state)
        _state.update(running=True, done=0, total=0, result=None, error="", started_at=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=_run, args=(hashes,), daemon=True, name="library-health").start()
    return snapshot()


def _run(hashes: bool) -> None:
    try:
        result = scan(hashes=hashes)
        with _lock:
            _state.update(running=False, result=result, finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:
        with _lock:
            _state.update(running=False, error=f"检查失败：{type(exc).__name__}")


def _digest(path: Path, stat) -> str:
    key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key not in _hashes:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
            raise ValueError("文件正在变化，稍后重新检查")
        _hashes[key] = digest.hexdigest()
    return _hashes[key]


def scan(*, hashes: bool = False) -> dict:
    with session_scope() as session:
        links = [x.to_dict() for x in session.scalars(select(MediaLink))]
        codes = {x.code: x.to_dict() for x in session.scalars(select(Code))}
    with _lock:
        _state["total"] = len(links)
    issues, physical, by_code, by_size = [], {}, defaultdict(list), defaultdict(list)
    apparent = 0
    for index, link in enumerate(links):
        path = Path(link["link_path"])
        try:
            stat = path.stat()
            apparent += stat.st_size
            identity = (stat.st_dev, stat.st_ino) if stat.st_ino else str(path.resolve())
            file = {"code": link["code"], "path": str(path), "size": stat.st_size, "hardlinks": stat.st_nlink,
                    "multipart": bool(_PART.search(path.stem)), "stat": stat}
            by_code[link["code"]].append(file)
            if identity not in physical:
                physical[identity] = file
                by_size[stat.st_size].append(file)
            if stat.st_size == 0:
                issues.append({"kind": "empty", "code": link["code"], "path": str(path), "message": "空文件"})
            elif _SAMPLE.search(path.stem):
                issues.append({"kind": "sample", "code": link["code"], "path": str(path), "message": "文件名疑似样片或预告，需人工核对"})
            if link["source_path"] != str(path) and not Path(link["source_path"]).exists():
                issues.append({"kind": "source_missing", "code": link["code"], "path": link["source_path"], "message": "源路径不存在，媒体库链接可能仍可用"})
        except (OSError, ValueError):
            issues.append({"kind": "unavailable", "code": link["code"], "path": str(path), "message": "文件不存在或无法访问（请检查挂载与权限）"})
        with _lock:
            _state["done"] = index + 1
    for code in by_code:
        metadata = codes.get(code, {})
        missing = [name for key, name in (("title", "标题"), ("casts", "演员"), ("genres", "标签")) if not metadata.get(key)]
        if missing:
            issues.append({"kind": "metadata", "code": code, "path": "", "message": "缺少" + "、".join(missing)})
    def clean(file):
        return {k: v for k, v in file.items() if k != "stat"}
    versions = [{"code": code, "multipart": any(x["multipart"] for x in files), "files": [clean(x) for x in files]}
                for code, files in by_code.items() if len(files) > 1]
    exact = []
    if hashes:
        for size, files in by_size.items():
            if not size or len(files) < 2:
                continue
            groups = defaultdict(list)
            for file in files:
                try:
                    groups[_digest(Path(file["path"]), file["stat"])].append(clean(file))
                except (OSError, ValueError):
                    issues.append({"kind": "hash_failed", "code": file["code"], "path": file["path"], "message": "文件变化或读取失败，未判断重复"})
            exact.extend({"sha256": key, "size": size, "files": group} for key, group in groups.items() if len(group) > 1)
    return {"issues": issues, "versions": versions, "exact": exact, "hashed": hashes,
            "links": len(links), "physical_files": len(physical), "apparent_bytes": apparent,
            "unique_bytes": sum(x["size"] for x in physical.values()),
            "duplicate_bytes": sum(x["size"] * (len(x["files"]) - 1) for x in exact)}
