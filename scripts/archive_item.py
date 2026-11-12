#!/usr/bin/env python3
"""按指定生效日归档/解除归档资产项。

为什么需要这个脚本：`POST /api/asset-items/{id}/archive` 不接受生效日，一律用
「今天」。而归档清理（例如某个子项自某历史日期起已归零、想让它自那天起退出
计量）需要精确日期。本脚本直接调用 `asset_service.set_item_status()`，与 API
走同一状态机、同一审计路径，只是把 `effective_from` 显式传进去。

默认 dry-run，只打印不动库。加 `--apply` 才写。

用法
----
    # 预演（默认，不写库）
    .venv/bin/python scripts/archive_item.py --item-id 117 --effective-from 2026-01-31

    # 执行
    .venv/bin/python scripts/archive_item.py --item-id 117 --effective-from 2026-01-31 --apply

    # 撤销归档（effective_from 会被清回 NULL）
    .venv/bin/python scripts/archive_item.py --item-id 117 --unarchive --apply

    # 指定库（默认生产库 deploy/data/person_fin.db）
    .venv/bin/python scripts/archive_item.py --item-id 117 --effective-from 2026-01-31 \
        --db data/rebuild/new/person_fin.db --apply

注意
----
* 归档属人裁决动作，写审计（action=asset_item/archive）。脚本不做任何数值修正。
* 归档只终止「该日起」的计量，此前的历史测量值原样保留（ADR-0017）。
* 若该资产项在生效日之后仍有非零有效行，归档会让那些日期少算金额。
  dry-run 会把这类行列出来，请确认是「本该剔除」还是「漏记了终止值」。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到: {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="按指定生效日归档资产项")
    parser.add_argument("--item-id", type=int, required=True, help="资产项 id")
    parser.add_argument("--effective-from", type=_parse_date,
                        help="归档生效日 YYYY-MM-DD（归档时必填）")
    parser.add_argument("--unarchive", action="store_true", help="撤销归档")
    parser.add_argument("--db", default=str(ROOT / "deploy" / "data" / "person_fin.db"),
                        help="数据库路径（默认生产库）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = parser.parse_args()

    if not args.unarchive and args.effective_from is None:
        parser.error("归档必须给 --effective-from（API 默认今天，本脚本不替你猜）")

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"❌ 库不存在: {db_path}")
        return 2

    os.environ["DATABASE_PATH"] = str(db_path)
    sys.path.insert(0, str(BACKEND))

    from sqlalchemy import select                      # noqa: E402
    from database import SessionLocal                  # noqa: E402
    from models import Asset, AssetItem                # noqa: E402
    from services import asset_service                 # noqa: E402

    db = SessionLocal()
    try:
        item = db.get(AssetItem, args.item_id)
        if item is None:
            print(f"❌ 资产项 {args.item_id} 不存在")
            return 2

        print(f"库: {db_path}")
        print(f"资产项: #{item.id} {item.display_name}")
        print(f"现状: status={item.status} archived_effective_from={item.archived_effective_from}")

        if args.unarchive:
            print("将执行: 解除归档（status→active 或 draft，日期清回 NULL）")
        else:
            print(f"将执行: 归档，生效日 {args.effective_from}")

            # 生效日之后仍有非零有效行 → 归档会让那些日期少算金额，提示人工确认
            rows = db.execute(
                select(Asset.asset_date, Asset.amount, Asset.side)
                .where(Asset.asset_item_id == args.item_id,
                       Asset.superseded_by.is_(None),
                       Asset.asset_date >= args.effective_from,
                       Asset.amount != 0)
                .order_by(Asset.asset_date)
            ).all()
            if rows:
                print(f"\n⚠️  生效日之后仍有 {len(rows)} 条非零有效行，归档后这些日期将不再计入：")
                for d, amt, side in rows:
                    print(f"     {d}  {amt}  ({side})")
                print("     若这些值是「已还清」之类的终止测量，应先把它们记为 0 再归档。")
            else:
                print("\n✅ 生效日之后无非零有效行，归档不会有数值影响")

        if not args.apply:
            print("\n（dry-run，未写库。加 --apply 执行）")
            return 0

        status = "active" if args.unarchive else "archived"
        result = asset_service.set_item_status(
            db, args.item_id, status,
            effective_from=None if args.unarchive else args.effective_from,
        )
        if result is None:
            print("❌ 状态机返回 None（不存在？）")
            return 2
        new_item, change = result
        print(f"\n✅ 完成: status={new_item.status} "
              f"archived_effective_from={new_item.archived_effective_from}")
        print(f"   审计: {change}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())