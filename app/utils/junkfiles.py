"""挑出种子里的广告垃圾文件。

公开站的种子常夹带广告：跳转网页、"最新地址"文本、几十 MB 的引流视频。
它们不该占带宽和磁盘，更麻烦的是会引发连锁 —— 刮削工具（MDC-NG 等）
把它们清理掉后，媒体服务器发出删除事件，联动删除又把整部片的种子和
正片一起删了。

判据以体积为主，不做关键词黑名单：广告的花样无穷，但正片总是最大的
那个文件。只有实在无法按体积判断时才看扩展名。
"""
from __future__ import annotations

from pathlib import PurePath

from loguru import logger

# 影片扩展名。与 medialink.VIDEO_SUFFIXES 保持一致，
# 但这里不导入它 —— 那个模块拉的依赖太重，工具函数不该牵连进来
VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts",
    ".flv", ".rmvb", ".iso", ".mpg", ".mpeg", ".m4v",
}

# 非影片文件超过这个大小就留着：可能是用户要的字幕包、花絮压缩包。
# 广告网页和文本都只有几 KB
KEEP_NON_VIDEO_BYTES = 50 * 1024 * 1024

# 影片文件小于最大影片的这个比例即视为引流片。
# 取 0.2 而不是更高：多集式种子（一部片分 CD1/CD2）各集大小接近，
# 定得高会把正片的某一集误判成广告
JUNK_VIDEO_RATIO = 0.2

# 但比例再小也得有绝对下限兜底：正片本身只有几百 MB 时，
# 20% 也才几十 MB，仍可能是有效内容
JUNK_VIDEO_MAX_BYTES = 300 * 1024 * 1024


def pick_junk_files(files: list[dict]) -> list[str]:
    """从种子文件清单里挑出广告垃圾，返回它们的路径。

    files 形如 [{"path": 绝对路径, "size": 字节数}]。
    判断不了就一个都不挑 —— 少标记只是多占点磁盘，标错会让正片下不下来。
    """
    usable = [
        f for f in files
        if isinstance(f, dict) and f.get("path") and (f.get("size") or 0) >= 0
    ]
    if len(usable) <= 1:
        # 单文件种子没什么可挑的，全是正片
        return []

    videos = [f for f in usable if PurePath(f["path"]).suffix.lower() in VIDEO_SUFFIXES]
    if not videos:
        # 一个影片都没有说明这个种子不是影片种子（或扩展名不认识），
        # 此时无从判断哪些是广告，一律留着
        return []

    largest = max(int(f["size"] or 0) for f in videos)
    if largest <= 0:
        return []

    junk: list[str] = []
    for f in usable:
        path = f["path"]
        size = int(f.get("size") or 0)
        suffix = PurePath(path).suffix.lower()

        if suffix in VIDEO_SUFFIXES:
            # 影片：明显小于正片的是引流片
            if size < largest * JUNK_VIDEO_RATIO and size < JUNK_VIDEO_MAX_BYTES:
                junk.append(path)
        elif size < KEEP_NON_VIDEO_BYTES:
            # 非影片小文件：广告网页、"最新地址"文本、图片
            junk.append(path)

    # 兜底：绝不能把所有文件都标记掉。真出现这种情况说明判据不适用于
    # 这个种子，宁可全留着
    if len(junk) >= len(usable):
        logger.warning(f"广告识别把全部 {len(usable)} 个文件都判成垃圾，放弃标记")
        return []

    return junk
