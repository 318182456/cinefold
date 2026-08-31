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


# 简繁对照字表。左为简体、右为繁体，同一位置一一对应。
#
# 写成成对的字符串而不是两个独立集合：判定靠比较两边的出现次数，
# 字表一旦错位或一边多出几个字，计数就带着偏差，而这种错误光看代码
# 看不出来 —— 下面用断言把对齐关系钉住。
#
# 字表要足够大：判定的失败模式不是判错，而是两边都数不到（见
# is_simplified_chinese 第 3 条），那种情况一律放弃。字表越小，正经的
# 简体字幕越容易因为「恰好没用到这几十个字」而被丢掉，口语化的短字幕
# 尤其容易踩中。
_GLYPH_PAIRS: tuple[tuple[str, str], ...] = (
    # 高频虚词、代词、动词
    ("们说过还这来时对没样开关闭个为国将实现给认识话请让边远进运动",
     "們說過還這來時對沒樣開關閉個為國將實現給認識話請讓邊遠進運動"),
    # 具象名词
    ("东车马鸟长门问间见觉学图书区医体单双发头买卖钱银铁钢铜针线",
     "東車馬鳥長門問間見覺學圖書區醫體單雙發頭買賣錢銀鐵鋼銅針線"),
    # 动作与状态
    ("爱欢乐办务听闻写读讲谈论议决战胜负担应该须愿",
     "愛歡樂辦務聽聞寫讀講談論議決戰勝負擔應該須願"),
    # 身体、亲属、称谓
    ("脸颊脑脏肿众丽妆娱儿妇妈爷孙亲师员",
     "臉頰腦臟腫眾麗妝娛兒婦媽爺孫親師員"),
    # 场景高频
    ("点热闹静紧张压电灯灾难险终继续离归剧场",
     "點熱鬧靜緊張壓電燈災難險終繼續離歸劇場"),
)

# 两边必须严格等长且逐位对应，否则计数比较从一开始就是偏的
for _simp, _trad in _GLYPH_PAIRS:
    assert len(_simp) == len(_trad), f"简繁字表长度不一致: {_simp!r} / {_trad!r}"

# 简体独有字形。出现即为简体的证据
_SIMPLIFIED_ONLY = frozenset("".join(s for s, _ in _GLYPH_PAIRS))

# 繁体独有字形，与上面一一对应
_TRADITIONAL_ONLY = frozenset("".join(t for _, t in _GLYPH_PAIRS))

# 同一个字不该同时算作简体证据和繁体证据（如「乐」既是「樂」的简化，
# 本身又是繁体用字），那种字对两边计数都加分，等于噪声
assert not (_SIMPLIFIED_ONLY & _TRADITIONAL_ONLY), "简繁字表有重叠字"

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

    只做判定、不改内容。繁体要转成简体的话走 as_simplified_chinese ——
    判定函数保持无副作用，调用方才好分别复用这两件事。

    判定本身不引 opencc：为一个判定拉进几十 MB 的词典不划算，字形集合
    对字幕这种口语化文本已经足够准。转换那边同理，用的是内联字表（见
    t2s）。
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


def is_chinese_subtitle(text: str) -> bool:
    """这段字幕是中文吗（简繁不论）。

    与 is_simplified_chinese 共用前两道判据（汉字量、假名占比），
    只是不再要求字形偏简体 —— 繁体也算中文，转换过后一样能用。
    """
    if not text:
        return False

    han = _HAN.findall(text)
    if len(han) < 20:
        return False

    kana = _KANA.findall(text)
    if len(kana) > len(han) * 0.15:
        return False

    # 得有一边的字形能数到，否则可能是日文汉字文本之类
    simplified = sum(1 for ch in text if ch in _SIMPLIFIED_ONLY)
    traditional = sum(1 for ch in text if ch in _TRADITIONAL_ONLY)
    return simplified > 0 or traditional > 0


def as_simplified_chinese(text: str) -> str:
    """把字幕正文规整成简体中文，拿不到就返回空串。

    简体原样返回；繁体逐字转成简体。以前繁体一律丢弃，媒体库因此白缺
    很多本来能用的字幕 —— 转换后的简体比没有字幕有用得多。

    日文、英文仍旧返回空串：那是「看不懂」而非「字形不同」，转不出来。
    """
    if not is_chinese_subtitle(text):
        return ""

    if is_simplified_chinese(text):
        return text

    from app.modules.subtitle.t2s import to_simplified

    converted = to_simplified(text)
    # 转完再验一次：字形偏简体才算成功。转不动（表里没有这些字）时
    # 结果与原文一样，那就仍旧不可用
    if not is_simplified_chinese(converted):
        return ""
    return converted


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
