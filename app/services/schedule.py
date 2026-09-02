"""把自然语言的排班说法翻成 cron，以及把 cron 翻回人话。

任务页要让人直接改触发时间，而 cron 表达式不是所有人都会写 ——
「35 * * * *」看不出是「每小时」，想改成「每两小时」更无从下手。
所以输入统一收自然语言（「每小时」「每天凌晨4点」「每20分钟」），
由这里翻成 cron 存进配置。

两条路径，规则优先：

    parse_schedule("每天凌晨4点")  →  ("0 4 * * *", "规则")
    parse_schedule("工作日下午三点半跑一次")  →  AI 兜底

规则必须排在 AI 前面，而不是图省事全丢给 AI：

  * 常用说法就那么几十种，正则一次匹中，不必为「每小时」发一次网络请求
    再等一秒钟；
  * AI 没配、网关抽风、模型抽风的时候，改排班这件事不能跟着一起坏掉 ——
    它是配置页的基础功能，不该依赖一个可选组件。

所以 AI 只接规则没认出来的那部分说法。两条路都没认出来时返回 None，
由调用方提示用户换个说法，绝不猜一个 cron 塞进去 —— 排班猜错了不会
报错，只会在某个没人看的时刻悄悄跑或者悄悄不跑。
"""
from __future__ import annotations

import re

from loguru import logger

# ----------------------------------------------------------------------
# 中文数字
# ----------------------------------------------------------------------
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """把「十」「二十」「二十三」这类中文数字转成整数。认不出返回 None。

    只覆盖 0-99：排班里出现的数字不外乎小时（0-23）、分钟（0-59）、
    星期（1-7），用不着更大的量级。
    """
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    if "十" not in text:
        # 单字，或者「二三」这种不规范写法——只认单字
        return _CN_DIGITS.get(text) if len(text) == 1 else None

    before, _, after = text.partition("十")
    # 「十」= 10，「二十」= 20，「十五」= 15，「二十五」= 25
    tens = 1 if before == "" else _CN_DIGITS.get(before)
    ones = 0 if after == "" else _CN_DIGITS.get(after)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


# 数字：阿拉伯数字或中文数字都收
_NUM = r"(\d{1,2}|[零〇一两二三四五六七八九十]{1,3})"


def _num(raw: str) -> int | None:
    return _cn_to_int(raw)


# ----------------------------------------------------------------------
# 时段 → 小时的偏移
# ----------------------------------------------------------------------
# 「下午三点」得是 15 点。中午 12 点和下午 12 点都按 12 处理，
# 「凌晨」「早上」本身就是 24 小时制的前半段，不做偏移
def _apply_period(hour: int, period: str) -> int:
    if not period:
        return hour
    if period in ("下午", "傍晚", "晚上", "晚"):
        # 「晚上12点」说的是 0 点，不是 24 点
        if hour == 12:
            return 12 if period == "下午" else 0
        return hour + 12 if hour < 12 else hour
    if period in ("中午",):
        return 12 if hour == 12 else hour
    if period in ("半夜",):
        return 0 if hour == 12 else hour
    return hour


_PERIOD = r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚|半夜|夜里)?"


# ----------------------------------------------------------------------
# 星期
# ----------------------------------------------------------------------
_WEEKDAYS = {
    "一": 1, "1": 1, "二": 2, "2": 2, "三": 3, "3": 3, "四": 4, "4": 4,
    "五": 5, "5": 5, "六": 6, "6": 6, "日": 0, "天": 0, "7": 0, "0": 0,
}


def _clean(text: str) -> str:
    """归一化：全角转半角、去空白、统一常见异体写法。"""
    if not text:
        return ""
    # 全角数字与冒号
    trans = str.maketrans(
        "０１２３４５６７８９：　",
        "0123456789: ",
    )
    out = text.translate(trans).strip()
    out = re.sub(r"\s+", "", out)
    # 「每隔」「每过」都当「每」；「钟」「整」是语气词
    out = out.replace("每隔", "每").replace("每过", "每")
    out = out.replace("分钟", "分").replace("小时", "时")
    out = out.replace("点整", "点").replace("点钟", "点")
    # 「半小时」= 30 分。放在 分钟→分 / 小时→时 的替换之后，
    # 这时它已经变成「半时」了
    out = out.replace("半时", "30分")
    return out


# ----------------------------------------------------------------------
# 规则表
# ----------------------------------------------------------------------
def _rule_parse(text: str) -> str | None:
    """按规则把自然语言转成 cron。认不出返回 None。

    规则的先后有讲究：带「每天」的必须排在裸时刻前面，否则
    「每天凌晨4点」会被裸时刻那条先吃掉（结果一样，但语义匹配更清楚）。
    """
    s = _clean(text)
    if not s:
        return None

    # 已经是 cron 就原样收下——高级用户仍然可以直接写
    if is_cron(text.strip()):
        return " ".join(text.strip().split())

    # --- 每 N 分钟 ---
    m = re.fullmatch(rf"每{_NUM}分(一次|钟一次|执行一次)?", s)
    if m:
        n = _num(m.group(1))
        if n and 1 <= n <= 59:
            return f"*/{n} * * * *"
        if n == 60:
            return "0 * * * *"
        return None

    # 「每分钟」= 每 1 分钟
    if re.fullmatch(r"每分(一次)?", s):
        return "* * * * *"

    # --- 每 N 小时 ---
    m = re.fullmatch(rf"每{_NUM}时(一次|执行一次)?", s)
    if m:
        n = _num(m.group(1))
        if n and 1 <= n <= 23:
            # 每 N 小时的第 0 分跑。不用 :35 那种错峰，用户说每 N 小时
            # 就给整点，要错峰他会自己说「每2小时的第10分」
            return f"0 */{n} * * *" if n > 1 else "0 * * * *"
        if n == 24:
            return "0 0 * * *"
        return None

    # 「每小时」= 每 1 小时
    if re.fullmatch(r"每时(一次)?", s):
        return "0 * * * *"

    # --- 每小时的第 N 分 ---
    m = re.fullmatch(rf"每时(的)?第?{_NUM}分(一次)?", s)
    if m:
        n = _num(m.group(2))
        if n is not None and 0 <= n <= 59:
            return f"{n} * * * *"
        return None

    # --- 每天 / 每日 [时段] N 点[N 分] ---
    m = re.fullmatch(
        rf"(每天|每日|天天)?{_PERIOD}{_NUM}点(半|{_NUM}分?)?(一次|执行一次)?",
        s,
    )
    if m:
        hour = _num(m.group(3))
        if hour is None:
            return None
        hour = _apply_period(hour, m.group(2) or "")
        minute = _parse_minute_part(m.group(4))
        if minute is None or not (0 <= hour <= 23):
            return None
        return f"{minute} {hour} * * *"

    # --- 每天 HH:MM ---
    # 界面上的回读就是这个格式（「每天 04:00」），而用户点「改周期」时
    # 输入框预填的正是回读文本 —— 认不出它，就会出现「什么都没改、
    # 点保存却报错」这种莫名其妙的情况
    m = re.fullmatch(r"(每天|每日|天天)?(\d{1,2}):(\d{2})(一次|执行一次)?", s)
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * *"
        return None

    # --- 每周 X HH:MM / 工作日 HH:MM / 周末 HH:MM ---
    m = re.fullmatch(
        r"(每周|每星期|每礼拜)([一二三四五六日天0-7])(\d{1,2}):(\d{2})", s
    )
    if m:
        dow = _WEEKDAYS.get(m.group(2))
        hour, minute = int(m.group(3)), int(m.group(4))
        if dow is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * {dow}"
        return None

    m = re.fullmatch(r"(工作日|周末)(\d{1,2}):(\d{2})", s)
    if m:
        dow = "1-5" if m.group(1) == "工作日" else "0,6"
        hour, minute = int(m.group(2)), int(m.group(3))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * {dow}"
        return None

    # --- 每月 N 号 HH:MM ---
    m = re.fullmatch(r"每月{}(号|日)(\d{{1,2}}):(\d{{2}})".format(_NUM), s)
    if m:
        day = _num(m.group(1))
        hour, minute = int(m.group(3)), int(m.group(4))
        if day and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} {day} * *"
        return None

    # --- 每 N 小时第 M 分 ---
    m = re.fullmatch(rf"每{_NUM}时第{_NUM}分", s)
    if m:
        n, minute = _num(m.group(1)), _num(m.group(2))
        if n and 1 <= n <= 23 and minute is not None and 0 <= minute <= 59:
            return f"{minute} */{n} * * *"
        return None

    # --- 每天（没说几点）---
    if re.fullmatch(r"(每天|每日|天天)(一次|执行一次)?", s):
        # 不猜时刻：凌晨 0 点是最没有歧义的「每天」
        return "0 0 * * *"

    # --- 每周 X [时段] N 点 ---
    m = re.fullmatch(
        rf"(每周|每星期|每礼拜)([一二三四五六日天0-7])"
        rf"(的)?{_PERIOD}(?:{_NUM}点(半|{_NUM}分?)?)?(一次|执行一次)?",
        s,
    )
    if m:
        dow = _WEEKDAYS.get(m.group(2))
        if dow is None:
            return None
        hour_raw = m.group(5)
        hour = _num(hour_raw) if hour_raw else 0
        if hour is None:
            return None
        hour = _apply_period(hour, m.group(4) or "")
        minute = _parse_minute_part(m.group(6)) if hour_raw else 0
        if minute is None or not (0 <= hour <= 23):
            return None
        return f"{minute} {hour} * * {dow}"

    # --- 每月 N 号 [时段] N 点 ---
    m = re.fullmatch(
        rf"每月{_NUM}(号|日)(的)?{_PERIOD}(?:{_NUM}点(半|{_NUM}分?)?)?(一次|执行一次)?",
        s,
    )
    if m:
        day = _num(m.group(1))
        if day is None or not (1 <= day <= 31):
            return None
        hour_raw = m.group(5)
        hour = _num(hour_raw) if hour_raw else 0
        if hour is None:
            return None
        hour = _apply_period(hour, m.group(4) or "")
        minute = _parse_minute_part(m.group(6)) if hour_raw else 0
        if minute is None or not (0 <= hour <= 23):
            return None
        return f"{minute} {hour} {day} * *"

    # --- 工作日 / 周末 ---
    m = re.fullmatch(
        rf"(工作日|周末)(的)?{_PERIOD}(?:{_NUM}点(半|{_NUM}分?)?)?(一次|执行一次)?", s
    )
    if m:
        dow = "1-5" if m.group(1) == "工作日" else "0,6"
        hour_raw = m.group(4)
        hour = _num(hour_raw) if hour_raw else 0
        if hour is None:
            return None
        hour = _apply_period(hour, m.group(3) or "")
        minute = _parse_minute_part(m.group(5)) if hour_raw else 0
        if minute is None or not (0 <= hour <= 23):
            return None
        return f"{minute} {hour} * * {dow}"

    return None


def _parse_minute_part(raw: str | None) -> int | None:
    """解析「点」后面那截：None/空 → 0，「半」→ 30，「20分」→ 20。"""
    if not raw:
        return 0
    if raw == "半":
        return 30
    value = _num(raw.rstrip("分"))
    if value is None or not (0 <= value <= 59):
        return None
    return value


# ----------------------------------------------------------------------
# cron 校验与回译
# ----------------------------------------------------------------------
def is_cron(text: str) -> bool:
    """是不是一个合法的 5 段 cron。"""
    parts = text.strip().split()
    if len(parts) != 5:
        return False
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(text.strip())
        return True
    except (ValueError, TypeError):
        return False


_DOW_CN = {
    "0": "周日", "1": "周一", "2": "周二", "3": "周三",
    "4": "周四", "5": "周五", "6": "周六", "7": "周日",
}


def describe_cron(cron: str) -> str:
    """把 cron 翻回人话，给界面显示。

    翻不出来就原样返回表达式 —— 显示原文总好过显示一句错的解释。
    这里只覆盖本项目实际会用到的形态，不做通用 cron 的完整叙述。
    """
    cron = (cron or "").strip()
    if not cron:
        return "未排班"
    parts = cron.split()
    if len(parts) != 5:
        return cron

    minute, hour, day, month, dow = parts

    def _hm(h: str, mi: str) -> str:
        try:
            return f"{int(h):02d}:{int(mi):02d}"
        except ValueError:
            return f"{h}:{mi}"

    # 每 N 分钟
    if minute.startswith("*/") and hour == "*" and day == "*" and dow == "*":
        return f"每 {minute[2:]} 分钟"
    if minute == "*" and hour == "*" and day == "*" and dow == "*":
        return "每分钟"

    # 每 N 小时
    if minute.isdigit() and hour.startswith("*/") and day == "*" and dow == "*":
        at = "" if minute == "0" else f"第 {int(minute)} 分"
        return f"每 {hour[2:]} 小时{at}"

    # 每小时
    if minute.isdigit() and hour == "*" and day == "*" and dow == "*":
        return "每小时" if minute == "0" else f"每小时的第 {int(minute)} 分"

    # 每周
    if minute.isdigit() and hour.isdigit() and day == "*" and dow != "*":
        when = _hm(hour, minute)
        if dow == "1-5":
            return f"工作日 {when}"
        if dow in ("0,6", "6,0"):
            return f"周末 {when}"
        names = [_DOW_CN.get(d.strip(), d.strip()) for d in dow.split(",")]
        if all(n.startswith("周") for n in names):
            return f"每{'、'.join(names)} {when}"
        return cron

    # 每月某日
    if minute.isdigit() and hour.isdigit() and day.isdigit() and dow == "*":
        return f"每月 {int(day)} 号 {_hm(hour, minute)}"

    # 每天
    if minute.isdigit() and hour.isdigit() and day == "*" and month == "*" and dow == "*":
        return f"每天 {_hm(hour, minute)}"

    # 每天多个时刻，如 "30 */12 * * *" 之外的 "0 8,20 * * *"
    if minute.isdigit() and "," in hour and day == "*" and dow == "*":
        times = "、".join(_hm(h, minute) for h in hour.split(","))
        return f"每天 {times}"

    return cron


# ----------------------------------------------------------------------
# AI 兜底
# ----------------------------------------------------------------------
_AI_PROMPT = """你是一个 crontab 表达式生成器。用户用自然语言描述执行周期，
你输出对应的 5 段 crontab 表达式（分 时 日 月 周）。

规则：
- 只输出表达式本身，不要解释、不要代码块、不要引号。
- 星期用 0-6，0 表示周日。
- 无法理解时只输出 UNKNOWN 这一个词。
- 用户没说具体时刻的，分钟位取 0，不要自己发挥。

示例：
每小时 -> 0 * * * *
每两小时 -> 0 */2 * * *
每天凌晨三点 -> 0 3 * * *
每周一早上九点半 -> 30 9 * * 1
工作日中午 -> 0 12 * * 1-5"""


def _ai_parse(text: str) -> str | None:
    """规则认不出来时，问 AI。AI 没配或答得不对都返回 None。"""
    try:
        from app.core.config import get_settings
        from app.modules.review.reviewai import _config_of, _pick_provider, _usable

        settings = get_settings()
        _, config = _pick_provider(settings)
        if not _usable(config):
            logger.debug("[排班] AI 未配置，跳过智能解析")
            return None

        import httpx

        endpoint = config["url"]
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        # trust_env=False 的理由同 translateai：AI 网关常挂在内网，
        # 系统代理会把这类请求吞掉
        with httpx.Client(
            timeout=20, proxy=settings.proxy or None, trust_env=False
        ) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config["model"],
                    "messages": [
                        {"role": "system", "content": _AI_PROMPT},
                        {"role": "user", "content": text.strip()},
                    ],
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            choices = response.json().get("choices") or []

        if not choices:
            return None
        raw = ((choices[0] or {}).get("message") or {}).get("content") or ""
        # 模型爱套代码围栏，剥掉
        raw = re.sub(r"```[a-z]*|```", "", raw).strip().strip("`\"'")
        if not raw or "UNKNOWN" in raw.upper():
            return None

        # 模型可能多说一句，取看起来像 cron 的那一行
        for line in raw.splitlines():
            line = line.strip()
            if is_cron(line):
                return " ".join(line.split())
        return None
    except Exception as exc:
        # AI 不通不该让改排班这件事整个失败，退回「没认出来」
        logger.warning(f"[排班] AI 解析异常: {exc}")
        return None


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def parse_schedule(text: str, allow_ai: bool = True) -> tuple[str, str] | None:
    """把一句话翻成 cron，返回 (cron, 来源)。认不出返回 None。

    来源是「规则」或「AI」，只用于日志与界面提示，让用户知道这条是怎么
    定下来的 —— AI 认的那条更值得他扫一眼确认。
    """
    if not text or not text.strip():
        return None

    cron = _rule_parse(text)
    if cron:
        return cron, "规则"

    if allow_ai:
        cron = _ai_parse(text)
        if cron:
            logger.info(f"[排班] AI 把「{text.strip()}」解析为 {cron}")
            return cron, "AI"

    return None


def parse_interval(text: str) -> int | None:
    """把一句话翻成「多少分钟」，给固定间隔的任务用。认不出返回 None。

    固定间隔任务（同步下载状态、翻译标题等）走的是 APScheduler 的
    interval 触发器，配置里存的是分钟数而不是 cron，所以单开一个入口。
    """
    if not text or not text.strip():
        return None

    s = _clean(text)

    m = re.fullmatch(rf"每?{_NUM}分(一次|钟一次|执行一次)?", s)
    if m:
        n = _num(m.group(1))
        return n if n and n > 0 else None

    m = re.fullmatch(rf"每?{_NUM}时(一次|执行一次)?", s)
    if m:
        n = _num(m.group(1))
        return n * 60 if n and n > 0 else None

    if re.fullmatch(r"每?时(一次)?", s):
        return 60
    if re.fullmatch(r"每?分(一次)?", s):
        return 1

    # 说法没命中就借道 cron 那套：「每半小时」这类能翻成 */30 的，
    # 换算回分钟数照样能用
    parsed = parse_schedule(text)
    if not parsed:
        return None
    minute, hour = parsed[0].split()[:2]
    if minute.startswith("*/") and hour == "*":
        return int(minute[2:])
    if minute.isdigit() and hour.startswith("*/"):
        return int(hour[2:]) * 60
    if minute.isdigit() and hour == "*":
        return 60
    return None


def describe_interval(minutes: int) -> str:
    """把分钟数说成人话。"""
    if minutes <= 0:
        return "未排班"
    if minutes < 60:
        return f"每 {minutes} 分钟"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "每小时" if hours == 1 else f"每 {hours} 小时"
    return f"每 {minutes // 60} 小时 {minutes % 60} 分"
