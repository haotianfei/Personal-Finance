#!/usr/bin/env python3
"""asof_diff_report.py —— as-of 净值逐点「改前 vs 改后」只读报告（spec §6.3）。

定位：本脚本是 ADR-0017 D2（归档项退出 as-of 聚合）的发布门禁。它用**裸 SQL**
复刻 `analysis_service._get_latest_records_as_of` 的读法，对每个快照日期并排给出：

  TOTAL_PREV 改前口径：active 行、按身份（asset_item_id，缺失回退 asset_name）取
             截至该日的最近值求和——**不**看 `asset_items.status`
  TOTAL_NOW  改后口径：同上，但排除 `status='archived'` 的资产项（D2）
  DIFF       NOW − PREV

当前库 `archived=0`，预期全表 DIFF=0；任何非零行都应逐项说明（列出「仍残留非零
值的归档项」一栏即为根因清单）。

只读：以 `mode=ro` 打开 SQLite，不写、不加锁、不迁移。仅打印资产项 id/显示名
（公开安全红线：不输出账户号、证据路径、本机路径、金额以外的个人信息）。
金额为该库真实数据，输出仅本地查看，**不要**粘进提交/issue/日志。

用法：
  .venv/bin/python scripts/asof_diff_report.py                 # 默认 data/person_fin_dev.db
  .venv/bin/python scripts/asof_diff_report.py --db /path/x.db
退出码：0＝全部 DIFF 为 0；1＝存在 DIFF（归档排除改变了历史序列）；2＝环境错误。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "person_fin_dev.db"

# 归档项退出口径（ADR-0017 D2 修订版）：**自 archived_effective_from 起**排除，
# 与 backend/services/snapshot_identity.py::active_item_predicate(as_of) 同义——
# 归档前的历史测量值必须保留。DIFF=0 的含义是「归档没有改变任何历史点」。
# `_archived_residuals()` 仅作信息清单（归档项生效日之前的最后一次值），不参与判定。
_ACTIVE_ITEM = (
    "(assets.asset_item_id IS NULL OR assets.asset_item_id NOT IN "
    "(SELECT id FROM asset_items WHERE status = 'archived' "
    " AND (archived_effective_from IS NULL OR archived_effective_from <= :as_of)))"
)
# 行身份（与 snapshot_identity.row_identity() 同义）：i:<item_id> 或 n:<name>
_IDENTITY = "COALESCE('i:' || assets.asset_item_id, 'n:' || assets.asset_name)"

_TOTALS_SQL = f"""
SELECT assets.side, assets.amount
FROM assets
JOIN (
    SELECT {_IDENTITY} AS identity, MAX(assets.asset_date) AS max_date
    FROM assets
    WHERE assets.asset_date <= :as_of
      AND assets.superseded_by IS NULL
      {{item_filter}}
    GROUP BY identity
) AS m
  ON {_IDENTITY} = m.identity
 AND assets.asset_date = m.max_date
WHERE assets.superseded_by IS NULL
  {{item_filter}}
"""

_ARCHIVED_RESIDUAL_SQL = """
SELECT i.id, i.display_name, a.asset_date, a.amount
FROM asset_items AS i
JOIN assets AS a
  ON a.asset_item_id = i.id
 AND a.superseded_by IS NULL
WHERE i.status = 'archived'
  AND a.asset_date = (
        SELECT MAX(a2.asset_date) FROM assets AS a2
        WHERE a2.asset_item_id = i.id AND a2.superseded_by IS NULL)
  AND a.amount <> 0
ORDER BY i.id
"""


def _totals(conn: sqlite3.Connection, as_of: str, *, exclude_archived: bool) -> Decimal:
    """截至 as_of 的净口径合计（负债取负，与 _sum_as_of 同号）。"""
    sql = _TOTALS_SQL.format(
        item_filter=f"AND {_ACTIVE_ITEM}" if exclude_archived else "")
    net = Decimal("0")
    for side, amount in conn.execute(sql, {"as_of": as_of}):
        amt = Decimal(str(amount))
        net += -amt if side == "liability" else amt
    return net


def _snapshot_dates(conn: sqlite3.Connection) -> list[str]:
    """x 轴日期（P0-1 口径）：仅 active 行撑出的日期。"""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT asset_date FROM assets "
        "WHERE superseded_by IS NULL ORDER BY asset_date")]


def _archived_residuals(conn: sqlite3.Connection) -> list[tuple]:
    return list(conn.execute(_ARCHIVED_RESIDUAL_SQL))


def _dual_diff(base_conn: sqlite3.Connection, new_conn: sqlite3.Connection) -> int:
    """双库对比：同一批日期上，基线库与目标库各自的 as-of 净额并排。

    与单库模式的差别：单库比的是「同库两种口径」（归档排除前/后），双库比的是
    「两个库的同一口径」（各自都按 active_item 谓词算）。日期取两库并集——
    清洗若删掉了某个整日，这一行会显式暴露为「该日新库无数据」而不是静默消失。
    """
    dates = sorted(set(_snapshot_dates(base_conn)) | set(_snapshot_dates(new_conn)))
    base_dates = set(_snapshot_dates(base_conn))
    new_dates = set(_snapshot_dates(new_conn))
    print(f"{'DATE':<12}{'BASE':>16}{'NEW':>16}{'DIFF':>14}  NOTE")
    diffs = 0
    for d in dates:
        b = _totals(base_conn, d, exclude_archived=True) if d in base_dates else None
        n = _totals(new_conn, d, exclude_archived=True) if d in new_dates else None
        note = ""
        if b is None:
            note = "新库新增日期"
        elif n is None:
            note = "该日仅存在于基线"
        diff = (n - b) if (b is not None and n is not None) else None
        if diff is not None and diff != 0:
            diffs += 1
        print(f"{d:<12}"
              f"{(str(b) if b is not None else '-'):>16}"
              f"{(str(n) if n is not None else '-'):>16}"
              f"{(str(diff) if diff is not None else '-'):>14}  {note}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="as-of 净值改前/改后只读 diff 报告")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 库路径（只读打开）")
    parser.add_argument("--baseline-db", default="",
                        help="基线库路径：给出则进入双库模式（基线 vs --db），"
                             "逐日并排净额。注意：本脚本不做签核项修正，"
                             "真实的切库门禁请看 scripts/rebuild/s5_verify.py")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"错误：数据库不存在：{db_path.name}", file=sys.stderr)
        return 2

    if args.baseline_db:
        base_path = Path(args.baseline_db)
        if not base_path.is_file():
            print(f"错误：基线库不存在：{base_path.name}", file=sys.stderr)
            return 2
        bconn = sqlite3.connect(f"file:{base_path}?mode=ro", uri=True)
        nconn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            print(f"# as-of 双库对比（只读）base={base_path.name} new={db_path.name}")
            diffs = _dual_diff(bconn, nconn)
            print(f"\n# 结论：DIFF≠0 的日期 {diffs} 个"
                  f"{'（两库一致）' if diffs == 0 else '（需逐项说明）'}")
            return 0 if diffs == 0 else 1
        finally:
            bconn.close()
            nconn.close()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # 环境错误（权限/文件损坏）→ 退出码 2
        print(f"错误：无法只读打开数据库（{type(exc).__name__}）", file=sys.stderr)
        return 2

    try:
        dates = _snapshot_dates(conn)
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM asset_items WHERE status = 'archived'").fetchone()[0]

        print(f"# as-of diff 报告（只读）db={db_path.name}")
        print(f"# 快照日 {len(dates)} 个；archived 资产项 {archived_count} 个")
        print(f"{'DATE':<12}{'TOTAL_PREV':>16}{'TOTAL_NOW':>16}{'DIFF':>14}")

        diffs = 0
        for d in dates:
            prev = _totals(conn, d, exclude_archived=False)
            now = _totals(conn, d, exclude_archived=True)
            diff = now - prev
            if diff != 0:
                diffs += 1
            print(f"{d:<12}{prev:>16}{now:>16}{diff:>14}")

        residuals = _archived_residuals(conn)
        print(f"\n# 归档项残留非零值清单（{len(residuals)} 项）")
        if not residuals:
            print("（无：归档项未残留非零值，D2 对当前库零影响）")
        for item_id, name, last_date, amount in residuals:
            print(f"  #{item_id}\t{name}\t残留 {amount} @ {last_date}")

        print(f"\n# 结论：DIFF≠0 的快照日 {diffs} 个"
              f"{'（门禁通过：当前库全 0）' if diffs == 0 else '（需逐项说明）'}")
        return 0 if diffs == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())