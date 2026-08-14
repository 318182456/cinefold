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

# 番号形如 ABP-984 / SSIS-001 / 259LUXU-1234 / FC2-PPV-1234567 / S2MBD-002
# 边界用 lookaround 而非消耗字符，否则 "nhdta-800、nhdta-526" 这类连写会
# 因为分隔符被前一次匹配吃掉，导致后面的番号丢失。
#
# 厂牌前缀里允许内嵌数字（S2MBD、T28、H4610）：这类前缀是「字母-数字-字母」
# 交错的，用 [0-9]{0,4}[A-Za-z]{2,6} 死活匹配不上 —— 数字段只能在最前面，
# S2MBD 的那个 2 卡在中间，而让数字段从 2 开始又会让 S 撞上左边界断言。
# 所以字母段本身要容许数字，只是首字符必须是字母（纯数字开头由前面的
# [0-9]{0,4} 负责，两者不能混在一起，否则 "1080-1234" 这种也会被当番号）。
#
# FC2 有两种写法：带 PPV（FC2-PPV-1234567）和不带（FC2-1347256）。
# 不带的那种数字位数与通用分支的 \d{2,5} 冲突（7 位数字），必须单列一支，
# 否则会被通用分支切成 FC2-13472 这种错番号。
CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"((?:FC2[-_ ]?PPV[-_ ]?\d{6,8})"
    r"|(?:FC2[-_ ]?\d{6,8})"
    # 日期型（032416_267，无码站的 MMDDYY_序号）。放在字母那几支之前，
    # 否则 Carib-032416_267 会先被前缀那支匹配成 CARIB-032416。
    #
    # 边界全交给外层的 (?<![A-Za-z0-9]) / (?![A-Za-z0-9])：
    #   - 左边界已挡住 20240315_001 —— 240315 前面是数字 0
    #   - 右边界不能额外排除 -，否则 032416_267-1pon-1080p 这种常见文件名
    #     会整条匹配失败（番号后紧跟 - 是常态）
    # 序号段写 \d{2,4} 而非 \d+ 是唯一的宽度约束，够用：真实序号都是 2-4 位
    r"|(?:\d{6}[-_]\d{2,4})"
    # 无分隔符写法（032416267）。这一支在全文搜索里风险最高 —— 任意 8-10 位
    # 数字都是这个形状，所以把 MMDDYY 的月日范围直接写进正则，而不是只靠
    # 事后的 _is_mmddyy 校验（finditer 一旦框错就直接返回，没有回退）
    r"|(?:(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{2}\d{2,4})"
    r"|(?:[0-9]{0,4}[A-Za-z][A-Za-z0-9]{1,7}[-_ ]\d{2,5})"
    r"|(?:[0-9]{0,4}[A-Za-z]{2,6}\d{2,5}))"
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
    # 日期型（032416_267 / 032416267）整个是纯数字，下面那个模式要求字母，
    # 认不出它。前 6 位要像 MMDDYY，否则任意 9 位数字都会被当成番号
    date_match = _DATE_CODE.match(candidate) or _DATE_CODE_PLAIN.match(candidate)
    if date_match and _is_mmddyy(date_match.group(1)):
        return True
    return bool(re.fullmatch(r"(?:FC2-PPV-\d{6,8})|(?:[0-9]{0,4}[A-Z]{2,6}-?\d{2,5})", candidate))


def find_serial_number(text: str) -> str:
    """从文件名或标题中提取番号，找不到返回空串。"""
    if not text:
        return ""
    # 先剥掉扩展名与常见分隔噪声。
    # 下划线保留原样：日期型番号（032416_267）的下划线是官方写法的一部分，
    # 拍平成 - 会让 get_true_code 认不出它、也就吐不回正确的分隔符。
    # 其余番号的 _ 由 get_true_code 自己拍平，这里不做
    cleaned = re.sub(r"\.(mp4|mkv|avi|wmv|rmvb|iso|torrent)$", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace(" ", "-")

    for match in CODE_PATTERN.finditer(cleaned):
        candidate = match.group(1).upper().replace(" ", "-")
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
    # 下划线保留原样，理由同 find_serial_number
    cleaned = re.sub(r"\.(mp4|mkv|avi|wmv|rmvb|iso|torrent)$", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace(" ", "-")

    out: list[str] = []
    seen: set[str] = set()
    for match in CODE_PATTERN.finditer(cleaned):
        candidate = match.group(1).upper().replace(" ", "-")
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

# 日期型番号：Caribbeancom / 一本道 / 10musume 等无码站用 MMDDYY_序号，
# 例 032416_267。整个番号纯数字，没有字母。
#
# 分隔符必须保留下划线原样：032416_267 是这些站的官方写法，也是 javdb 上
# 的条目名，规范化成 032416-267 会导致精确比对失败、检索不到。
#
# 日期段限定 6 位、序号段 2-4 位，是为了压住误判面 —— 这个形状太宽泛，
# 订单号、时间戳都长这样。即便如此仍需前后边界断言配合（见 CODE_PATTERN），
# 单靠位数挡不住 8 位日期串 20240315_001 之类
_DATE_CODE = re.compile(r"^(\d{6})[-_](\d{2,4})$")
# 无分隔符的日期型：用户手打搜索时常省掉分隔符（032416267）。
# 只认 8-10 位 —— 日期段固定 6 位，序号 2-4 位，所以总长只可能是这个区间。
# 切分点固定在第 6 位之后：9 位串理论上也能切成 0324162+67，但日期段恒为
# 6 位（MMDDYY），没有歧义
_DATE_CODE_PLAIN = re.compile(r"^(\d{6})(\d{2,4})$")


def _is_mmddyy(part: str) -> bool:
    """这 6 位数字像不像 MMDDYY 日期。

    日期型番号的前 6 位是发布日期，月份 01-12、日 01-31。校验它能挡掉
    大量同形状的非番号（订单号、流水号）—— 尤其是无分隔符写法，
    032416267 与随便一串 9 位数字在形状上无法区分，只能靠这个。

    年份不校验：无码站从 2000 年代至今都在用，两位年任何值都合法。
    """
    if len(part) != 6:
        return False
    month, day = part[:2], part[2:4]
    return "01" <= month <= "12" and "01" <= day <= "31"


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

    # 日期型（032416_267）要在替换分隔符之前分流：下面那句会把 _ 换成 -，
    # 而这类番号的下划线是官方写法的一部分，换掉就查不到了。
    # 同理也不能落到「必须含字母」那道检查上 —— 它整个是纯数字
    stripped = code.strip()
    date_match = _DATE_CODE.match(stripped) or _DATE_CODE_PLAIN.match(stripped)
    if date_match and _is_mmddyy(date_match.group(1)):
        return f"{date_match.group(1)}_{date_match.group(2)}"

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
