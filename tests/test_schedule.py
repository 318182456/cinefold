"""自然语言排班：说法 → cron，以及 cron → 人话的回读。

任务页让人用大白话改执行周期，这里覆盖的是那条翻译链路。用例值得写密
一点：排班翻错了不会抛异常、不会报错，只会在某个没人盯着的时刻悄悄跑
或者悄悄不跑，事后极难发现。

所有用例都关掉 AI（allow_ai=False）—— 测的是规则本身认不认得，
不能让结果取决于外部服务通不通、模型今天心情如何。
"""
from __future__ import annotations

import pytest

from app.services.schedule import (
    describe_cron,
    describe_interval,
    is_cron,
    parse_interval,
    parse_schedule,
)


def _cron(text: str) -> str | None:
    """按规则翻一句话，只返回 cron。认不出返回 None。"""
    result = parse_schedule(text, allow_ai=False)
    return result[0] if result else None


# ----------------------------------------------------------------------
# 间隔类说法
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("每小时", "0 * * * *"),
        ("每2小时", "0 */2 * * *"),
        ("每两小时", "0 */2 * * *"),
        ("每 2 小时", "0 */2 * * *"),          # 空格不该影响
        ("每12小时", "0 */12 * * *"),
        ("每30分钟", "*/30 * * * *"),
        ("每 5 分钟", "*/5 * * * *"),
        ("每五分钟", "*/5 * * * *"),
        ("每半小时", "*/30 * * * *"),
        ("每分钟", "* * * * *"),
        ("每隔20分钟", "*/20 * * * *"),        # 「每隔」等同「每」
    ],
)
def test_interval_phrases(text, expected):
    assert _cron(text) == expected


# ----------------------------------------------------------------------
# 每天 / 时段
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("每天", "0 0 * * *"),
        ("每天凌晨4点", "0 4 * * *"),
        ("每天凌晨四点", "0 4 * * *"),
        ("每日凌晨4点", "0 4 * * *"),
        ("每天早上9点", "0 9 * * *"),
        ("每天8点20分", "20 8 * * *"),
        ("每天下午3点", "0 15 * * *"),         # 下午要 +12
        ("每天下午三点半", "30 15 * * *"),      # 「半」= 30 分
        ("每天晚上8点", "0 20 * * *"),
        ("晚上8点", "0 20 * * *"),             # 省略「每天」也认
        ("中午12点", "0 12 * * *"),
        ("每天中午12点半", "30 12 * * *"),
    ],
)
def test_daily_phrases(text, expected):
    assert _cron(text) == expected


def test_midnight_is_zero_not_twentyfour():
    """「晚上12点」说的是 0 点。

    +12 的规则套到 12 点上会算出 24，而 cron 里没有 24 点 ——
    CronTrigger 会直接拒绝，用户只会看到一句莫名其妙的失败。
    """
    assert _cron("每天晚上12点") == "0 0 * * *"
    assert is_cron(_cron("每天晚上12点"))


def test_afternoon_twelve_stays_twelve():
    """「下午12点」是 12 点，不是 24 点也不是 0 点。"""
    assert _cron("每天下午12点") == "0 12 * * *"


# ----------------------------------------------------------------------
# 每周 / 每月 / 工作日
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("每周一早上9点", "0 9 * * 1"),
        ("每周五晚上8点", "0 20 * * 5"),
        ("每周日", "0 0 * * 0"),               # 周日是 0
        ("每周天", "0 0 * * 0"),
        ("每星期三下午2点", "0 14 * * 3"),
        ("每礼拜六中午12点", "0 12 * * 6"),
        ("每月1号凌晨2点", "0 2 1 * *"),
        ("每月15日早上6点", "0 6 15 * *"),
        ("工作日早上9点", "0 9 * * 1-5"),
        ("周末中午12点", "0 12 * * 0,6"),
    ],
)
def test_weekly_monthly_phrases(text, expected):
    assert _cron(text) == expected


# ----------------------------------------------------------------------
# 直接写 cron
# ----------------------------------------------------------------------
def test_raw_cron_passes_through():
    """会写 cron 的人应该还能直接写，不该被逼着说白话。"""
    assert _cron("0 4 * * *") == "0 4 * * *"
    assert _cron("*/15 * * * *") == "*/15 * * * *"
    # 多余空格会被归一
    assert _cron("0   4  *  *  *") == "0 4 * * *"


def test_invalid_input_returns_none():
    """认不出来就得返回 None，绝不能猜一个塞进去。"""
    for text in ["", "   ", "随便什么时候", "每 99 小时", "每天25点", "asdf"]:
        assert parse_schedule(text, allow_ai=False) is None, text


def test_every_parsed_result_is_valid_cron():
    """规则产出的每一条都必须能被 APScheduler 收下。

    产出一个语法不合法的表达式比认不出来更糟：认不出来当场报错，
    不合法的表达式要等到重启调度器那一刻才炸。
    """
    samples = [
        "每小时", "每2小时", "每30分钟", "每天", "每天凌晨4点", "每天下午三点半",
        "每周一早上9点", "每周日", "每月1号凌晨2点", "工作日早上9点", "周末中午12点",
        "每天晚上12点", "每半小时", "每分钟",
    ]
    for text in samples:
        cron = _cron(text)
        assert cron is not None, text
        assert is_cron(cron), f"{text} -> {cron}"


# ----------------------------------------------------------------------
# 回读
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "cron, expected",
    [
        ("0 * * * *", "每小时"),
        ("35 * * * *", "每小时的第 35 分"),
        ("*/30 * * * *", "每 30 分钟"),
        ("0 */2 * * *", "每 2 小时"),
        ("0 4 * * *", "每天 04:00"),
        ("30 15 * * *", "每天 15:30"),
        ("0 9 * * 1", "每周一 09:00"),
        ("0 9 * * 1-5", "工作日 09:00"),
        ("0 12 * * 0,6", "周末 12:00"),
        ("0 2 1 * *", "每月 1 号 02:00"),
    ],
)
def test_describe_cron(cron, expected):
    assert describe_cron(cron) == expected


def test_describe_cron_falls_back_to_raw():
    """翻不出来就原样显示表达式 —— 显示原文好过显示一句错的解释。"""
    weird = "0 0 1 1 1"
    assert describe_cron(weird) == weird
    assert describe_cron("") == "未排班"


def test_roundtrip_is_stable():
    """人话 → cron → 人话，再翻回去应当还是同一个 cron。

    这条守的是界面上的编辑流程：页面显示的是回读文本，用户点「改周期」
    时预填的也是它。要是回读出来的说法翻不回原来的 cron，用户什么都没
    改、只点了保存，排班就悄悄变了。
    """
    for text in [
        "每小时", "每2小时", "每30分钟", "每天凌晨4点", "每天下午三点半",
        "每周一早上9点", "工作日早上9点", "每月1号凌晨2点", "每分钟",
        "每天晚上8点", "周末中午12点", "每小时的第35分", "每天8点20分",
        # 这几条是项目里实际在用的默认值，回读之后必须还能翻回去
        "0 */2 * * *", "10 */2 * * *", "30 */12 * * *", "*/25 * * * *",
    ]:
        cron = _cron(text)
        readable = describe_cron(cron)
        again = _cron(readable)
        assert again == cron, f"{text} -> {cron} -> {readable} -> {again}"


# ----------------------------------------------------------------------
# 固定间隔任务
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, minutes",
    [
        ("每5分钟", 5),
        ("每 10 分钟", 10),
        ("每小时", 60),
        ("每2小时", 120),
        ("每半小时", 30),
        ("30分钟", 30),
        ("每6小时", 360),
    ],
)
def test_parse_interval(text, minutes):
    assert parse_interval(text) == minutes


def test_parse_interval_rejects_nonsense():
    """认不出的、以及「几点几分」这类定时说法，都必须拒掉。

    「每天凌晨4点」对固定间隔任务没有意义：interval 触发器只认间隔，
    把它折算成某个分钟数收下，只会变成一个和用户预期完全不同的排班 ——
    而界面上还会显示成功。宁可拒绝，让用户换个说法。
    """
    for text in ["", "   ", "随便", "每天凌晨4点", "每周一早上9点"]:
        assert parse_interval(text) is None, text


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (5, "每 5 分钟"),
        (30, "每 30 分钟"),
        (60, "每小时"),
        (120, "每 2 小时"),
        (360, "每 6 小时"),
        (90, "每 1 小时 30 分"),
        (0, "未排班"),
    ],
)
def test_describe_interval(minutes, expected):
    assert describe_interval(minutes) == expected


def test_interval_roundtrip():
    """分钟数 → 人话 → 分钟数，同样要能原样翻回来。"""
    for minutes in [5, 10, 20, 30, 60, 120, 360]:
        assert parse_interval(describe_interval(minutes)) == minutes
