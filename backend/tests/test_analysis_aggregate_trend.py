"""汇总项在**趋势口径**里的表现（ADR-0029 D1/D2/D14；票 03 验收）。

两条硬约束，一处断言：

1. **总额线＝叶子之和**：父 100 ＋ 子 60 ＋ 子 40，同日在册 → 总额 100（不是 200）。
   `get_total_trend` 走 `_latest_records_by_as_of` 的批量 Python 判定，父项 id 必须
   从 `snapshot_identity.aggregate_item_ids()` 取（P3：这里传 `is_aggregate=True`），
   否则总额线比净值出口多算一遍父项——那正是本组用例要防的口径分叉。
2. **父项自己的曲线照常出点**（D14）：`get_trend_by_item` 走展示口径
   （`leaves_only=False`），父项金额仍是 100；隐藏它会让用户以为数据丢了。
3. 附带护栏：建构成边**不该制造漂移假警报**。父项退出合计后被沿用（无后继快照）时，
   若漂移诊断还用「仍在计量」的旧判据，它会被报成「凭空消失的持仓」
   （`drift_amount` 100、`drift_suspect` True）。`is_aggregate` 传进去后必须消失。

数据一律合成（「示例-」、虚构金额）。
"""
from datetime import date
from decimal import Decimal

import pytest

from models import AssetItem
from services import analysis_service, composition_service
from services.asset_service import create_asset

# 三个**真实快照日**（趋势 x 轴只由实际行日期撑出，不补空账期）：
# 首日全套、中间日只有一张明细更新、末日两张明细更新 → 中间日正好是「父项被沿用且
# 后继快照存在于更晚日期」的经典 vanished 形状。
D1, D_MID, D_LAST = date(2026, 5, 31), date(2026, 7, 31), date(2026, 8, 31)


def mk_item(db, name) -> AssetItem:
    """资产项；默认 draft（四维全空）——本组只关心趋势口径。"""
    item = AssetItem(display_name=name, status="draft")
    db.add(item)
    db.flush()
    db.commit()
    return item


def add_row(db, item, amount, on_date):
    return create_asset(db, item.display_name, Decimal(str(amount)), on_date,
                        side="asset", item_id=item.id)


@pytest.fixture()
def trio(db):
    """父 100（仅 D1）＋ 子 60/40（D1）＋ 子 65（D_MID）＋ 子 70/45（D_LAST）。

    父项在 D_MID/D_LAST 无新快照 → 沿用 D1 的 100。这个形状同时用于验漂移假警报：
    D_MID 点上父项「被沿用且后继快照在更晚日期」是典型的 vanished 候选。
    """
    parent = mk_item(db, "示例-贵金属汇总")
    child_a = mk_item(db, "示例-金产品A")
    child_b = mk_item(db, "示例-金产品B")
    add_row(db, parent, 100, D1)
    add_row(db, child_a, 60, D1)
    add_row(db, child_b, 40, D1)
    add_row(db, child_a, 65, D_MID)
    add_row(db, child_a, 70, D_LAST)
    add_row(db, child_b, 45, D_LAST)
    return {"parent": parent, "child_a": child_a, "child_b": child_b}


def _totals(db) -> dict:
    return {pt.date: pt.total_amount for pt in analysis_service.get_total_trend(db, "day")}


def test_total_trend_equals_leaf_sum_but_parent_curve_survives(db, trio):
    parent, child_a, child_b = trio["parent"], trio["child_a"], trio["child_b"]

    # 建构成前：三项各自独立计量（父项也是一笔，且中间日沿用）
    assert _totals(db) == {D1: Decimal("200.00"), D_MID: Decimal("205.00"),
                           D_LAST: Decimal("215.00")}

    composition_service.create_edge(db, parent.id, child_a.id)
    composition_service.create_edge(db, parent.id, child_b.id)

    # 建构成后：总额线＝叶子之和（D1 60+40、D_MID 65+40、D_LAST 70+45）
    totals = _totals(db)
    assert totals == {D1: Decimal("100.00"), D_MID: Decimal("105.00"),
                      D_LAST: Decimal("115.00")}
    # 与净值出口同源（「四出口一致」的登记表在 test_composition_service 锁死）
    assert analysis_service._sum_as_of(db, D1)[0] == Decimal("100.00")
    assert analysis_service._sum_as_of(db, D_LAST)[0] == Decimal("115.00")

    # 父项自己的曲线仍照常出点（展示口径，D14）：D1 测量 100，此后沿用 100
    curve = {p.date: p.amount for p in analysis_service.get_trend_by_item(
        db, parent.display_name, "day")}
    assert curve == {D1: Decimal("100.00"), D_MID: Decimal("100.00"),
                     D_LAST: Decimal("100.00")}

    # 子项曲线不受影响
    child_curve = {p.date: p.amount for p in analysis_service.get_trend_by_item(
        db, child_a.display_name, "day")}
    assert child_curve == {D1: Decimal("60.00"), D_MID: Decimal("65.00"),
                           D_LAST: Decimal("70.00")}


def test_creating_composition_does_not_fake_identity_drift(db, trio):
    """建构成边不是「持仓消失」：父项不得进 vanished，也不得把该点标成疑似漂移。"""
    parent, child_a, child_b = trio["parent"], trio["child_a"], trio["child_b"]
    composition_service.create_edge(db, parent.id, child_a.id)
    composition_service.create_edge(db, parent.id, child_b.id)

    points = {pt.date: pt for pt in analysis_service.get_total_trend(db, "day")}
    mid = points[D_MID]
    assert [v.name for v in mid.vanished] == []
    assert [a.name for a in mid.added] == []
    assert mid.drift_amount == Decimal("0")
    assert mid.drift_suspect is False


def test_removing_compositions_restores_parent_in_total_trend(db, trio):
    """解除构成 → 父项**自动**回到总额线（无开关、无例外标记，D4）。"""
    parent, child_a, child_b = trio["parent"], trio["child_a"], trio["child_b"]
    composition_service.create_edge(db, parent.id, child_a.id)
    composition_service.create_edge(db, parent.id, child_b.id)
    assert _totals(db)[D1] == Decimal("100.00")

    composition_service.remove_edge(db, child_a.id)
    composition_service.remove_edge(db, child_b.id)
    assert _totals(db)[D1] == Decimal("200.00")