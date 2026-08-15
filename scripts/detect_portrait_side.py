"""扫描已缓存的封面，判断人像在哪半边并写回数据库。

前端靠 code.portrait_side 决定卡片上封面往哪边偏。这个字段只在封面下载时
顺手算出来，存量记录是空的，得跑一次这个脚本补上。

只读图片、只写数据库的一个字段，不动任何图片文件，重复跑是安全的。

已经被早期版本裁成竖版的封面会被判成 none（宽高比不满足双拼条件），
卡片上按普通封面居中显示 —— 那本来就已经是人像半边了，不用再偏。
想拿回完整原图的话，用卡片上的重抓按钮。

用法（在挂了 /data 的环境里跑，比如容器内）：
    python -m scripts.detect_portrait_side --dry-run   # 只统计，不写库
    python -m scripts.detect_portrait_side            # 实际写库
    python -m scripts.detect_portrait_side --all      # 连已判断过的一起重算
    python -m scripts.detect_portrait_side --limit 100
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

# Windows 控制台默认编码（cp932/gbk）打不出中文，容器里是 utf-8 无影响
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.database.models import Code
from app.database.session import session_scope
from app.utils import imagecache, imgcrop


def main() -> int:
    parser = argparse.ArgumentParser(description="判断封面人像面并写回数据库")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--all", action="store_true", help="连已判断过的记录一起重算")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0 为不限）")
    args = parser.parse_args()

    with session_scope() as session:
        query = select(Code.code, Code.local_banner).where(
            (Code.local_banner.isnot(None)) & (Code.local_banner != "")
        )
        if not args.all:
            # 只补没判断过的。--all 用于换了判断算法后重算
            query = query.where(
                (Code.portrait_side.is_(None)) | (Code.portrait_side == "")
            )
        if args.limit:
            query = query.limit(args.limit)
        rows = session.execute(query).all()

    if not rows:
        print("没有需要处理的记录")
        return 0

    print(f"待处理 {len(rows)} 条{'（dry-run，不写库）' if args.dry_run else ''}")

    tally: dict[str, int] = {}
    updates: list[tuple[str, str]] = []
    missing = 0

    for code, local_banner in rows:
        relative = (local_banner or "").split(",")[0]
        path = imagecache.resolve_relative(relative)
        if path is None:
            missing += 1
            continue

        side = imgcrop.detect_from_file(path)
        tally[side] = tally.get(side, 0) + 1
        updates.append((code, side))

    if not args.dry_run and updates:
        with session_scope() as session:
            for code, side in updates:
                row = session.get(Code, code)
                if row is not None:
                    row.portrait_side = side

    print("\n判断结果：")
    for side in (imgcrop.LEFT, imgcrop.RIGHT, imgcrop.NONE):
        print(f"  {side:<6} {tally.get(side, 0)}")
    if missing:
        print(f"  图片文件不存在，跳过 {missing} 条")

    if args.dry_run:
        print("\n这是 dry-run，去掉 --dry-run 才会真正写库")
    else:
        print(f"\n已更新 {len(updates)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
