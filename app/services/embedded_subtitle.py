"""检测影片文件里的内挂字幕轨。

外挂字幕（旁边的 .srt/.ass）好认，列一次目录就够。内挂字幕封装在容器
内部，不读文件头看不出来 —— 而把内挂当成「缺字幕」有两重代价：页面
显示是错的，补漏任务还会一遍遍去抓已经有字幕的片子。

这里只读文件头部的几百 KB，不解码、不扫全文件，也不依赖 ffmpeg：
装 ffmpeg 要给镜像加 80MB，而每个文件起一次子进程比读头慢一个数量级，
回填几千条时差别很明显。

只认 MKV/WebM 与 MP4/MOV 两族，覆盖了媒体库里绝大多数封装。
认不出的一律返回 False（当作没有内挂）—— 那只是退回到原来的行为，
不会把没字幕的片子误标成有字幕。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

# 读多少字节找字幕轨。轨道信息在头部，正常封装几百 KB 足够；
# 读太多会把 NAS 往返时间拖上去
_HEAD_BYTES = 2 * 1024 * 1024

# EBML 元素 ID（Matroska）
_ID_SEGMENT = b"\x18\x53\x80\x67"
_ID_TRACKS = b"\x16\x54\xae\x6b"
_ID_TRACK_ENTRY = b"\xae"
_ID_TRACK_TYPE = b"\x83"
# Matroska 的 TrackType：0x11 是字幕
_TRACK_TYPE_SUBTITLE = 0x11

_MKV_SUFFIXES = (".mkv", ".webm", ".mka")
_MP4_SUFFIXES = (".mp4", ".m4v", ".mov", ".m4a")


def has_embedded_subtitle(path: str | Path) -> bool:
    """文件里有没有内挂字幕轨。

    读不出、格式不认、文件不在 —— 一律返回 False。这个判断只用来
    「别把有内挂的片子标成缺字幕」，宁可漏判也不能误判。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        if suffix in _MKV_SUFFIXES:
            return _mkv_has_subtitle(p)
        if suffix in _MP4_SUFFIXES:
            return _mp4_has_subtitle(p)
    except (OSError, ValueError, IndexError):
        # 截断的文件、非法封装都会走到这里。不是异常情况，不用惊动日志
        return False
    except Exception as exc:
        # 解析逻辑自己的 bug 不该让调用方崩掉 —— 它只是想知道有没有字幕
        logger.debug(f"[字幕] 内挂检测失败 {p.name}: {exc}")
        return False
    return False


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(_HEAD_BYTES)


# ---------- MKV / WebM (EBML) ----------

def _ebml_num(data: bytes, pos: int, keep_marker: bool) -> tuple[int, int]:
    """读一个 EBML 变长整数，返回（值, 下一个位置）。

    首字节的前导零个数决定总长度：0b1xxxxxxx 占 1 字节，
    0b01xxxxxx 占 2 字节，依此类推。元素 ID 要保留标记位（keep_marker），
    长度字段则要把标记位去掉。
    """
    if pos >= len(data):
        raise IndexError("EBML 越界")

    first = data[pos]
    if first == 0:
        raise ValueError("非法 EBML 前导字节")

    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("EBML 变长整数过长")

    if pos + length > len(data):
        raise IndexError("EBML 越界")

    if keep_marker:
        value = int.from_bytes(data[pos:pos + length], "big")
    else:
        value = first & (mask - 1)
        for byte in data[pos + 1:pos + length]:
            value = (value << 8) | byte
    return value, pos + length


def _ebml_id(data: bytes, pos: int) -> tuple[bytes, int]:
    """读元素 ID，返回原始字节（便于直接和常量比对）。"""
    _, end = _ebml_num(data, pos, keep_marker=True)
    return data[pos:end], end


def _mkv_has_subtitle(path: Path) -> bool:
    """MKV 的轨道表里有 TrackType == 0x11 的轨吗。

    结构：Segment → Tracks → TrackEntry → TrackType。
    只钻这三层，其他元素整段跳过。
    """
    data = _read_head(path)
    # EBML 魔数。不匹配说明不是 Matroska，交给调用方当作没有内挂
    if not data.startswith(b"\x1a\x45\xdf\xa3"):
        return False

    tracks = _ebml_find(data, 0, len(data), [_ID_SEGMENT, _ID_TRACKS])
    if tracks is None:
        return False

    start, end = tracks
    pos = start
    while pos < end:
        elem_id, pos = _ebml_id(data, pos)
        size, pos = _ebml_num(data, pos, keep_marker=False)
        stop = min(pos + size, end)
        if elem_id == _ID_TRACK_ENTRY and _track_is_subtitle(data, pos, stop):
            return True
        pos = stop
    return False


def _track_is_subtitle(data: bytes, start: int, end: int) -> bool:
    """一个 TrackEntry 是不是字幕轨。"""
    pos = start
    while pos < end:
        elem_id, pos = _ebml_id(data, pos)
        size, pos = _ebml_num(data, pos, keep_marker=False)
        stop = min(pos + size, end)
        if elem_id == _ID_TRACK_TYPE:
            value = int.from_bytes(data[pos:stop], "big")
            if value == _TRACK_TYPE_SUBTITLE:
                return True
        pos = stop
    return False


def _ebml_find(
    data: bytes, start: int, end: int, path: list[bytes]
) -> tuple[int, int] | None:
    """按 ID 路径逐层往下找，返回最内层元素的（起点, 终点）。"""
    if not path:
        return start, end

    want, rest = path[0], path[1:]
    pos = start
    while pos < end:
        elem_id, pos = _ebml_id(data, pos)
        size, pos = _ebml_num(data, pos, keep_marker=False)
        # 未知长度（全 1）的元素一路读到底，Segment 常见这种写法
        stop = end if size >= (1 << 56) else min(pos + size, end)
        if elem_id == want:
            return _ebml_find(data, pos, stop, rest)
        pos = stop
    return None


# ---------- MP4 / MOV (ISO BMFF) ----------

# 字幕轨的 handler type。tx3g 走 'text'，TTML/WebVTT 走 'sbtl'，
# CEA 字幕走 'clcp'
_SUBTITLE_HANDLERS = (b"sbtl", b"text", b"subp", b"clcp")


def _mp4_has_subtitle(path: Path) -> bool:
    """MP4 的 moov 里有字幕 handler 的轨道吗。

    结构：moov → trak → mdia → hdlr，hdlr 里第 8..12 字节是 handler type。
    """
    data = _read_head(path)
    moov = _mp4_find(data, 0, len(data), b"moov")
    if moov is None:
        return False

    start, end = moov
    pos = start
    while pos < end:
        box = _mp4_box(data, pos, end)
        if box is None:
            return False
        name, body_start, body_end, next_pos = box
        if name == b"trak" and _trak_is_subtitle(data, body_start, body_end):
            return True
        pos = next_pos
    return False


def _trak_is_subtitle(data: bytes, start: int, end: int) -> bool:
    found = _mp4_find(data, start, end, b"mdia")
    if found is None:
        return False
    hdlr = _mp4_find(data, found[0], found[1], b"hdlr")
    if hdlr is None:
        return False

    body_start, body_end = hdlr
    # hdlr: version(1) + flags(3) + pre_defined(4) + handler_type(4)
    handler = data[body_start + 8:body_start + 12]
    return handler in _SUBTITLE_HANDLERS and body_end >= body_start + 12


def _mp4_box(
    data: bytes, pos: int, limit: int
) -> tuple[bytes, int, int, int] | None:
    """读一个 box 头，返回（类型, 内容起点, 内容终点, 下一个 box 位置）。"""
    if pos + 8 > limit:
        return None

    size = int.from_bytes(data[pos:pos + 4], "big")
    name = data[pos + 4:pos + 8]
    body = pos + 8

    if size == 1:
        # 64 位长度，紧跟在头后面
        if body + 8 > limit:
            return None
        size = int.from_bytes(data[body:body + 8], "big")
        body += 8
    elif size == 0:
        # 一直到文件末尾
        size = limit - pos

    if size < 8:
        return None
    end = min(pos + size, limit)
    return name, body, end, end


def _mp4_find(
    data: bytes, start: int, end: int, want: bytes
) -> tuple[int, int] | None:
    """在同一层里找指定类型的 box，返回它的（内容起点, 内容终点）。"""
    pos = start
    while pos < end:
        box = _mp4_box(data, pos, end)
        if box is None:
            return None
        name, body_start, body_end, next_pos = box
        if name == want:
            return body_start, body_end
        pos = next_pos
    return None
