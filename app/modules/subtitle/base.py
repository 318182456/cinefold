"""字幕抓取的公共基础。

简体判定与编码识别放在这里 —— 两件事各站点都要做，且都不是「看一眼就对」
的逻辑，散在各 parser 里迟早会各写各的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 字幕正文的合法扩展名。压缩包不收：解压要引依赖，且包里常是一堆
# 分集字幕，挑不出对应哪一部
SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt", ".sub")

# 单个字幕文件的体积上限（字节）。正常字幕几十 KB，超过这个数量级的
# 多半是站点返回了错误页或整包内容
MAX_SUBTITLE_BYTES = 2 * 1024 * 1024

# 判定为有效字幕所需的最少字节数。站点找不到时常返回只有头部的空壳
MIN_SUBTITLE_BYTES = 200


@dataclass
class SubtitleItem:
    """一条抓到的字幕。"""
    code: str = ""
    # 站点上的标题，仅用于日志
    title: str = ""
    site: str = ""
    # 正文，已解码为 str
    content: str = ""
    # 落盘用的扩展名，含点
    suffix: str = ".srt"

    @property
    def filename(self) -> str:
        return f"{self.code}{self.suffix}"


# 简体独有字形。这些字在繁体里写法不同，出现即可判定为简体。
# 取高频字，短字幕也能命中
_SIMPLIFIED_ONLY = set("们说过还这来时对没样开关闭东车马鸟长门问间见觉学"
                       "个为国将实现发现给认识话请让边远进运动图书区医")

# 繁体独有字形，与上面一一对应
_TRADITIONAL_ONLY = set("們說過還這來時對沒樣開關閉東車馬鳥長門問間見覺學"
                        "個為國將實現發現給認識話請讓邊遠進運動圖書區醫")

# 日文假名。JAV 字幕站上日文原文字幕很多，且常被标成「Chinese」
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")

# 汉字
_HAN = re.compile(r"[一-鿿]")


def decode_subtitle(raw: bytes) -> str:
    """把字幕字节流解成文本。解不出返回空串。

    中文字幕的编码非常杂：站点给 UTF-8，但用户上传的原始文件常是 GBK 或
    BIG5，站点原样透传。按顺序试，第一个不报错且能出汉字的就采用。
    """
    if not raw:
        return ""

    # BOM 优先，有就不用猜
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")

    for encoding in ("utf-8", "gb18030", "big5", "shift_jis"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # gb18030 几乎不会解码失败（它能映射绝大多数字节组合），
        # 用错了不会报错、只会出乱码，所以还要看解出来像不像话
        if _HAN.search(text) or encoding == "utf-8":
            return text

    return raw.decode("utf-8", errors="replace")


def is_simplified_chinese(text: str) -> bool:
    """这段字幕是简体中文吗。

    三道判据依次收紧：

      1. 得有足够的汉字 —— 纯英文、纯假名直接排除
      2. 假名占比不能高 —— 日文字幕里也有汉字，靠汉字数量区分不了
      3. 简体字形要多于繁体 —— 两者都为 0 时（生僻用字、极短字幕）
         判为不确定，宁可放弃

    不用 opencc 之类的库：为一个判定引入几十 MB 的词典不划算，
    而字形集合对字幕这种口语化文本已经足够准。
    """
    if not text:
        return False

    han = _HAN.findall(text)
    if len(han) < 20:
        return False

    kana = _KANA.findall(text)
    # 日文字幕里假名与汉字大致相当甚至更多；中文字幕里假名只会零星出现
    # （拟声词、原文残留），占比很低
    if len(kana) > len(han) * 0.15:
        return False

    simplified = sum(1 for ch in text if ch in _SIMPLIFIED_ONLY)
    traditional = sum(1 for ch in text if ch in _TRADITIONAL_ONLY)

    if simplified == 0 and traditional == 0:
        # 两种字形都没出现，无从判断。放弃比赌一把强
        return False
    return simplified > traditional


def looks_like_subtitle(text: str) -> bool:
    """内容像字幕文件吗。

    站点找不到资源时常返回 200 + 一个 HTML 错误页，只看 HTTP 状态码
    会把它当字幕存进媒体库。
    """
    if not text:
        return False
    head = text.lstrip()[:400].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return False
    # SRT 的时间轴、WebVTT 头、ASS 的段落头，三者必居其一
    if re.search(r"\d{1,2}:\d{2}:\d{2}[,.]\d{2,3}\s*-->", text):
        return True
    if head.startswith("webvtt"):
        return True
    if "[script info]" in head or "[events]" in head:
        return True
    return False


def pick_suffix(name: str) -> str:
    """从站点给的文件名里取扩展名，认不出就当 .srt。"""
    lowered = (name or "").lower()
    for suffix in SUBTITLE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return ".srt"
