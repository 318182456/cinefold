"""内挂字幕检测：MKV 的 EBML 与 MP4 的 box 解析。

用手工构造的最小封装做断言 —— 真实影片文件太大不适合进仓库，而这两个
格式的轨道信息就在头部，最小样本足以覆盖解析路径。
"""
from __future__ import annotations

from pathlib import Path

from app.services.embedded_subtitle import has_embedded_subtitle


def _vint(n: int) -> bytes:
    """EBML 长度字段（带标记位）。"""
    width = 1
    while n >= (1 << (7 * width)) - 1:
        width += 1
    return ((1 << (7 * width)) | n).to_bytes(width, "big")


def _elem(eid: bytes, payload: bytes) -> bytes:
    return eid + _vint(len(payload)) + payload


def _mkv(*track_types: int) -> bytes:
    """造一个只有轨道表的最小 MKV。track_types 是各轨的 TrackType。"""
    tracks = b"".join(
        _elem(b"\xae", _elem(b"\x83", bytes([t]))) for t in track_types
    )
    head = _elem(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01")
    return head + _elem(b"\x18S\x80\x67", _elem(b"\x16T\xae\x6b", tracks))


def _box(name: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + name + payload


def _mp4(*handlers: bytes) -> bytes:
    """造一个只有 moov/trak/mdia/hdlr 的最小 MP4。"""
    traks = b"".join(
        _box(b"trak", _box(b"mdia", _box(
            b"hdlr", b"\x00" * 8 + h + b"\x00" * 12 + b"und\x00",
        )))
        for h in handlers
    )
    return _box(b"ftyp", b"isom") + _box(b"moov", traks)


def test_mkv_detects_subtitle_track(tmp_path):
    """TrackType 0x11 是字幕轨，认出来。"""
    path = tmp_path / "with.mkv"
    # 0x01 视频 + 0x11 字幕
    path.write_bytes(_mkv(0x01, 0x11))
    assert has_embedded_subtitle(path) is True


def test_mkv_without_subtitle_track(tmp_path):
    """只有视频和音频轨时不该误报。"""
    path = tmp_path / "none.mkv"
    # 0x01 视频 + 0x02 音频
    path.write_bytes(_mkv(0x01, 0x02))
    assert has_embedded_subtitle(path) is False


def test_mp4_detects_subtitle_handlers(tmp_path):
    """sbtl / text / subp / clcp 都算字幕轨。"""
    for idx, handler in enumerate((b"sbtl", b"text", b"subp", b"clcp")):
        path = tmp_path / f"sub{idx}.mp4"
        path.write_bytes(_mp4(b"vide", handler))
        assert has_embedded_subtitle(path) is True, handler


def test_mp4_without_subtitle_handler(tmp_path):
    """只有视频和音频 handler 时不该误报。"""
    path = tmp_path / "none.mp4"
    path.write_bytes(_mp4(b"vide", b"soun"))
    assert has_embedded_subtitle(path) is False


def test_mp4_handles_64bit_box(tmp_path):
    """size==1 表示长度在头后面的 64 位字段，也要能解析。"""
    inner = _box(b"trak", _box(b"mdia", _box(
        b"hdlr", b"\x00" * 8 + b"sbtl" + b"\x00" * 12 + b"und\x00",
    )))
    moov = (1).to_bytes(4, "big") + b"moov" \
        + (16 + len(inner)).to_bytes(8, "big") + inner
    path = tmp_path / "big.mp4"
    path.write_bytes(_box(b"ftyp", b"isom") + moov)
    assert has_embedded_subtitle(path) is True


def test_unknown_and_broken_files_are_false(tmp_path):
    """认不出的格式、截断的文件、空文件都返回 False，且不抛异常。

    宁可漏判也不能误判：漏判只是退回「只认外挂」的老行为，
    误判会让没字幕的片子再也不被补漏任务抓到。
    """
    cases = {
        "junk.mp4": b"\x00\x00\x00\x08moov",
        "trunc.mkv": b"\x1a\x45\xdf\xa3\xff",
        "empty.mp4": b"",
        "text.avi": b"not a video at all",
        # 扩展名不认识的一律为假
        "movie.rmvb": _mkv(0x11),
    }
    for name, payload in cases.items():
        path = tmp_path / name
        path.write_bytes(payload)
        assert has_embedded_subtitle(path) is False, name


def test_missing_file_is_false(tmp_path):
    assert has_embedded_subtitle(tmp_path / "nope.mkv") is False


def test_embedded_counts_for_display_not_for_fetch(tmp_path):
    """内挂算「有字幕」（展示用），但不该拦住简中抓取。

    内挂轨多半是日文，拿它当已有字幕会让简中永远抓不下来。
    """
    from app.services import subtitle

    video = tmp_path / "ABS-100.mkv"
    video.write_bytes(_mkv(0x01, 0x11))

    # 展示口径：认内挂
    assert subtitle.has_subtitle(str(video)) is True
    # 抓取口径：只认外挂，内挂不算
    assert subtitle._has_sidecar(video) is False
