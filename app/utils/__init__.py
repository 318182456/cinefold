"""通用工具函数。"""
from __future__ import annotations

import functools
import hashlib
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar
from urllib.parse import urlparse, unquote

from loguru import logger

T = TypeVar("T")

# 番号形如 ABP-984 / SSIS-001 / 259LUXU-1234 / FC2-PPV-1234567
# 边界用 lookaround 而非消耗字符，否则 "nhdta-800、nhdta-526" 这类连写会
# 因为分隔符被前一次匹配吃掉，导致后面的番号丢失。
CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"((?:FC2[-_ ]?PPV[-_ ]?\d{6,8})|(?:[0-9]{0,4}[A-Za-z]{2,6}[-_ ]?\d{2,5}))"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 需要从标题里剔除的常见噪声，避免误判成番号
NOISE_WORDS = {"1080P", "720P", "2160P", "4K", "8K", "H264", "H265", "X264", "X265", "HEVC"}


def is_code(text: str) -> bool:
    """判断字符串是否是一个番号。"""
    if not text:
        return False
    candidate = text.strip().upper()
    if candidate in NOISE_WORDS:
        return False
    return bool(re.fullmatch(r"(?:FC2-PPV-\d{6,8})|(?:[0-9]{0,4}[A-Z]{2,6}-?\d{2,5})", candidate))


def find_serial_number(text: str) -> str:
    """从文件名或标题中提取番号，找不到返回空串。"""
    if not text:
        return ""
    # 先剥掉扩展名与常见分隔噪声
    cleaned = re.sub(r"\.(mp4|mkv|avi|wmv|rmvb|iso|torrent)$", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("_", "-").replace(" ", "-")

    for match in CODE_PATTERN.finditer(cleaned):
        candidate = match.group(1).upper().replace("_", "-").replace(" ", "-")
        if candidate.upper() in NOISE_WORDS:
            continue
        return get_true_code(candidate)
    return ""


def find_serial_numbers(text: str, limit: int = 0) -> list[str]:
    """提取文本中出现的所有番号，按出现顺序去重。

    一条消息里列一串番号（"nhdta-800、nhdta-526、..."）是常见写法，
    find_serial_number 只取第一个，这里返回全部。limit>0 时截断。
    """
    if not text:
        return []
    cleaned = re.sub(r"\.(mp4|mkv|avi|wmv|rmvb|iso|torrent)$", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("_", "-").replace(" ", "-")

    out: list[str] = []
    seen: set[str] = set()
    for match in CODE_PATTERN.finditer(cleaned):
        candidate = match.group(1).upper().replace("_", "-").replace(" ", "-")
        if candidate in NOISE_WORDS:
            continue
        code = get_true_code(candidate)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
        if limit and len(out) >= limit:
            break
    return out


# 干净番号的样子，用于判断输入是否已经标准化
_CLEAN_CODE = re.compile(r"[0-9]{0,4}[A-Z]+-\d{2,5}")
# 从"番号 + 后缀噪声"里框出番号那一段。右边界不能是字母数字，
# 否则 ABP9841080P 这种连写会被劈成 ABP-984
_CODE_HEAD = re.compile(r"^([0-9]{0,4}[A-Z]{2,6})-?(\d{2,5})(?![A-Za-z0-9])")
# FC2 前缀后紧跟的第一段数字才是番号
_FC2_HEAD = re.compile(r"^FC2-?(?:PPV-?)?(\d+)")
# 番号自带的后缀，要保留 —— 中文字幕(C/CH)、无码(U/UC)是番号的一部分，
# 同一部片子的不同版本靠它区分。画质、日期、分集号不在此列，一律切掉
_CODE_SUFFIX = re.compile(r"^-(?:C|CH|U|UC|UCH)(?![A-Za-z0-9])")


def _with_suffix(core: str, rest: str) -> str:
    """把番号本体与紧随其后的合法后缀拼回去。rest 是本体之后的剩余部分。"""
    match = _CODE_SUFFIX.match(rest)
    return core + match.group(0) if match else core


def get_true_code(code: str) -> str:
    """标准化番号格式：统一大写，字母与数字之间补横杠。

    只处理"番号本身"，末尾挂着的标题噪声会被切掉，但 -C/-UC 这类版本
    后缀会保留。传进来的若是整个标题（Emby 的 Name、种子名等），请先用
    find_serial_number 框出番号再交给这里——本函数不做全文搜索，只认
    开头那一段。

    无法构成番号的输入（纯符号、纯数字等）返回空串，避免脏数据入库。
    """
    if not code:
        return ""
    normalized = code.strip().upper().replace("_", "-").replace(" ", "-")
    # 番号必须同时含字母与数字
    if not (re.search(r"[A-Z]", normalized) and re.search(r"\d", normalized)):
        return ""
    if normalized.startswith("FC2"):
        # 只取前缀后的第一段数字。不能用 re.sub(r"\D", "") 把全串数字拼起来，
        # 那样 "FC2PPV-1570936 (1080P) 9134" 会得到 FC2-PPV-157093610809134
        match = _FC2_HEAD.match(normalized)
        if not match:
            return normalized
        return _with_suffix(f"FC2-PPV-{match.group(1)}", normalized[match.end():])
    if "-" in normalized:
        # 已经干净就原样返回。这条快路必须留着：T28-544 这类单字母前缀
        # 不符合 _CODE_HEAD 的 {2,6} letters，切一刀反而会切坏
        if _CLEAN_CODE.fullmatch(normalized):
            return normalized
        match = _CODE_HEAD.match(normalized)
        if not match:
            return normalized
        core = f"{match.group(1)}-{match.group(2)}"
        return _with_suffix(core, normalized[match.end():])
    # ABP984 → ABP-984
    match = re.match(r"^([0-9]{0,4}[A-Z]+)(\d+)$", normalized)
    return f"{match.group(1)}-{match.group(2)}" if match else normalized


def has_number(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def get_magnet_hash(magnet: str) -> str:
    """从磁力链接中提取 info hash。"""
    if not magnet:
        return ""
    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    return match.group(1).lower() if match else ""


def get_torrent_hash(content: bytes) -> str:
    """计算 .torrent 文件的 info hash。"""
    try:
        from torrentool.api import Torrent as _T
        return _T.from_string(content).info_hash.lower()
    except Exception as exc:
        logger.warning(f"解析种子失败，退化为内容摘要: {exc}")
        return hashlib.sha1(content).hexdigest()


def to_cookie_dict(cookie: str) -> dict:
    """"a=1; b=2" → {"a": "1", "b": "2"}"""
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def get_protocol_and_domain(url: str) -> str:
    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""


def get_host_and_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url or "")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname or "", parsed.port or default_port


def get_ip_by_domain(domain: str) -> str:
    try:
        return socket.gethostbyname(domain)
    except OSError:
        return ""


def get_filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url or "").path).name)


file_name_from_url = get_filename_from_url  # 原项目两个名字都在用


def get_image_suffix_from_url(url: str, default: str = ".jpg") -> str:
    suffix = Path(urlparse(url or "").path).suffix.lower()
    return suffix if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif") else default


def safe_map_url_to_filesystem(url: str) -> str:
    """把图片 URL 映射成安全的本地相对路径。"""
    parsed = urlparse(url or "")
    raw = f"{parsed.netloc}{parsed.path}"
    safe = re.sub(r"[^A-Za-z0-9._/\-]", "_", raw).lstrip("/")
    # 防目录穿越
    return safe.replace("..", "_")


def check_file_exists(path: str | Path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def date_str_to_timestamp(value: str, fmt: str = "%Y-%m-%d") -> int:
    try:
        return int(datetime.strptime(value.strip(), fmt).timestamp())
    except (ValueError, AttributeError):
        return 0


def copy_properties(source: Any, target: Any, skip_empty: bool = True) -> Any:
    """把 source 上的同名属性复制到 target，默认跳过空值以免覆盖已有数据。"""
    for key in vars(source) if hasattr(source, "__dict__") else []:
        if key.startswith("_") or not hasattr(target, key):
            continue
        value = getattr(source, key)
        if skip_empty and value in (None, "", [], {}):
            continue
        setattr(target, key, value)
    return target


def dict_trans_obj(data: dict, cls: type[T]) -> T:
    """dict → 对象，忽略多余的键。"""
    obj = cls()
    for key, value in (data or {}).items():
        if hasattr(obj, key):
            setattr(obj, key, value)
    return obj


def unique_objects_by_attribute(items: Iterable[T], attr: str) -> list[T]:
    """按属性去重，保留首次出现的顺序。"""
    seen: set = set()
    out: list[T] = []
    for item in items:
        key = getattr(item, attr, None)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def encrypt_first_half(text: str) -> str:
    """前半段打码，用于日志里脱敏。"""
    if not text:
        return ""
    half = len(text) // 2
    return "*" * half + text[half:]


def run_in_background(func: Callable, *args, **kwargs) -> threading.Thread:
    """丢到后台线程执行，异常只记日志不外抛。"""
    def _wrapper():
        try:
            func(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"后台任务 {func.__name__} 异常: {exc}")

    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.start()
    return thread


def timer(name: str = ""):
    """记录函数耗时。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                cost = time.perf_counter() - start
                logger.debug(f"{name or func.__name__} 耗时 {cost:.2f}s")
        return wrapper
    return decorator


def timer_count(name: str = ""):
    """记录函数耗时与返回条数。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = func(*args, **kwargs)
            cost = time.perf_counter() - start
            count = len(result) if isinstance(result, (list, tuple, dict)) else 1
            logger.debug(f"{name or func.__name__} 耗时 {cost:.2f}s，返回 {count} 条")
            return result
        return wrapper
    return decorator
