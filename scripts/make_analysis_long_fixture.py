#!/usr/bin/env python3
"""在数据库副本上生成「日粒度 N 期」合成资产数据，用于前端长列表/性能实测。

不碰真实库：只接受一份**副本**路径，就地清空 `assets` 后灌入合成行。
合成值全部为虚构递增/波动数列，不含任何真实金额、机构、账号。

用法：
    # 1) 先用 backup API 取一致快照副本（勿用 cp，运行库可能带未合并 WAL）
    python3 scripts/make_analysis_long_fixture.py --src data/person_fin_dev.db \
        --dst /tmp/fixture.db --days 730 --items 8

    # 2) 起后端指向副本，前端切「日」粒度即可看到全部账期
    DATABASE_PATH=/tmp/fixture.db python3 -m uvicorn main:app --port 8010

约束（见 backend/models.py:358/870、migrations/0026:150-179）：
  - assets.side 必须与所属 asset_classes.side_scope 一致（触发器）
  - asset_items.asset_class_id 必须是叶子类（触发器）
  - status='active' 时四维必填；liquidity_days 白名单
  - 同一 item 同一天只能有一条 superseded_by IS NULL 的行
  - created_at/updated_at 无 DDL 默认值，必须显式写入
  - 不写 audit_log（审计 hash 链保持 ok）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta

LIQUIDITY_CHOICES = [0, 1, 7, 30, 90, 180, 365, 9999]


def backup_copy(src: str, dst: str) -> None:
    """用 sqlite backup API 取一致快照（避开 WAL 未合并导致的陈旧副本）。"""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


def pick_leaf_classes(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """返回 {'asset': [叶子类 id...], 'liability': [...]}，只取没有子节点的类。"""
    rows = conn.execute(
        "select id, parent_id, side_scope from asset_classes order by id"
    ).fetchall()
    has_child = {r[1] for r in rows if r[1] is not None}
    out: dict[str, list[int]] = {"asset": [], "liability": []}
    for cid, _parent, side in rows:
        if cid in has_child:
            continue
        if side in out:
            out[side].append(cid)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="真实库路径（只读打开）")
    ap.add_argument("--dst", required=True, help="副本输出路径（会被覆盖）")
    ap.add_argument("--days", type=int, default=730, help="账期数（日粒度）")
    ap.add_argument("--items", type=int, default=8, help="合成资产项数量")
    ap.add_argument("--end", default="2026-11-08", help="最后一期日期 YYYY-MM-DD")
    ap.add_argument("--in-place", action="store_true", help="跳过复制，直接改写 --dst")
    args = ap.parse_args()

    if not args.in_place:
        backup_copy(args.src, args.dst)

    conn = sqlite3.connect(args.dst)
    conn.execute("pragma foreign_keys = on")

    leaves = pick_leaf_classes(conn)
    if not leaves["asset"]:
        print("错误：库里没有 asset 侧的叶子类，无法造数", file=sys.stderr)
        return 1

    # 6 资产 + 2 负债（负债可缺席时全部当资产，避免触发器拒绝）
    n_asset = args.items if not leaves["liability"] else max(1, args.items - 2)
    n_liab = 0 if not leaves["liability"] else args.items - n_asset

    end = date.fromisoformat(args.end)
    dates = [end - timedelta(days=args.days - 1 - i) for i in range(args.days)]

    inst = conn.execute("select id from institutions order by id limit 1").fetchone()
    owner = conn.execute("select id from members order by id limit 1").fetchone()
    if inst is None or owner is None:
        print("错误：缺少 institutions / members 基础行", file=sys.stderr)
        return 1
    institution_id, owner_id = inst[0], owner[0]

    with conn:
        conn.execute("delete from assets")

        created = f"{end.isoformat()}T00:00:00"
        item_ids: list[tuple[int, str]] = []  # (id, side)
        for i in range(n_asset + n_liab):
            side = "asset" if i < n_asset else "liability"
            pool = leaves[side]
            class_id = pool[i % len(pool)]
            cur = conn.execute(
                """
                insert into asset_items
                    (display_name, channel, status, legacy_name, archived_effective_from,
                     asset_class_id, liquidity_days, institution_id, owner_id,
                     channel_id, risk_level, created_at, updated_at)
                values (?, null, 'active', null, null, ?, ?, ?, ?, null, null, ?, ?)
                """,
                (
                    f"合成标的 {i + 1:02d}",
                    class_id,
                    LIQUIDITY_CHOICES[i % len(LIQUIDITY_CHOICES)],
                    institution_id,
                    owner_id,
                    created,
                    created,
                ),
            )
            item_ids.append((int(cur.lastrowid), side))

        payload = []
        for idx, (item_id, side) in enumerate(item_ids):
            base = 10_000.0 * (idx + 1) * (1.0 if side == "asset" else 0.12)
            for d_i, day in enumerate(dates):
                # 确定性波动：慢趋势 + 周期项，避免等差数列
                drift = 1.0 + 0.0004 * d_i
                wave = 1.0 + 0.03 * ((d_i + idx * 7) % 60) / 60.0
                amount = round(base * drift * wave, 2)
                payload.append(
                    (
                        f"合成标的 {idx + 1:02d}",
                        amount,
                        side,
                        day.isoformat(),
                        None,
                        item_id,
                        None,
                        None,
                        created,
                        created,
                    )
                )

        conn.executemany(
            """
            insert into assets
                (asset_name, amount, side, asset_date, source_evidence_id,
                 asset_item_id, superseded_by, cost_basis, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )

    fk = conn.execute("pragma foreign_key_check").fetchall()
    n_items = conn.execute("select count(*) from asset_items").fetchone()[0]
    n_rows = conn.execute("select count(*) from assets").fetchone()[0]
    n_dates = conn.execute(
        "select count(distinct asset_date) from assets where superseded_by is null"
    ).fetchone()[0]
    span = conn.execute(
        "select min(asset_date), max(asset_date) from assets"
    ).fetchone()
    conn.close()

    print(f"副本: {args.dst}")
    print(f"assets 行数: {n_rows}（{args.items} 项 × {args.days} 日）")
    print(f"distinct 账期: {n_dates}  区间: {span[0]} → {span[1]}")
    print(f"asset_items 行数: {n_items}（含原有）")
    print(f"foreign_key_check: {'OK' if not fk else fk[:5]}")
    return 0 if not fk else 1


if __name__ == "__main__":
    raise SystemExit(main())