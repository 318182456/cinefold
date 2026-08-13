"""诊断「下载中」为何虚高。只读，不改任何数据。

看板的「下载中」= code.status == DOWNLOADING 的行数。这个状态只有一条
自动出口：sync_download_status() 在下载器里按 hash 查到任务且已完成，才
推进到 DOWNLOADED。种子一旦从下载器删除，或被改了分类（qb 的
monitor_torrent 带 category 过滤），番号就永久卡在「下载中」。

本脚本把 DOWNLOADING 的番号按「卡住的原因」分类计数：

  无 History 记录    —— 状态被置为下载中但没登记过 hash，只能重新订阅
  下载器中查不到     —— 种子已删，或不在配置的 category 下
  在下载器中·未完成  —— 正常在下（真·下载中）
  在下载器中·已完成  —— 下完了但同步没跑到，下轮定时任务会自动推进

用法（在能连到下载器的环境里跑，比如容器内）：
    python -m scripts.diag_downloading
    python -m scripts.diag_downloading --list      # 附带列出各类番号
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from sqlalchemy import select

# Windows 控制台默认编码（cp932/gbk）打不出中文，容器里是 utf-8 无影响
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.database.models import Code, CodeStatus, History
from app.database.session import session_scope
from app.modules import downloadclient


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断卡在「下载中」的番号")
    parser.add_argument(
        "--list", action="store_true", help="列出每一类的番号，不只是计数"
    )
    parser.add_argument(
        "--limit", type=int, default=30, help="--list 时每类最多列多少条"
    )
    args = parser.parse_args()

    with session_scope() as session:
        stuck_codes = session.scalars(
            select(Code.code).where(Code.status == CodeStatus.DOWNLOADING)
        ).all()

        # 一个番号可能下过多个种子（换源重下），全都收起来：只要有任意一个
        # 还在下载器里，这个番号就不算僵尸
        rows = session.execute(
            select(History.code, History.hash).where(History.code.in_(stuck_codes))
        ).all() if stuck_codes else []

    code_to_hashes: dict[str, list[str]] = defaultdict(list)
    for code, torrent_hash in rows:
        code_to_hashes[code].append(torrent_hash)

    print(f"状态为「下载中」的番号: {len(stuck_codes)}")
    if not stuck_codes:
        return

    no_history = [c for c in stuck_codes if not code_to_hashes.get(c)]

    client = downloadclient.get_download_client()
    if client is None:
        print("未配置下载器，无法区分「种子已删」与「正在下载」")
        print(f"  无 History 记录: {len(no_history)}")
        return

    all_hashes = [h for hashes in code_to_hashes.values() for h in hashes]
    # 传 hash 列表查一次，再不带 hash 全量查一次。两者的差集就是「种子还在
    # 下载器里，但不在 cinefold 配置的 category 下」—— 这类不是种子被删，
    # 是分类被改，处理方式完全不同
    states = client.monitor_torrent(all_hashes)
    state_by_hash = {s.get("hash", ""): s for s in states}

    live_all = {s.get("hash", "") for s in client.monitor_torrent()}

    in_client_done: list[str] = []
    in_client_downloading: list[str] = []
    wrong_category: list[str] = []
    missing: list[str] = []

    for code in stuck_codes:
        hashes = code_to_hashes.get(code)
        if not hashes:
            continue
        found = [state_by_hash[h] for h in hashes if h in state_by_hash]
        if not found:
            if any(h in live_all for h in hashes):
                wrong_category.append(code)
            else:
                missing.append(code)
        elif any(s.get("completed") for s in found):
            in_client_done.append(code)
        else:
            in_client_downloading.append(code)

    buckets = [
        ("无 History 记录（需重新订阅）", no_history),
        ("下载器中查不到（种子已删 → 僵尸）", missing),
        ("在下载器但分类不符（category 过滤掉了）", wrong_category),
        ("在下载器中·正在下载（正常）", in_client_downloading),
        ("在下载器中·已完成（下轮同步会推进）", in_client_done),
    ]

    print()
    for label, codes in buckets:
        print(f"  {label}: {len(codes)}")
        if args.list and codes:
            for code in codes[: args.limit]:
                print(f"      {code}")
            if len(codes) > args.limit:
                print(f"      ... 另有 {len(codes) - args.limit} 条")

    zombie = len(no_history) + len(missing)
    print()
    print(f"真正卡死需要人工处理的: {zombie}")
    print(f"看板「下载中」的合理值应为: {len(stuck_codes) - zombie}")


if __name__ == "__main__":
    main()
