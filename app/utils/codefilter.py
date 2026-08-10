"""上行消息里的番号订阅过滤。

TG/企业微信收到的消息是自由文本，直接把识别到的番号丢进订阅会有两类麻烦：

1. 误识别——URL 路径、分辨率、日期等片段长得像番号（javdb.com/v/abc123 → ABC-123）；
2. 不想要的——某些厂牌前缀根本不看，或一条消息里贴了几十个番号刷满订阅表。

这里只做"要不要订阅"的判断，不碰订阅本身的逻辑。
"""
from __future__ import annotations

import re

from loguru import logger

from app.utils import find_serial_numbers, get_true_code

# 一条消息最多接受多少个番号，防止误识别或恶意刷屏灌满订阅表
DEFAULT_MAX_PER_MESSAGE = 50

# URL 与番号形态高度重合，先整段剔除再识别
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# 番号前缀（去掉数字段后的字母部分），用于黑白名单比对
PREFIX_RE = re.compile(r"^([0-9]{0,4}[A-Z]+)")


def _prefix(code: str) -> str:
    """NHDTB-424 → NHDTB；259LUXU-1234 → 259LUXU；FC2-PPV-123 → FC2"""
    if code.startswith("FC2"):
        return "FC2"
    match = PREFIX_RE.match(code)
    return match.group(1) if match else ""


def _parse_list(raw: str) -> set[str]:
    """逗号/竖线/空格分隔的前缀列表 → 大写集合。"""
    if not raw:
        return set()
    return {p.strip().upper() for p in re.split(r"[,|\s]+", raw) if p.strip()}


def strip_urls(text: str) -> str:
    """去掉 URL，避免链接路径里的随机串被当成番号。"""
    return URL_RE.sub(" ", text or "")


def filter_codes(
    codes: list[str],
    allow_prefixes: str = "",
    block_prefixes: str = "",
    max_count: int = DEFAULT_MAX_PER_MESSAGE,
) -> tuple[list[str], list[str]]:
    """按前缀白/黑名单筛选番号。

    白名单非空时只放行名单内的前缀；黑名单始终生效且优先于白名单。
    返回 (通过的番号, 被拒的番号)。
    """
    allow = _parse_list(allow_prefixes)
    block = _parse_list(block_prefixes)

    passed: list[str] = []
    rejected: list[str] = []
    for code in codes:
        prefix = _prefix(code)
        if prefix in block:
            rejected.append(code)
        elif allow and prefix not in allow:
            rejected.append(code)
        elif max_count and len(passed) >= max_count:
            rejected.append(code)
        else:
            passed.append(code)
    return passed, rejected


def extract_subscribable_codes(
    text: str,
    allow_prefixes: str = "",
    block_prefixes: str = "",
    max_count: int = DEFAULT_MAX_PER_MESSAGE,
) -> tuple[list[str], list[str]]:
    """从消息文本中提取可订阅的番号。

    返回 (通过的番号, 被过滤掉的番号)。两者都为空说明消息里没有番号。
    """
    codes = find_serial_numbers(strip_urls(text))
    if not codes:
        return [], []

    passed, rejected = filter_codes(codes, allow_prefixes, block_prefixes, max_count)
    if rejected:
        logger.info(f"消息中 {len(rejected)} 个番号被过滤: {', '.join(rejected)}")
    return passed, rejected


def normalize_explicit_codes(
    argument: str,
    allow_prefixes: str = "",
    block_prefixes: str = "",
    max_count: int = DEFAULT_MAX_PER_MESSAGE,
) -> tuple[list[str], list[str]]:
    """处理 /sub、/cancel 的参数。

    显式指令里用户可能一次给多个番号，也可能给的是不带分隔符的写法。
    先按番号识别；识别不到再退回 get_true_code 兜底单个输入。
    """
    codes = find_serial_numbers(strip_urls(argument))
    if not codes:
        single = get_true_code(argument)
        codes = [single] if single else []
    if not codes:
        return [], []
    return filter_codes(codes, allow_prefixes, block_prefixes, max_count)
