"""影片文件名解析：分集、版本、类型标记。

刮削前必须先看懂文件名。这里只做"从文件名能读出什么"，不碰网络、不查库
——ladysite 那层负责抓元数据，这层负责决定「拿哪个番号去抓」以及
「产物该叫什么名字」。

三件事分得很清，混在一起是错误的根源：

    番号     ABP-554     决定抓哪条元数据，分集之间共用
    分集     CD1 / CD2   决定产物文件名，同一份元数据下的不同文件
    版本     -C / -UC    番号的一部分，不同版本是不同条目

MDCng issue #503（带 cd 分片的影片大概率刮不到剧照）就是第一二项没分开：
把 "PGD-613-CD1" 整串当番号去查，站上没有这个条目，元数据与剧照一起丢。
正确做法是查 PGD-613，产物写成 PGD-613-CD1.mp4，共用一份元数据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger
from pathlib import Path

from app.utils import _CLEAN_CODE, _CODE_SUFFIX, find_serial_number, get_true_code

# 分集标记的各种写法。实测样本里这几类占绝大多数：
#
#   ABP-554-CD1 / ABP-554-cd2      最常见，MDCng 自己也输出这个格式
#   ABP-554-A / ABP-554-B          字母式，注意不能和 -C（中文字幕）混淆
#   ABP-554-1 / ABP-554-2          纯数字式，最容易误判
#   ABP-554.part1 / ABP-554-pt2
#
# 为什么必须锚在结尾（\s*$）：番号中间也有数字和连字符，不锚定的话
# "300MIUM-123" 里的 "-123" 会被当成第 123 集。
_PART_PATTERNS = (
    # CD1 / cd-1 / CD 1
    re.compile(r"[-_\s.]cd[-_\s]?(\d{1,2})\s*$", re.IGNORECASE),
    # part1 / pt2 / p3（p 单独一个字母风险高，要求后面紧跟数字且在结尾）
    re.compile(r"[-_\s.](?:part|pt)[-_\s]?(\d{1,2})\s*$", re.IGNORECASE),
    # 中文：第1集 / 上 下 —— 上下另作处理，这里只认数字
    re.compile(r"[-_\s.]?第\s*(\d{1,2})\s*[集片段部]\s*$"),
)

# 纯数字式分集：ABP-554-1 / ABP-554-2。单独处理，因为它和番号本身的
# 数字段形状完全一样，必须要求「前面已经是一个完整番号」才认。
#
# 判据是前缀能被 _CLEAN_CODE 整串匹配（ABP-554-1 → 前缀 ABP-554 合格），
# 这样 300MIUM-123 不会被劈成 300MIUM + 第 123 集（前缀 300MIUM 没有
# 数字段，不合格），日期型 032416_267 也不会（前缀纯数字，不合格）。
#
# 序号上限 4：分集超过 4 的极少，而放宽会把 -10 -12 这类年份/画质残留
# 也吞进来
_PART_NUMERIC = re.compile(r"^(?P<head>.+?)[-_\s.](?P<num>[1-4])\s*$")

# 字母式分集：-A / -B / -C ... 单独处理，因为要排掉版本后缀。
#
# -C 是"中文字幕"的行业约定，-U/-UC 是"无码破解"，它们是番号的一部分，
# 不是分集。所以字母式只认 A/B/D/E/F —— 跳过 C，也不认 U。
# 代价是真有 A~D 四集的片子第三集会认不出来，但那比把所有 -C 中文字幕版
# 都误判成第三集要好得多（后者会让整个中文字幕库刮削错乱）。
_PART_ALPHA = re.compile(r"[-_\s.]([ABDEF])\s*$", re.IGNORECASE)

# 中文的上/中/下集。
#
# "下"给 2 而不是 3：绝大多数中文分集是「上/下」两集，只有少数是
# 「上/中/下」三集。给 3 会让两集片的第二集变成 CD3，媒体库里出现
# CD1+CD3 的空档；给 2 则只在三集片上让中集与下集撞号，而那种片子
# 本来就少。撞号时 _resolve_collisions 会兜住（见 group_parts）。
_PART_CN = {"上": 1, "中": 2, "下": 2}
# 撞号重排时用的次序权重。不能靠文件名排序 —— "下"(U+4E0B) 的码位小于
# "中"(U+4E2D)，按名字排会得到 上/下/中，重排后中下颠倒
_PART_CN_ORDER = {"上": 1, "中": 2, "下": 3}
_PART_CN_RE = re.compile(r"[-_\s.]?([上中下])\s*$")

# 番号自带的版本后缀，保留进番号，不当分集。与 utils._CODE_SUFFIX 同源，
# 这里再列一遍是因为判定时机不同：那边是切番号，这边是防误判成分集
_VERSION_SUFFIX = re.compile(r"-(?:C|CH|U|UC|UCH)$", re.IGNORECASE)

# 画质/来源噪声。剥掉它们再找分集标记，否则 "ABP-554-CD1-1080P" 的
# 分集标记不在结尾，锚定的正则匹配不到
_NOISE_TAIL = re.compile(
    r"[-_\s.]*(?:"
    r"1080p|720p|2160p|4k|8k|hd|sd|fhd|uhd"
    r"|x264|x265|h264|h265|hevc|avc|aac|ac3|dts"
    r"|web-?dl|webrip|bluray|blu-?ray|bdrip|dvdrip|hdrip|remux"
    r"|60fps|30fps|hdr|sdr|10bit|8bit"
    r"|中文字幕|中字|无码|有码|流出|破解|字幕组|高清"
    r")\s*$",
    re.IGNORECASE,
)

# 无码破解的文件名标记。MDCng issue #513：没破解的片子被打上破解标签。
# 只认这几个明确写法，不做模糊猜测 —— 宁可漏标也不能错标。
#
# 特别地，"U" 单独出现不算：太多正常番号以 U 结尾。必须是 -U / -UC
# 这种带分隔符的后缀，或者文件名里明写了"无码破解"。
_UNCENSORED_TOKENS = re.compile(
    r"(?:^|[-_\s.\[])(?:uncensored|leaked|无码破解|无碼破解|破解版)(?:$|[-_\s.\]])",
    re.IGNORECASE,
)
_UNCENSORED_SUFFIX = re.compile(r"-(?:U|UC|UCH)$", re.IGNORECASE)

# 中文字幕标记
_SUBBED_TOKENS = re.compile(
    r"(?:^|[-_\s.\[])(?:chinese\s*sub(?:title)?s?|中文字幕|中字|简中|繁中)(?:$|[-_\s.\]])",
    re.IGNORECASE,
)
_SUBBED_SUFFIX = re.compile(r"-(?:C|CH|UCH)$", re.IGNORECASE)

# 预告片/样品。这些不是正片，绝不能当影片刮削 —— 它们和正片同目录同前缀，
# 不排掉就会在媒体库里多出一堆几十秒的"影片"
_TRAILER_TOKENS = re.compile(
    r"(?:^|[-_\s.\[])(?:trailer|sample|preview|预告|样品|预览)(?:$|[-_\s.\]])",
    re.IGNORECASE,
)

# 欧美片的文件名标志。MDCng issue #508：
# "VRLatina_Samy Sun_Pretty Petite_4096p_8K_LR_180" 认不出来。
#
# 比"认不出"更糟的是**认错**：上面那个文件名里的 "LR_180" 正好符合
# 「字母+分隔符+数字」的番号形状，会被当成番号 LR-180 去抓元数据，
# 抓回来的是另一部毫不相干的日本片，然后写进 NFO、建好硬链接 ——
# 用户在 Emby 里看到的是错的封面、错的演员、错的简介。
#
# 番号体系是日本 AV 特有的，欧美片按「厂牌 + 演员 + 场景名」命名，
# 压根没有番号。所以这里的策略是：认出是欧美片就直接放弃提取番号，
# 让调用方跳过它，而不是硬凑一个错的出来。
#
# 判据一：已知的欧美厂牌名。只列高频的 —— 列表不求全，求准，
# 命中即确定是欧美片
_WESTERN_STUDIOS = re.compile(
    r"(?:^|[-_\s.\[/])(?:"
    r"blacked(?:raw)?|tushy|vixen|deeper|slayed|milfy|blackedraw"
    r"|brazzers|realitykings|naughtyamerica|digitalplayground|bangbros"
    r"|evilangel|wicked|newsensations|puretaboo|adulttime"
    r"|sexart|metart|xart|joymii|nubilefilms|passion-?hd"
    r"|vrlatina|vrbangers|badoinkvr|wankzvr|naughtyamericavr"
    r"|legalporno|analvids|privatecom|dorcel"
    r")(?:$|[-_\s.\]/])",
    re.IGNORECASE,
)

# 判据二：欧美片常见的「日期在文件名里」写法 —— Blacked.24.03.15.Title
# 或 Tushy.20.01.01。两位年.两位月.两位日，用点或下划线连
_WESTERN_DATE = re.compile(
    r"(?:^|[-_\s./])(?:19|20)?\d{2}[._-](?:0[1-9]|1[0-2])[._-](?:0[1-9]|[12]\d|3[01])(?:$|[-_\s./])"
)



@dataclass
class MediaFileInfo:
    """一个影片文件解析出来的全部信息。"""
    path: Path
    # 用来抓元数据的番号，分集之间相同。取不出则为空串
    code: str = ""
    # 分集序号，1 起。0 表示不分集（单文件）
    part: int = 0
    # 分集标记的原始写法，回写文件名时沿用用户的风格
    part_raw: str = ""
    # 文件名里读出来的标记
    uncensored: bool = False
    subbed: bool = False
    trailer: bool = False
    # 判定为欧美片命名，没有番号可提（见 looks_western）
    western: bool = False

    @property
    def is_multipart(self) -> bool:
        return self.part > 0

    @property
    def stem_for_output(self) -> str:
        """产物基名：番号 + 分集后缀。没有番号时退回原文件名。"""
        if not self.code:
            return self.path.stem
        if self.part > 0:
            return f"{self.code}-CD{self.part}"
        return self.code


def _strip_noise_tail(text: str) -> str:
    """反复剥掉结尾的画质/来源噪声，直到不再变化。"""
    previous = None
    current = text
    # 噪声可能叠好几层（-1080P-WEB-DL-x264），一次剥一层
    while previous != current:
        previous = current
        current = _NOISE_TAIL.sub("", current)
    return current


def detect_part(stem: str) -> tuple[int, str]:
    """从文件名主干里读出分集序号。返回 (序号, 原始标记)。

    序号 0 表示这不是分集文件。原始标记用于日志与回溯，不参与判断。

    剥噪声之前先剥一次、之后再试一次：有的文件把分集放在噪声后面
    （ABP-554-1080P-CD2），有的放在前面（ABP-554-CD2-1080P），
    两种都要认。
    """
    for candidate in (stem, _strip_noise_tail(stem)):
        if not candidate:
            continue

        # 版本后缀优先判定 —— 它长得像字母式分集，但不是
        if _VERSION_SUFFIX.search(candidate):
            continue

        for pattern in _PART_PATTERNS:
            match = pattern.search(candidate)
            if match:
                number = int(match.group(1))
                # CD0 / part0 不合理，当没匹配到
                if number > 0:
                    return number, match.group(0).strip(" -_.")

        match = _PART_ALPHA.search(candidate)
        if match:
            letter = match.group(1).upper()
            # A=1 B=2 D=4 E=5 F=6。跳过 C 造成的空档是有意的（见 _PART_ALPHA）
            return ord(letter) - ord("A") + 1, match.group(0).strip(" -_.")

        match = _PART_CN_RE.search(candidate)
        if match:
            return _PART_CN[match.group(1)], match.group(1)

        # 纯数字式放最后：它形状最宽，前面几种都不匹配时才轮到它，
        # 且要求剥掉序号后剩下的正好是个完整番号
        match = _PART_NUMERIC.match(candidate)
        if match:
            head = match.group("head").strip(" -_.")
            if _CLEAN_CODE.fullmatch(head.upper()):
                return int(match.group("num")), match.group("num")

    return 0, ""


def strip_part(stem: str) -> str:
    """去掉分集标记，留下能用来查元数据的部分。

    这是 issue #503 的正解：查元数据前必须先把 CD1/CD2 摘掉。
    """
    _, raw = detect_part(stem)
    if not raw:
        return stem
    # 按原始标记的位置切，比再跑一遍正则稳
    index = stem.rfind(raw)
    if index <= 0:
        return stem
    return stem[:index].rstrip(" -_.")


def looks_western(text: str) -> bool:
    """这个文件名看着像欧美片吗。

    像的话就别去提番号 —— 欧美片没有番号体系，硬提会提出个形状对但
    内容错的（"VRLatina_..._LR_180" → LR-180），拿它抓回来的元数据
    整条都是错的，比认不出来危害大得多（MDCng issue #508）。

    两条判据：命中已知欧美厂牌名，或命中欧美惯用的日期式命名。
    刻意都要求"确定性证据"，不用「下划线段数多」这类形状启发式 ——
    "ABP-554_1080p_x264_AAC_中文字幕" 就有 5 段，靠段数判会误伤
    正常的日本片，把能刮的也跳过了。
    """
    if not text:
        return False
    if _WESTERN_STUDIOS.search(text):
        return True
    return bool(_WESTERN_DATE.search(text))


def _restore_version_suffix(code: str, source: str) -> str:
    """把 find_serial_number 切掉的 -C / -UC 版本后缀补回番号。

    source 是已剥掉分集标记的文件名主干。只在后缀紧跟番号时才补，
    避免把文件名里别处的 -C 误接上去。
    """
    flat = source.upper().replace("_", "-")
    index = flat.find(code.upper())
    if index < 0:
        return code
    tail = flat[index + len(code):]
    match = _CODE_SUFFIX.match(tail)
    return f"{code}{match.group(0)}" if match else code


def parse(path: Path | str) -> MediaFileInfo:
    """解析一个影片文件路径。

    番号的取法：先剥分集标记，再交给 find_serial_number。顺序不能反 ——
    find_serial_number 拿到 "ABP-554-CD1" 时，CODE_PATTERN 会框出
    ABP-554 没错，但 "300MIUM-123-CD1" 这类前缀带数字的会被切坏。
    先摘分集能让它面对干净的输入。

    番号在文件名里找不到时，退一步找父目录名 —— 刮削工具的产物常是
    "ABP-554/ABP-554.mp4"，而下载来的常是 "ABP-554/video.mp4"，
    后者只有目录名带番号。
    """
    path = Path(path)
    stem = path.stem

    part, part_raw = detect_part(stem)
    base = strip_part(stem) if part else stem

    # 欧美片先挡掉：它们没有番号，提出来的必然是误判（issue #508）
    if looks_western(f"{path.parent.name}/{stem}"):
        logger.debug(f"[刮削] 判定为欧美片命名，不提取番号: {path.name}")
        return MediaFileInfo(path=path, part=part, part_raw=part_raw, western=True)

    code = find_serial_number(base)
    if not code:
        # 文件名认不出来，试父目录。同样要先剥分集
        parent = path.parent.name
        base = strip_part(parent) or parent
        code = find_serial_number(base)

    # find_serial_number 会把 -C / -UC 版本后缀切掉（它的既有约定，很多
    # 调用方靠这个把版本归一到同一部片）。刮削这条路不一样：中文字幕版与
    # 原版是媒体库里两个独立条目，产物文件名必须带上后缀，否则两版互相覆盖。
    # 所以这里把后缀补回来 —— 从剥完分集的输入里取，而不是改公共函数。
    if code:
        code = _restore_version_suffix(code, base)

    # 标记判定用完整文件名（含噪声段），因为 [中文字幕] 之类常在中间
    full = f"{path.parent.name}/{path.name}"
    normalized_code = get_true_code(code) if code else ""

    return MediaFileInfo(
        path=path,
        code=normalized_code,
        part=part,
        part_raw=part_raw,
        uncensored=bool(
            _UNCENSORED_TOKENS.search(full)
            or (normalized_code and _UNCENSORED_SUFFIX.search(normalized_code))
        ),
        subbed=bool(
            _SUBBED_TOKENS.search(full)
            or (normalized_code and _SUBBED_SUFFIX.search(normalized_code))
        ),
        trailer=bool(_TRAILER_TOKENS.search(full)),
    )


def group_parts(paths: list[Path | str]) -> dict[str, list[MediaFileInfo]]:
    """把一批文件按番号归组，组内按分集序号排序。

    刮削一个目录时用这个：同一番号的多个分集只需抓一次元数据。
    返回的 key 是番号，取不出番号的文件归到空串那组（调用方决定怎么处理）。
    """
    groups: dict[str, list[MediaFileInfo]] = {}
    for raw in paths:
        info = parse(raw)
        groups.setdefault(info.code, []).append(info)

    for items in groups.values():
        # 不分集的排前面，分集按序号。同序号时用中文次序权重兜底
        # （上/中/下 按码位排会错序），再退回路径名保证稳定
        items.sort(key=lambda i: (
            i.part, _PART_CN_ORDER.get(i.part_raw, 0), i.path.name
        ))
        _resolve_collisions(items)
    return groups


def _resolve_collisions(items: list[MediaFileInfo]) -> None:
    """同一番号下分集序号撞号时重排，原地修改。

    撞号的来路有两类：
      上/中/下 三集片 —— 中与下都判成 2（见 _PART_CN）
      混合命名     —— 目录里同时有 ABP-554-CD1.mp4 和 ABP-554-B.mp4

    重排只在确实撞号时发生，且保持已排好的先后顺序 —— 序号本身可能是错的，
    但相对顺序来自文件名排序，是可信的。不撞号就一个字节都不动，避免把
    正确的 CD1/CD3（第二集缺失）硬压成 CD1/CD2。
    """
    numbered = [i for i in items if i.part > 0]
    if len(numbered) < 2:
        return
    if len({i.part for i in numbered}) == len(numbered):
        return

    logger.warning(
        "分集序号撞号，按文件名顺序重排: "
        + ", ".join(f"{i.path.name}(CD{i.part})" for i in numbered)
    )
    for index, info in enumerate(numbered, start=1):
        info.part = index
