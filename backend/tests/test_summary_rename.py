"""W0-P0 回归：净值 as-of 聚合按资产项身份（改名不双计）。

改名只更新 AssetItem.display_name，不回写历史快照行的 asset_name（时点语义）。
`_sum_as_of` / `_get_latest_records_as_of` 若按 name 分组，改名后同一 item 的
新旧名快照会被当成两个资产 → 净资产双计。本文件锁定：
1) 改名前后同一 item 只计一次（取截至 as_of 的最新有效行）；
2) 改名不改变未变项数值与 side 拆分语义；
3) 无 asset_item_id 的存量行仍按名字分组（双轨窗口回退口径不变）。
"""
from datetime import date
from decimal import Decimal

from models import Asset
from services import analysis_service, asset_service

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)
ZERO = Decimal("0.00")


def _mk(db, name, amount, d, side="asset"):
    return asset_service.create_asset(db, name, Decimal(str(amount)), d, side=side)


def test_rename_does_not_double_count_net_worth(db):
    """同一 item 旧名(1/31) → 改名 → 新名(2/28)：净值只计最新一条。"""
    old = _mk(db, "示例-旧名", 1000, D_JAN)
    asset_service.rename_item(db, old.asset_item_id, "示例-新名")
    _mk(db, "示例-新名", 1200, D_FEB)

    rows = analysis_service._get_latest_records_as_of(db, D_FEB)
    assert len(rows) == 1  # 同一 item 只有一行，不被新旧名拆成两行
    assert rows[0][0] == "示例-新名"  # 最新行的 name 快照

    total, assets, liab = analysis_service._sum_as_of(db, D_FEB)
    assert (total, assets, liab) == (Decimal("1200.00"), Decimal("1200.00"), ZERO)

    # as-of 改名前的时点：仍取旧名快照，单计
    total_jan, assets_jan, _ = analysis_service._sum_as_of(db, D_JAN)
    assert (total_jan, assets_jan) == (Decimal("1000.00"), Decimal("1000.00"))

    # 端点契约同源：summary.net_worth 同口径
    summary = analysis_service.get_summary(db)
    assert summary.net_worth == Decimal("1200.00")


def test_unchanged_item_values_stable_across_rename(db):
    """改名前后未变项数值不变；改名项从旧值过渡到新值，不叠加。"""
    _mk(db, "示例-稳定", 500, D_JAN)
    _mk(db, "示例-稳定", 500, D_FEB)
    target = _mk(db, "示例-待改名", 300, D_JAN)
    asset_service.rename_item(db, target.asset_item_id, "示例-改名后")
    _mk(db, "示例-改名后", 400, D_FEB)

    total_jan, assets_jan, _ = analysis_service._sum_as_of(db, D_JAN)
    assert (total_jan, assets_jan) == (Decimal("800.00"), Decimal("800.00"))

    total_feb, assets_feb, _ = analysis_service._sum_as_of(db, D_FEB)
    assert (total_feb, assets_feb) == (Decimal("900.00"), Decimal("900.00"))


def test_rename_preserves_liability_side_aggregation(db):
    """改名不改变 side 语义：负债项仍只进负债合计，净值扣减一次。"""
    old = _mk(db, "示例-旧负债", 200, D_JAN, side="liability")
    asset_service.rename_item(db, old.asset_item_id, "示例-新负债")
    _mk(db, "示例-新负债", 250, D_FEB, side="liability")

    total, assets, liab = analysis_service._sum_as_of(db, D_FEB)
    assert (total, assets, liab) == (Decimal("-250.00"), ZERO, Decimal("250.00"))


def test_rows_without_item_still_group_by_name(db):
    """双轨窗口回退：无 asset_item_id 的存量行按 name 分组，行为与改前一致。"""
    db.add(Asset(asset_name="示例-无项", amount=Decimal("100"), asset_date=D_JAN))
    db.add(Asset(asset_name="示例-无项", amount=Decimal("150"), asset_date=D_FEB))
    db.commit()

    rows = analysis_service._get_latest_records_as_of(db, D_FEB)
    assert len(rows) == 1
    total, assets, _ = analysis_service._sum_as_of(db, D_FEB)
    assert (total, assets) == (Decimal("150.00"), Decimal("150.00"))