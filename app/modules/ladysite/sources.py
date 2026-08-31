"""内置数据源清单与配置读写。

地址落库而非写死在代码里：这些站换域名很频繁，改配置比改代码快。

`bypass_first` 标了 True 的站直连必定吃 403（Cloudflare 挑战页），
必须配 BYPASS_URL 才能抓；没配的话这些源开着也拿不到数据。
`parser` 指向已实现的解析器，为空表示尚未接入解析，只能做连通性测试。

各源的覆盖分工（抓取按 DETAIL_SITES 的顺序取最快返回的结果）：
- 有码主力：javbus、javdb、jav321、avbase、avmoo
- 无码/素人：avsox、carib（日期型番号）、mgstage
- 官方一手数据：dmm、mgstage
- 中文标题：missav、airav、7mmtv、xchina
- FC2 专属：fc2、fc2hub —— 其余源对 FC2-PPV 基本无覆盖
- 国产：madou、madouqu
"""
from __future__ import annotations

import re
from functools import lru_cache

from loguru import logger
from sqlalchemy import select

from app.database.session import session_scope

# 日期型番号：032416_267。与 utils.get_true_code 的归一化结果对齐
_DATE_CODE_RE = re.compile(r"^\d{6}[-_]\d{2,4}$")

# key, 显示名, 默认地址, 解析器, 直连是否必被拦, 番号路由规则
#
# 番号前缀分组。前缀比对是"整段匹配"（MD 不会命中 MDBK，见 _matches_token），
# 所以每个厂牌前缀都要列全，不能靠 MD 顺带覆盖 MDBK
_DOMESTIC = "MD,MDX,MDBK,MDSJ,MDCM,MSD,MKY,MTVQ,TM,TMW,PME,JD,JDBC,91CM,91BCM,RS,XSJ,LY"
_UNCENSORED = ("HEYZO,CARIB,1PONDO,10MUSUME,PACO,PACOPACOMAMA,MURAMURA,"
               "GACHINCO,TOKYOHOT,KIN8,XXXAV,C0930,H0930,H4610")
_AMATEUR = ("SIRO,GANA,LUXU,MIUM,ARA,KNB,SCUTE,MAAN,CHN,ABW,"
            "300MIUM,200GANA,259LUXU,261ARA,336KNB,390JAC")

# 日系有码源共用的排除规则：FC2 个人投稿、日期型与厂牌无码、国产片，
# 这几类在这些站上基本查不到，问了也是白跑一次请求 + 占一个并发位。
# javbus/javdb 对素人番号（SIRO 等）有收录，因此不排除素人段
_SKIP_NON_JAV = f"skip:FC2,date,{_UNCENSORED},{_DOMESTIC}"

SOURCES: tuple[dict, ...] = (
    {"key": "javbus", "name": "JavBus", "host": "https://www.javbus.com",
     "parser": "bus", "bypass_first": False, "code_rule": _SKIP_NON_JAV},
    {"key": "javdb", "name": "JavDB", "host": "https://javdb.com",
     "parser": "javdb", "bypass_first": False, "interval": 10.0,
     "code_rule": _SKIP_NON_JAV},
    {"key": "javlibrary", "name": "JavLibrary", "host": "https://www.javlibrary.com/cn",
     "parser": "library", "bypass_first": True, "code_rule": _SKIP_NON_JAV},
    {"key": "airav", "name": "Airav", "host": "https://airav.io/cn",
     "parser": "airav", "bypass_first": True, "interval": 2.0,
     "code_rule": _SKIP_NON_JAV},
    {"key": "avbase", "name": "Avbase", "host": "https://www.avbase.net",
     "parser": "avbase", "bypass_first": True, "interval": 2.0,
     "code_rule": _SKIP_NON_JAV},
    # 与 javbus 同模板同收录范围
    {"key": "avmoo", "name": "Avmoo", "host": "https://avmoo.website",
     "parser": "avmoo", "bypass_first": False, "interval": 2.0,
     "code_rule": _SKIP_NON_JAV},
    # 素人与无码为主：日期型番号、HEYZO/1PONDO 这类无码厂牌，以及素人段
    {"key": "avsox", "name": "Avsox", "host": "https://avsox.click",
     "parser": "avsox", "bypass_first": False, "interval": 2.0,
     "code_rule": f"only:date,{_UNCENSORED},{_AMATEUR}"},
    # 只收日期型番号（032416_267），普通番号一律 404
    {"key": "carib", "name": "Caribbeancom", "host": "https://www.caribbeancom.com",
     "parser": "carib", "bypass_first": False, "interval": 2.0,
     "code_rule": "only:date"},
    # 日系官方站，不收 FC2 个人投稿、无码与国产片
    {"key": "dmm", "name": "DMM", "host": "https://www.dmm.co.jp",
     "parser": "dmm", "bypass_first": False, "interval": 2.0,
     "code_rule": _SKIP_NON_JAV},
    # 只收自家投稿，反之 FC2 番号在日系源上基本查不到
    {"key": "fc2", "name": "FC2", "host": "https://adult.contents.fc2.com",
     "parser": "fc2", "bypass_first": False, "interval": 2.0,
     "code_rule": "only:FC2"},
    {"key": "fc2hub", "name": "FC2 Hub", "host": "https://javten.com",
     "parser": "fc2hub", "bypass_first": True, "interval": 2.0,
     "code_rule": "only:FC2"},
    # 有解析器但数据会串号，默认停用；详见 freejavbt.py 的说明
    {"key": "freejavbt", "name": "FreeJavBT", "host": "https://freejavbt.com",
     "parser": "freejavbt", "bypass_first": False, "interval": 2.0, "enabled": False},
    {"key": "hbox", "name": "Hbox", "host": "https://hbox.jp",
     "parser": "hbox", "bypass_first": False, "interval": 2.0,
     "code_rule": _SKIP_NON_JAV},
    {"key": "jav321", "name": "Jav321", "host": "https://www.jav321.com",
     "parser": "jav321", "bypass_first": False, "interval": 1.5,
     "code_rule": _SKIP_NON_JAV},
    # 国产厂牌（麻豆、天美、蜜桃、精东等）
    {"key": "madou", "name": "Madou", "host": "https://madou.club",
     "parser": "madou", "bypass_first": False, "interval": 2.0,
     "code_rule": f"only:{_DOMESTIC}"},
    {"key": "madouqu", "name": "Madouqu", "host": "https://madouqu.com",
     "parser": "madouqu", "bypass_first": True, "interval": 2.0,
     "code_rule": f"only:{_DOMESTIC}"},
    # 素人系官方站，这类番号在二手站上信息往往缺失或错乱
    {"key": "mgstage", "name": "MGStage", "host": "https://www.mgstage.com",
     "parser": "mgstage", "bypass_first": True, "interval": 2.0,
     "code_rule": f"only:{_AMATEUR}"},
    # 收录面广（有码、FC2、国产都有），只排掉日期型无码
    {"key": "missav", "name": "MissAV", "host": "https://missav123.com",
     "parser": "missav", "bypass_first": True, "interval": 2.0,
     "code_rule": "skip:date"},
    {"key": "7mmtv", "name": "7mmTV", "host": "https://7mmtv.sx/zh",
     "parser": "7mmtv", "bypass_first": True, "interval": 2.0,
     "code_rule": "skip:date"},
    # 需要 API Token 才能用（填在 Cookie 栏），没配等于抓不到，默认停用
    {"key": "theporndb", "name": "ThePornDB", "host": "https://api.theporndb.net",
     "parser": "theporndb", "bypass_first": False, "interval": 1.5, "enabled": False},
    {"key": "xchina", "name": "XChina", "host": "https://xchina.co",
     "parser": "xchina", "bypass_first": False, "interval": 2.0,
     "code_rule": "skip:date"},

    # --- 字幕源（kind="subtitle"）---
    # 登记在这里只为让用户能在页面上改域名与开关 —— 字幕站换域名同样频繁。
    # parser 留空是有意的：详情抓取只认有 parser 的源
    # （enabled_parser_sources），留空才不会被拉进查番号详情那条链路。
    # 解析实现在 modules/subtitle 下，按 key 取地址
    {"key": "javsub", "name": "JavSub.ai（字幕）",
     "host": "https://javsub.ai", "kind": "subtitle",
     "parser": "", "bypass_first": True, "interval": 2.0},
    {"key": "subtitlecat", "name": "SubtitleCat（字幕）",
     "host": "https://www.subtitlecat.com", "kind": "subtitle",
     "parser": "", "bypass_first": False, "interval": 2.0},
    # 不给默认地址：此前那个默认值指向一个压根不存在的仓库，兜底源因此
    # 从一开始就在空转（每次取都是 404，日志只落在 debug 级，看不出来）。
    # 与其再猜一个仓库名，不如让「没配地址」显式地等于「没启用」。
    # 可以填多个，用逗号或换行分隔，见 modules/subtitle/github.py
    {"key": "subtitlegh", "name": "GitHub 字幕库",
     "host": "", "kind": "subtitle",
     "parser": "", "bypass_first": False, "interval": 0.5},
)

# 番号详情源。字幕源与之共用这张表（同样需要页面上改地址），但不参与
# 详情抓取，凡是遍历"抓详情的源"的地方都该用这个而非 SOURCES
DETAIL_SOURCES: tuple[dict, ...] = tuple(
    item for item in SOURCES if item.get("kind", "detail") == "detail"
)

SUBTITLE_SOURCES: tuple[dict, ...] = tuple(
    item for item in SOURCES if item.get("kind") == "subtitle"
)

SOURCE_MAP: dict[str, dict] = {item["key"]: item for item in SOURCES}

# 已接入解析的源，只有这些能真正抓到数据
PARSERS: dict[str, str] = {
    item["key"]: item["parser"] for item in SOURCES if item["parser"]
}

# 不允许删除的核心源。这三个是抓取链路的主力，全删光会让整个应用
# 没有可用数据源；停用可以，删除不行
PROTECTED: frozenset[str] = frozenset({"javbus", "javdb", "javlibrary"})

# 落到这些路径说明还停在前置校验页，没真正进站。
# 过盾服务也可能返回 200 + 校验页（javbus 就是这样），因此直连与
# 过盾两条路径都要查。
VERIFY_MARKS: tuple[str, ...] = ("age_check", "age-check", "driver-verify")

# 首页挂了前置校验、但内容页正常的源：测首页会误报不可用。
# javbus 首页固定跳 /doc/driver-verify（过盾也一样），而详情页可直接抓，
# 所以拿一个真实番号页当探针。
CHECK_PATH: dict[str, str] = {"javbus": "/SSIS-001"}

# 抓取优先级的默认顺序（越靠前越优先）。不能沿用 SOURCES 的字母序：
# 首批并发位有限（见 ladysite.MAX_PARALLEL_SITES），直连可用的主力源
# 必须排在需过盾/新接入的源前面，否则字母靠前的 airav、avbase 把并发位
# 占了，可靠的 jav321、missav 反而在队列里干等
DEFAULT_ORDER: tuple[str, ...] = (
    "javbus", "javdb", "jav321", "avbase", "missav",
    "avmoo", "dmm", "airav", "7mmtv", "hbox", "xchina",
    "javlibrary", "mgstage", "avsox", "carib",
    "fc2", "fc2hub", "madou", "madouqu", "theporndb", "freejavbt",
)


def _default_priority(key: str) -> int:
    try:
        return DEFAULT_ORDER.index(key)
    except ValueError:
        return len(DEFAULT_ORDER)


# 曾作为默认值下发过、后来被修订的规则。backfill 时视同"未定制"，
# 直接替换成新默认；用户自己写的规则不会恰好等于这些串。
# 以后修订某个源的默认规则时，把旧串登记到这里，存量库才能跟上 ——
# 否则默认值的演进永远到不了已部署的实例（backfill 只补 NULL 行）
_SUPERSEDED_RULES: dict[str, tuple[str, ...]] = {}


# ----------------------------------------------------------------------
# 番号路由规则
# ----------------------------------------------------------------------
# 规则控制"哪些番号该问这个源"。抓取时多个源并发跑，拿 SSIS-001 去问只收
# FC2 的站是必然的 404 —— 白费一次请求，还占掉一个并发位（见
# ladysite.MAX_PARALLEL_SITES 的说明）。
#
# 语法（填在数据源页面的「番号规则」里）：
#   only:FC2,SIRO   只有这些前缀的番号才问这个源
#   skip:MD,MDX     这些前缀的番号不问这个源
#   date            日期型番号（032416_267）
#   only:FC2;skip:X 两条用分号分隔
# 空规则表示不限制。前缀比对在 get_true_code 归一化之后做，大小写不敏感。
_RULE_DATE = "date"


@lru_cache(maxsize=256)
def _parse_rule_cached(rule: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """解析规则文本，返回 (only, skip)。

    加缓存：每次检索要对每个 (番号, 源) 组合判定一遍，规则串是静态的，
    没必要反复 split + 正则；规则种类撑死几十条，缓存不会涨。
    """
    only: list[str] = []
    skip: list[str] = []
    if not rule:
        return (), ()

    # 分号、换行都当分隔符 —— 用户多半会换行写
    for chunk in re.split(r"[;\n]+", rule):
        chunk = chunk.strip()
        if not chunk:
            continue

        kind, _, raw = chunk.partition(":")
        kind = kind.strip().lower()
        # 没写 only:/skip: 前缀时按 only 处理，那是更常见的意图
        if kind not in ("only", "skip"):
            kind, raw = "only", chunk

        target = only if kind == "only" else skip
        for token in re.split(r"[,，\s]+", raw):
            token = token.strip().upper().rstrip("-")
            if token and token not in target:
                target.append(token)

    return tuple(only), tuple(skip)


def parse_code_rule(rule: str) -> dict[str, list[str]]:
    """把规则文本解析成 {"only": [...], "skip": [...]}。

    容错优先：这是用户在文本框里手打的，写错的部分忽略掉而不是整条报废，
    否则一个笔误会让整个源静默不参与抓取。
    """
    only, skip = _parse_rule_cached(rule or "")
    return {"only": list(only), "skip": list(skip)}


@lru_cache(maxsize=512)
def _token_re(token: str) -> "re.Pattern[str]":
    # 前缀匹配到分隔符或数字为止：MD 不该命中 MDBK，否则 skip:MD 会
    # 顺带把 MDBK 也排除掉
    return re.compile(rf"^{re.escape(token)}(?=[-_\d]|$)")


def _matches_token(code: str, token: str) -> bool:
    """番号是否命中某个前缀 token。code 应已过 get_true_code。"""
    if token == _RULE_DATE.upper():
        return bool(_DATE_CODE_RE.match(code))
    return bool(_token_re(token).match(code))


def code_allowed(code: str, rule: str) -> bool:
    """按规则判断这个番号该不该问对应的源。

    只有 only 时：命中才问。只有 skip 时：命中就不问。
    两者都有时 skip 优先 —— 排除是更强的意图。
    """
    only, skip = _parse_rule_cached(rule or "")
    if not only and not skip:
        return True

    code = (code or "").upper()
    if skip and any(_matches_token(code, t) for t in skip):
        return False
    if only:
        return any(_matches_token(code, t) for t in only)
    return True


def is_builtin(key: str) -> bool:
    """是不是内置源。不在清单里的都是用户自己加的。"""
    return key in SOURCE_MAP


def is_protected(key: str) -> bool:
    return key in PROTECTED


def sync_builtin_sources() -> int:
    """把内置源补进库。已存在的不覆盖，用户改过的地址要保留。

    被软删除的源不会重新登记 —— 否则用户删掉的源下次启动就复活了。
    要找回来走 restore_builtin_source()。
    """
    from app.database.models import DataSource

    added = 0
    with session_scope() as session:
        # 含已软删除的，这些 key 不该被当成"缺失"再插一遍
        existing = {row.key for row in session.scalars(select(DataSource)).all()}
        for item in SOURCES:
            if item["key"] in existing:
                continue
            session.add(DataSource(
                key=item["key"],
                name=item["name"],
                host=item["host"],
                # 数据质量有问题的源默认停用，由用户显式开启
                enabled=item.get("enabled", True),
                interval=item.get("interval", 0.0),
                # 按 DEFAULT_ORDER 而非 SOURCES 的字母序：首批并发位有限，
                # 直连主力必须排在需过盾/新接入的源前面（restore 同此逻辑）
                priority=_default_priority(item["key"]),
                bypass_first=item.get("bypass_first", False),
                code_rule=item.get("code_rule") or None,
            ))
            added += 1

    if added:
        logger.info(f"已登记 {added} 个内置数据源")

    backfill_builtin_rules()
    return added


def backfill_builtin_rules() -> int:
    """给存量的内置源补上默认番号规则。

    番号规则是后加的字段，老库里这些行早就存在，sync_builtin_sources 不会
    回头改它们 —— 不补的话升级上来的用户拿不到任何路由优化。

    两类行会被写：
    - NULL 的行（还没补过）补上当前默认；
    - 值恰好等于旧版默认（_SUPERSEDED_RULES）的行，替换成修订后的默认 ——
      那是我们下发的、不是用户写的，修订就该跟着到位。
    用户自己改过的（含清成空串表示"不限制"）一律不动。
    """
    from app.database.models import DataSource

    filled = 0
    with session_scope() as session:
        rows = session.scalars(select(DataSource)).all()
        for row in rows:
            default = SOURCE_MAP.get(row.key, {}).get("code_rule", "")
            if row.code_rule is None:
                if default:
                    row.code_rule = default
                    filled += 1
                continue
            if row.code_rule in _SUPERSEDED_RULES.get(row.key, ()):
                row.code_rule = default
                filled += 1

    if filled:
        logger.info(f"已为 {filled} 个内置数据源更新默认番号规则")
    return filled


def restore_builtin_source(key: str) -> bool:
    """把软删除的内置源找回来，地址等配置一并重置为默认值。

    用户删源多半是因为把配置改坏了，恢复时连带重置比只清 deleted 标记有用。
    """
    from app.database.models import DataSource

    item = SOURCE_MAP.get(key)
    if item is None:
        return False

    priority = _default_priority(key)
    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is None:
            session.add(DataSource(
                key=key,
                name=item["name"],
                host=item["host"],
                enabled=item.get("enabled", True),
                interval=item.get("interval", 0.0),
                priority=priority,
                bypass_first=item.get("bypass_first", False),
                code_rule=item.get("code_rule") or None,
            ))
            return True

        row.deleted = False
        row.name = item["name"]
        row.host = item["host"]
        row.enabled = item.get("enabled", True)
        row.interval = item.get("interval", 0.0)
        row.priority = priority
        row.bypass_first = item.get("bypass_first", False)
        row.code_rule = item.get("code_rule") or None
        # 连通性结果是删除前的旧数据，留着会误导
        row.status = None
        row.status_message = None
        row.checked_time = None

    return True


def get_source(key: str) -> dict | None:
    """取单个源的配置，库里没有则回退到内置清单。"""
    from app.database.models import DataSource

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        # 已软删除的直接当不存在，不能落到下面的内置清单兜底
        if row is not None and row.deleted:
            return None
        if row is not None:
            return {
                "key": row.key,
                "name": row.name,
                "host": row.host,
                "enabled": row.enabled,
                "interval": row.interval,
                "cookie": row.cookie or "",
                "bypass_first": row.bypass_first,
                "code_rule": row.code_rule or "",
                "parser": SOURCE_MAP.get(key, {}).get("parser", ""),
            }

    item = SOURCE_MAP.get(key)
    if item is None:
        return None
    return {
        **item,
        "enabled": True,
        "cookie": "",
        "interval": item.get("interval", 0.0),
        "code_rule": item.get("code_rule", ""),
    }


def _check_direct(url: str, timeout: float, key: str = "") -> tuple[str, str]:
    """直连测一个地址，返回 (status, message)。

    请求头复用抓取链路的 SiteClient.headers()：只发 User-Agent 会被部分站点
    当成机器人（javbus 缺 Accept-Language 就必定踢回 /doc/driver-verify），
    测试因此报不通，而真实抓取是好的。两条路径必须发一样的头。
    """
    import httpx

    from app.core.config import get_settings
    from app.modules.ladysite.base import DEFAULT_UA, SiteClient

    settings = get_settings()
    client_obj = SiteClient.from_source(key) if key else None
    # 源被停用时 from_source 返回 None，此处仍要能测，退回一份等价的头
    headers = client_obj.headers() if client_obj is not None else {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    }
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            proxy=settings.proxy or None,
            verify=False,
        ) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException:
        return "fail", f"连接超时（{timeout:.0f}s）"
    except Exception as exc:
        return "fail", f"{type(exc).__name__}: {exc}"[:200]

    code = response.status_code
    if code in (403, 503):
        return "blocked", f"HTTP {code}，被反爬拦截"
    if code >= 400:
        return "fail", f"HTTP {code}"

    final = str(response.url)
    if any(mark in final for mark in VERIFY_MARKS):
        return "blocked", f"跳转到前置校验页：{final[:80]}"
    if final.rstrip("/") != url.rstrip("/"):
        return "ok", f"跳转到 {final[:80]}"
    return "ok", ""


def _check_via_bypass(url: str) -> tuple[str, str]:
    """走过盾服务测一个地址，返回 (status, message)。

    抓取链路对 bypass_first 的站就是这么取页面的，测试必须走同一条路，
    否则页面上报的"不通"跟实际抓取能力对不上。
    """
    from app.modules.ladysite.base import fetch_via_bypass

    try:
        html = fetch_via_bypass(url, quick=True)
    except Exception as exc:
        return "fail", f"绕过服务异常：{type(exc).__name__}: {exc}"[:200]

    if not html:
        return "blocked", "绕过服务未返回内容，可能未运行或过盾失败"
    # 过盾服务可能拿回 200 的校验页，内容里仍是人机验证
    if any(mark in html for mark in VERIFY_MARKS):
        return "blocked", "绕过服务返回前置校验页，未真正进站"
    return "ok", "经绕过服务连通"


def check_source(key: str, timeout: float = 12.0) -> dict:
    """测一个源的连通性，结果写回库供页面展示。

    与抓取链路保持一致（见 base.SiteClient.get）：bypass_first 的站直接走
    过盾服务，其余站直连；直连被拦时若配了过盾服务则再复测一次。
    否则这些站测试永远显示被拦，而实际抓取是通的。

    区分"通"与"被盾拦"：Cloudflare 挑战页也是能连上的，
    只报连通会让用户以为可以抓，实际一条都拿不到。
    """
    from app.core.config import get_settings
    from app.database.models import DataSource
    from datetime import datetime

    source = get_source(key)
    if source is None:
        return {"key": key, "status": "fail", "message": f"未知数据源 {key}"}

    host = (source.get("host") or "").rstrip("/")
    if not host:
        return {"key": key, "status": "fail", "message": "未配置地址"}

    has_bypass = bool(get_settings().bypass_url)
    # 首页有前置校验的源改探内容页，否则测的是校验页而非站点本身
    url = f"{host}{CHECK_PATH.get(key, '')}"

    if source.get("bypass_first") and has_bypass:
        status, message = _check_via_bypass(url)
    else:
        status, message = _check_direct(url, timeout, key)
        # 直连被拦，抓取链路此时会改走过盾服务，测试也照做
        if status == "blocked":
            if not has_bypass:
                message = f"{message}。需配置反爬绕过服务"
            else:
                bypass_status, bypass_message = _check_via_bypass(url)
                status = bypass_status
                message = (
                    bypass_message if bypass_status == "ok"
                    else f"{message}；{bypass_message}"
                )

    with session_scope() as session:
        row = session.scalar(select(DataSource).where(DataSource.key == key))
        if row is not None:
            row.status = status
            row.status_message = message
            row.checked_time = datetime.now()

    return {"key": key, "status": status, "message": message}


def enabled_parser_sources() -> list[dict]:
    """已启用且有解析器的源，按优先级排序。

    抓取链路只认这些；没有解析器的源开着也抓不到东西。
    """
    from app.database.models import DataSource

    with session_scope() as session:
        rows = session.scalars(
            select(DataSource)
            .where(DataSource.enabled.is_(True), DataSource.deleted.is_(False))
            .order_by(DataSource.priority)
        ).all()
        out = []
        for row in rows:
            parser = SOURCE_MAP.get(row.key, {}).get("parser", "")
            if not parser:
                continue
            out.append({
                "key": row.key,
                "host": row.host,
                "interval": row.interval,
                "cookie": row.cookie or "",
                "bypass_first": row.bypass_first,
                "code_rule": row.code_rule or "",
                "parser": parser,
            })
        return out
