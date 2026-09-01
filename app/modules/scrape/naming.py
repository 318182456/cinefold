"""产物路径与文件名的模板渲染。

模板语法与 MDCng 对齐，用户从那边搬配置过来不用改写：

    基础     {number}        缺失字段显示「未知」
    高级     {{ number }}    缺失字段为空，支持条件判断与 filter（Jinja2）

两套语法按模板里有没有 {{ }} 自动分流，不要用户选。

Jinja2 是可选依赖：没装时高级语法退回基础语法处理（只做变量替换，
条件与 filter 失效并给出一次警告）。这样 SQLite 单容器的最小部署
不会因为少一个包就整个刮削不可用 —— 命名退化成简单替换仍然能跑。

字段清单见 FIELDS。命名与 MDCng 的变量表一致，多出来的几个
（part / cd）是本项目的分集支持需要的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

try:  # pragma: no cover - 取决于部署环境装没装
    from jinja2 import Environment, StrictUndefined  # noqa: F401
    from jinja2 import TemplateError
    HAS_JINJA = True
except ImportError:  # pragma: no cover
    HAS_JINJA = False

    class TemplateError(Exception):  # type: ignore[no-redef]
        """占位，让下面的 except 子句在没装 Jinja2 时也成立。"""


# 基础语法缺失字段的占位符。跟 MDCng 一致 —— 用户看到「未知」就知道
# 是哪个字段没抓到，而空串会让路径里出现 "//" 或首尾多余的分隔符
UNKNOWN = "未知"

# 这些字段为空是正常状态，不是「没抓到」，所以基础语法下也填空串。
#
# 分集号最典型：单文件影片本来就没有分集，填「未知」会得到
# ABS-001未知.mp4。字幕/马赛克标识同理 —— 没有中文字幕不等于
# 不知道有没有中文字幕
_EMPTY_OK = {"cd", "part", "subtitle", "mosaic"}

# 模板里认得的字段。值是「从哪里取」的说明，仅供文档与前端提示用
FIELDS = {
    "number": "番号 (如 ABS-001)",
    "publish_number": "发行号 (如 118abs001)",
    "series_name": "番号前缀 (如 ABS)",
    "serial_number": "番号后缀 (如 001)",
    "first_letter": "番号前缀首字母 (如 A)",
    "series": "系列",
    "category": "分类",
    "actor": "演员",
    "first_actor": "首位演员",
    "title": "标题",
    "originaltitle": "原标题",
    "year": "发布年份",
    "director": "导演",
    "studio": "制片方",
    "publisher": "发行方",
    "runtime": "时长(分钟)",
    "release": "发布日期",
    "source_filename": "源文件名 (不含扩展名)",
    "filename": "源文件名别名 (等同于 source_filename)",
    "source_path": "源文件完整路径",
    "subtitle": "中文字幕标识",
    "mosaic": "有码/无码标识",
    "resolution": "分辨率",
    # 以下为本项目扩展，MDCng 没有
    "part": "分集序号 (不分集时为空)",
    "cd": "分集后缀 (如 -CD1，不分集时为空)",
}

# 高级语法的判据：出现 {{ 或 {% 就当 Jinja2 模板
_JINJA_MARK = re.compile(r"\{\{|\{%")

# 高级语法里的分集变量位。{{ cd }} / {{ part }}，可能带 filter
_JINJA_VAR_PART = re.compile(r"\{\{\s*(?:cd|part)\b")

# 字段渲染成空后留下的孤立分隔符。"{number}-{resolution}" 在没有
# 分辨率时会得到 "ABS-001-"，结尾那个横杠得去掉。
#
# 只处理首尾与连续重复，不动中间的单个分隔符 —— "ABS-001" 自己就带
# 横杠，误删会毁掉番号
_EDGE_SEP = re.compile(r"^[-_\s]+|[-_\s]+$")
_DUP_SEP = re.compile(r"([-_])[-_\s]*\1+")


def _tidy_separators(text: str) -> str:
    """清掉字段为空后残留的孤立/重复分隔符。"""
    if not text:
        return ""
    value = _DUP_SEP.sub(r"\1", text)
    return _EDGE_SEP.sub("", value)

# 基础语法的变量位。只认 {word}，不吃 {{ }}（那是 Jinja2 的）
_BASIC_VAR = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")

# 路径里不能出现的字符。Windows 比 Linux 严，一律按 Windows 来 ——
# 媒体库挂在 SMB 上时，Linux 侧写得进去而 Windows 侧打不开，更难查
_ILLEGAL_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 连续的分隔符与首尾的点/空格。Windows 上以点或空格结尾的目录名
# 无法访问（资源管理器创建不了，API 层面也会被静默 trim）
_MULTI_SEP = re.compile(r"[/\\]{2,}")
_TRAILING_DOTS = re.compile(r"[.\s]+$")

# 单段路径的长度上限。多数文件系统限 255 字节，中文按 UTF-8 算 3 字节，
# 保守取 80 个字符 —— 再加上扩展名与 -fanart 之类的后缀也不会超
MAX_SEGMENT = 80

# Windows 保留设备名。拿它们当目录名会造出无法访问的路径
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 默认模板。目录按「分类/演员」，文件名用番号 —— 与 MDCng 的默认接近
DEFAULT_DIR_TEMPLATE = "{category}/{first_actor}"
DEFAULT_FILE_TEMPLATE = "{number}"


@dataclass
class NamingContext:
    """渲染模板要用到的全部字段值。空串表示该字段没有。"""
    number: str = ""
    publish_number: str = ""
    series: str = ""
    category: str = ""
    actor: str = ""
    title: str = ""
    originaltitle: str = ""
    year: str = ""
    director: str = ""
    studio: str = ""
    publisher: str = ""
    runtime: str = ""
    release: str = ""
    source_path: str = ""
    subtitle: str = ""
    mosaic: str = ""
    resolution: str = ""
    part: int = 0

    def to_vars(self) -> dict[str, str]:
        """摊平成模板变量表。派生字段在这里算。"""
        number = self.number or ""
        # 番号拆前后段：ABS-001 → ABS / 001。日期型（032416_267）按
        # 下划线拆，没有分隔符时前段就是整串、后段为空
        if "-" in number:
            head, _, tail = number.rpartition("-")
        elif "_" in number:
            head, _, tail = number.rpartition("_")
        else:
            head, tail = number, ""

        actors = [a.strip() for a in (self.actor or "").split(",") if a.strip()]

        return {
            "number": number,
            "publish_number": self.publish_number or "",
            "series_name": head,
            "serial_number": tail,
            "first_letter": head[:1].upper() if head else "",
            "series": self.series or "",
            "category": self.category or "",
            # actor 给全部演员，逗号分隔。MDCng issue #510 提到多演员会
            # 被拆成两个目录 —— 那是因为把 actor 直接当目录名用了。
            # 这里保持原样，是否只取一个由模板决定（用 first_actor）
            "actor": ",".join(actors),
            "first_actor": actors[0] if actors else "",
            "title": self.title or "",
            "originaltitle": self.originaltitle or "",
            "year": self.year or "",
            "director": self.director or "",
            "studio": self.studio or "",
            "publisher": self.publisher or "",
            "runtime": self.runtime or "",
            "release": self.release or "",
            "source_filename": Path(self.source_path).stem if self.source_path else "",
            "filename": Path(self.source_path).stem if self.source_path else "",
            "source_path": self.source_path or "",
            "subtitle": self.subtitle or "",
            "mosaic": self.mosaic or "",
            "resolution": self.resolution or "",
            "part": str(self.part) if self.part > 0 else "",
            "cd": f"-CD{self.part}" if self.part > 0 else "",
        }


_env = None
_jinja_warned = False


def _filter_split(value: str, sep: str = "-") -> list[str]:
    """str.split 的 filter 版。

    Jinja2 没有内置 split，但 MDCng 的模板文档里列了它
    （{{ number | split("-") | first }} 是文档给的示例），
    用户照着写过来必须能跑，所以在这里注册一个同名的。
    """
    return str(value or "").split(sep)


def _get_env():
    """懒建 Jinja2 环境。模板渲染是热路径，环境只建一次。"""
    global _env
    if _env is None:
        # 缺失字段渲染成空串（而非报错），与 MDCng 的高级语法约定一致。
        # 所以不用 StrictUndefined
        _env = Environment(autoescape=False, keep_trailing_newline=False)
        _env.filters["split"] = _filter_split
    return _env


def _render_basic(template: str, variables: dict[str, str]) -> str:
    """基础语法：{field} 直接替换，缺失填「未知」。"""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            logger.debug(f"[命名] 模板里有未知字段 {{{key}}}，按缺失处理")
            return UNKNOWN
        if not variables[key]:
            # 这几个字段「空」是正常状态而不是抓取失败：不分集的片子
            # 本来就没有分集号。填「未知」会造出 ABS-001未知.mp4
            return "" if key in _EMPTY_OK else UNKNOWN
        return variables[key]

    return _BASIC_VAR.sub(replace, template)


def _render_jinja(template: str, variables: dict[str, str]) -> str:
    """高级语法：交给 Jinja2。缺失字段为空串。"""
    global _jinja_warned
    if not HAS_JINJA:
        if not _jinja_warned:
            logger.warning(
                "命名模板用了高级语法（{{ }}）但未安装 jinja2，"
                "条件判断与 filter 会失效，仅做变量替换。"
                "装上 jinja2 即可恢复"
            )
            _jinja_warned = True
        # 退化：把 {{ x }} 拍成 {x} 再走基础语法，但缺失填空串而非「未知」
        # —— 高级语法的约定是缺失为空，不能因为退化就改变语义
        flattened = re.sub(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", r"{\1}", template)
        # 条件块整段丢掉：留着会把 {% if %} 原文写进路径
        flattened = re.sub(r"\{%.*?%\}", "", flattened)
        # 剩下的 {{ ... }} 是带 filter 或表达式的，退化模式下算不出来。
        # 必须整段删掉 —— 留着会把模板原文当成目录名写进媒体库
        flattened = re.sub(r"\{\{.*?\}\}", "", flattened)
        return _BASIC_VAR.sub(
            lambda m: variables.get(m.group(1), ""), flattened
        )

    try:
        return _get_env().from_string(template).render(**variables)
    except TemplateError as exc:
        logger.error(f"[命名] 模板渲染失败，退回番号: {template} ({exc})")
        return variables.get("number", "")


def sanitize_segment(text: str, max_length: int = MAX_SEGMENT) -> str:
    """把一段文本清成合法的目录名/文件名。

    非法字符换成下划线而不是删掉：删掉会让 "A/B" 变成 "AB"，
    两个不同的值撞成同一个名字。

    超长按字符数截断。不按字节截是因为截在多字节字符中间会产生
    乱码文件名，而字符数上限已经留足了余量（见 MAX_SEGMENT）。
    """
    if not text:
        return ""
    value = _ILLEGAL_PATH.sub("_", str(text)).strip()
    # 结尾的点与空格在 Windows 上无法访问
    value = _TRAILING_DOTS.sub("", value)
    if len(value) > max_length:
        value = _TRAILING_DOTS.sub("", value[:max_length])
    if value.upper() in _RESERVED:
        # 保留名加个下划线就能用，比整段丢掉好
        value = f"{value}_"
    return value


def render_template(template: str, context: NamingContext) -> str:
    """渲染一个模板，返回未切分的原始结果。"""
    variables = context.to_vars()
    if _JINJA_MARK.search(template or ""):
        return _render_jinja(template, variables)
    return _render_basic(template or "", variables)


def render_dir(template: str, context: NamingContext) -> Path:
    """渲染目录模板，返回相对根目录的路径。

    模板里的 / 是目录分隔，要保留；每一段单独清洗。空段直接丢掉 ——
    "{series}/{number}" 在没有系列时不该产出 "/ABS-001" 这种带空层的路径。

    模板留空表示**不分子目录**，产物直接落在根目录下（返回 Path(".")）。
    这是有意义的配置，不能回退到默认模板 —— 用户特意清空就是想要平铺，
    悄悄给他建一层 "日本AV/未知" 是帮倒忙。
    """
    if not (template or "").strip():
        return Path(".")

    raw = render_template(template, context)
    # 反斜杠也当分隔符：用户从 Windows 抄模板过来常写 \
    raw = raw.replace("\\", "/")
    raw = _MULTI_SEP.sub("/", raw)

    parts = []
    for chunk in raw.split("/"):
        segment = _tidy_separators(sanitize_segment(chunk))
        # 「未知」是有意义的占位，保留；纯空的段丢掉
        if segment:
            parts.append(segment)

    if not parts:
        # 模板整个渲染成空，退回番号目录 —— 总得有个地方放
        fallback = sanitize_segment(context.number) or "未分类"
        logger.warning(f"[命名] 目录模板渲染为空，退回 {fallback}: {template}")
        return Path(fallback)
    return Path(*parts)


def render_file(template: str, context: NamingContext, suffix: str = "") -> str:
    """渲染文件名模板，返回带扩展名的文件名。

    分集后缀不由模板负责：模板管的是「这部片子叫什么」，而分集是
    同一部片的多个文件。模板里没写 {cd} 时自动补在末尾，写了就不重复补
    —— 否则用户想把 CD 放中间（ABS-001-CD1-中文字幕）就做不到。
    """
    raw = render_template(template or DEFAULT_FILE_TEMPLATE, context)
    # 文件名里的 / 是非法字符，不是分隔符
    name = sanitize_segment(raw.replace("/", "_").replace("\\", "_"))
    name = _tidy_separators(name)

    if not name:
        name = sanitize_segment(context.number) or "unnamed"
        logger.warning(f"[命名] 文件名模板渲染为空，退回 {name}: {template}")

    # 模板已经带了分集位（{cd} / {part} / 手写的 CD）就不再补，
    # 否则用户想把 CD 放中间就会得到两个后缀。
    #
    # 已带 CD 后缀的源文件名也要认（{source_filename} 渲染出
    # ABS-001-CD2-1080P 时，末尾再补一次就成了 -CD2-1080P-CD2）
    if context.part > 0 and not _has_part_slot(template, name, context.part):
        name = f"{name}-CD{context.part}"

    return f"{name}{suffix}" if suffix else name


def _has_part_slot(template: str, rendered: str, part: int) -> bool:
    """模板或渲染结果里是否已经表达了分集。"""
    text = template or ""
    if "{cd}" in text or "{part}" in text:
        return True
    if _JINJA_VAR_PART.search(text):
        return True
    # 渲染结果里已经有 CD<n>（大小写与分隔符都可能不同）
    return bool(re.search(rf"(?:^|[-_\s.])cd\s*{part}\b", rendered, re.IGNORECASE))
