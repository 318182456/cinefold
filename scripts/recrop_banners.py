"""把已经缓存下来的封面重新裁一遍，取人像那半边。

裁剪是在 imagecache.store() 里做的，只对新下载的图生效。这个功能上线前
落盘的封面还是整张双拼图，得跑一次这个脚本补上。

裁剪覆盖原图，没有备份。判断不了的图会原样跳过（见 utils/imgcrop.py），
所以重复跑是安全的 —— 已经裁成竖版的图宽高比不再满足双拼条件，第二次
跑会被直接跳过，不会越裁越窄。

用法（在挂了 /data 的环境里跑，比如容器内）：
    python -m scripts.recrop_banners --dry-run    # 只统计，不写盘
    python -m scripts.recrop_banners             # 实际裁剪
    python -m scripts.recrop_banners --limit 100 # 先拿一小批验证效果
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认编码（cp932/gbk）打不出中文，容器里是 utf-8 无影响
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.utils import imgcrop
from app.utils.imagecache import MIN_IMAGE_BYTES, MISC_DIR, PIC_DIR


def iter_banners():
    """遍历 pics 下所有番号目录的封面文件。

    _misc 是演员头像等非封面图，不参与裁剪。
    """
    if not PIC_DIR.is_dir():
        return

    for entry in sorted(PIC_DIR.iterdir()):
        if not entry.is_dir() or entry.name == MISC_DIR:
            continue
        for path in entry.glob("banner.*"):
            if path.is_file() and path.stat().st_size >= MIN_IMAGE_BYTES:
                yield path


def recrop(path: Path, dry_run: bool) -> bool:
    """裁一张图，返回是否真的裁了。"""
    try:
        original = path.read_bytes()
    except OSError as exc:
        print(f"  读取失败 {path.name}: {exc}")
        return False

    cropped = imgcrop.pick_portrait_half(original)
    if cropped is original:
        return False

    if dry_run:
        return True

    try:
        # 裁完统一是 JPEG，.webp 之类的壳得换掉，否则 image-local 会按
        # 后缀返回错误的 MIME
        target = path.with_suffix(".jpg")
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(cropped)
        tmp.replace(target)
        if path != target:
            path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"  写入失败 {path.name}: {exc}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="重新裁剪已缓存的封面")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少张（0 为不限）")
    args = parser.parse_args()

    if not PIC_DIR.is_dir():
        print(f"图片缓存目录不存在: {PIC_DIR}")
        return 1

    print(f"扫描 {PIC_DIR}{'（dry-run，不写盘）' if args.dry_run else ''}")

    scanned = cropped = 0
    for path in iter_banners():
        scanned += 1
        if recrop(path, args.dry_run):
            cropped += 1
            print(f"  裁剪 {path.parent.name}")
        if args.limit and scanned >= args.limit:
            break

    skipped = scanned - cropped
    print(f"\n共扫描 {scanned} 张，裁剪 {cropped} 张，保持原样 {skipped} 张")
    if args.dry_run and cropped:
        print("这是 dry-run，去掉 --dry-run 才会真正写盘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
