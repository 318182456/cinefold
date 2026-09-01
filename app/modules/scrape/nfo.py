"""NFO 生成。

Emby / Jellyfin / Plex 都读影片旁的同名 .nfo，格式是 Kodi 定的
<movie> 结构。这里从本地 Code 表的元数据生成一份完整的。

与 services/review.py 里的 _write_nfo 分工：
    这里      从零生成整份 NFO（自建刮削）
    review    只改已有 NFO 的 <plot>/<tagline>（含本模块的产物）

两者会先后作用于同一个文件：本模块先写出完整 NFO，review 再往 plot 里
拼 AI 看点。所以生成时 plot 只放官方简介，把 AI 那段留给 review 去拼——
它有自己的标记体系（MARKER/END_MARKER）来做可重复替换。

分集的处理：每个分集文件旁各写一份 NFO，内容除了 <title> 带 CD 后缀外
完全相同。不共用一份是因为 Emby 按文件名配对 NFO，CD2.mp4 找的是
CD2.nfo，找不到就整个条目没元数据（MDCng issue #503 的症状之一）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from loguru import logger

# 库里 genres/casts 都是逗号分隔的文本，中英文标点都可能出现
_SPLIT_RE = re.compile(r"[,，、]\s*")

# 时长字段可能是 "120分钟" / "120 min" / "02:00:00" / "120"，统一取分钟数
_DURATION_MIN = re.compile(r"(\d+)\s*(?:分|min)", re.IGNORECASE)
_DURATION_HMS = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

# XML 1.0 不允许的控制字符。抓来的简介里偶尔混进这些，写进去整份文件
# 就解析不了了 —— Emby 会当成没有元数据，而报错只出现在它自己的日志里
_ILLEGAL_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f﷐-﷟￾￿]"
)


def _clean(text: str | None) -> str:
    """清掉不能进 XML 的字符，顺带去首尾空白。"""
    if not text:
        return ""
    return _ILLEGAL_XML.sub("", str(text)).strip()


def _split_list(text: str | None, simplify: bool = False) -> list[str]:
    """逗号分隔的文本切成列表，去空去重且保持原序。

    simplify=True 时顺带繁转简。类别标签用得上 —— 部分源（avbase 走的
    DMM 数据、airav）给的是繁体，实测 SSIS-001 抓回来是
    「薄馬賽克 / 高畫質 / 戲劇」。Emby 按标签分类浏览，繁简混在一起
    会分成两个类目（「戲劇」和「戏剧」各一个），点进去各有一半片子。

    演员名不转：人名不该动，「鈴村あいり」这类日文名转了反而错。
    """
    if not text:
        return []

    if simplify:
        from app.modules.subtitle.t2s import to_simplified

    out: list[str] = []
    seen: set[str] = set()
    for chunk in _SPLIT_RE.split(str(text)):
        value = _clean(chunk)
        if simplify and value:
            value = to_simplified(value)
        # 去重放在转换之后：繁简两份同名标签转完是同一个，只留一个
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _runtime_minutes(duration: str | None) -> str:
    """把库里的时长文本折成分钟数字符串。取不出返回空串。"""
    text = _clean(duration)
    if not text:
        return ""

    match = _DURATION_HMS.match(text)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        return str(hours * 60 + minutes)

    match = _DURATION_MIN.search(text)
    if match:
        return match.group(1)

    # 纯数字就当已经是分钟。上限校验一下：4 位数以上不可能是片长
    if text.isdigit() and 0 < int(text) < 1000:
        return text
    return ""


@dataclass
class NfoData:
    """写一份 NFO 需要的全部信息。字段名对齐 Code 表。"""
    code: str = ""
    title: str = ""
    cn_title: str = ""
    plot: str = ""
    release_date: str = ""
    duration: str = ""
    producer: str = ""
    publisher: str = ""
    series: str = ""
    genres: str = ""
    casts: str = ""
    star: float | None = None
    # 分集序号，0 为单文件。影响 <title> 与 <sorttitle>
    part: int = 0
    total_parts: int = 0
    # 文件名读出来的标记。只影响标签，不做内容推断
    uncensored: bool = False
    subbed: bool = False
    # 图片的相对文件名（与 NFO 同目录），供 <thumb>/<fanart> 引用
    poster_file: str = ""
    fanart_file: str = ""
    extra_genres: list[str] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        """NFO 里的 <title>。优先中文标题，带上番号前缀便于识别。

        Emby 列表页显示的就是这个。番号放前面是刮削工具的通行做法 ——
        同一演员几十部片，标题各异但番号有序，按名字排序时能排到一起。
        """
        base = _clean(self.cn_title) or _clean(self.title)
        code = _clean(self.code)
        if base and code:
            head = f"{code} {base}"
        else:
            head = base or code

        if self.part > 0:
            # 分集后缀放最后。Emby 列表上分集各自一行，
            # 标题里不写清楚就分不出哪个是哪集
            head = f"{head} CD{self.part}"
        return head


def build_tree(data: NfoData) -> ElementTree.ElementTree:
    """按 Kodi <movie> 结构组装 XML 树。"""
    root = ElementTree.Element("movie")

    def add(tag: str, value: str | None) -> None:
        """有值才加节点。

        空节点会让部分 Emby 版本显示成空字符串，比字段缺失更难看 ——
        缺失时它会回退到文件名，那通常还能看。
        """
        text = _clean(value)
        if text:
            ElementTree.SubElement(root, tag).text = text

    add("title", data.display_title)
    add("originaltitle", data.title)
    # sorttitle 用番号：保证同一系列排在一起，与显示标题解耦
    sort = _clean(data.code)
    if sort and data.part > 0:
        sort = f"{sort}-CD{data.part}"
    add("sorttitle", sort)

    # plot 只放官方简介。AI 看点由 services/review.py 事后拼进来
    add("plot", data.plot)
    add("outline", data.plot)

    add("num", data.code)
    # uniqueid 是 Emby 认条目的依据，type 自定义一个即可
    if _clean(data.code):
        node = ElementTree.SubElement(
            root, "uniqueid", {"type": "num", "default": "true"}
        )
        node.text = _clean(data.code)

    add("premiered", data.release_date)
    add("releasedate", data.release_date)
    # year 单列一个字段：Emby 的年份筛选读它，不会自己从 premiered 推
    release = _clean(data.release_date)
    if len(release) >= 4 and release[:4].isdigit():
        add("year", release[:4])

    add("runtime", _runtime_minutes(data.duration))
    add("studio", data.producer)
    add("maker", data.producer)
    add("publisher", data.publisher)
    add("label", data.publisher)

    if _clean(data.series):
        add("set", data.series)
        add("series", data.series)

    if data.star is not None:
        try:
            # Kodi 的 rating 是 0-10，库里 star 已经是这个量纲
            add("rating", f"{float(data.star):.1f}")
        except (TypeError, ValueError):
            pass

    # 标签：库里的类别 + 文件名读出的标记。
    # 标记必须来自文件名或番号后缀，绝不做内容推断 ——
    # MDCng issue #513 就是把没破解的片子打上了破解标签
    genres = _split_list(data.genres, simplify=True)
    for extra in data.extra_genres:
        if extra and extra not in genres:
            genres.append(extra)
    # genre 与 tag 都写：Emby 按 genre 分类浏览，按 tag 做筛选，
    # 两处读的是不同节点
    for value in genres:
        add("genre", value)
    for value in genres:
        add("tag", value)

    for name in _split_list(data.casts):
        actor = ElementTree.SubElement(root, "actor")
        ElementTree.SubElement(actor, "name").text = name
        ElementTree.SubElement(actor, "type").text = "Actor"

    if data.poster_file:
        add("thumb", data.poster_file)
        add("poster", data.poster_file)
    if data.fanart_file:
        fanart = ElementTree.SubElement(root, "fanart")
        ElementTree.SubElement(fanart, "thumb").text = _clean(data.fanart_file)

    return ElementTree.ElementTree(root)


def _indent(elem: ElementTree.Element, level: int = 0) -> None:
    """给 XML 加缩进。用户会手动看/改 NFO，压成一行没法读。

    不用 ElementTree.indent：3.9 才有，且这里要自己控制换行细节。
    """
    pad = "\n" + "  " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "  "
        child = elem
        for child in elem:
            _indent(child, level + 1)
        if not (child.tail or "").strip():
            child.tail = pad
    if level and not (elem.tail or "").strip():
        elem.tail = pad


def render(data: NfoData) -> bytes:
    """生成 NFO 的字节内容（带 XML 声明，UTF-8）。"""
    tree = build_tree(data)
    root = tree.getroot()
    _indent(root)
    body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body + b"\n"


def write(path: Path, data: NfoData, overwrite: bool = True) -> bool:
    """把 NFO 写到指定路径。返回是否真的写了。

    先写临时文件再 rename：Emby 的扫库可能正好在读这个文件，
    直接原地写会让它读到半份 XML 并把条目标成损坏。

    overwrite=False 时已存在就跳过 —— 用于「只补缺失的 NFO」这种场景，
    用户手改过的内容不该被自动任务冲掉。
    """
    try:
        if path.exists() and not overwrite:
            logger.debug(f"[刮削] NFO 已存在且不覆盖，跳过: {path}")
            return False

        content = render(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning(f"[刮削] 写 NFO 失败: {path} ({exc})")
        return False
