"""总额同源不变量（ADR-0020 D4）——三处独立求和实现与 `_sum_as_of` 对齐。

`analysis_service._sum_as_of`（＝`get_summary` 的 total_assets/total_liabilities/
net_worth）是唯一权威总额。本文件钉死 D4 表格里修掉的三处不同源出口：

1. `attribution_service._net_at`（归因页期初/期末净值）：曾不过滤归档项 →
   净值含已归档资产，违反 ADR-0017 D2；
2. `allocation_service.get_total_assets`（配置分析分母）：曾无 superseded 过滤、
   无归档过滤、且按「当日有行」求和 → 同日更正重复计数、含归档项、漏掉按 as-of
   沿用的持仓；现改为直接委托权威实现（含 as-of 沿用）；
3. `export_service.get_export_records`（导出）：曾显式排除 `status='draft'` →
   导出 CSV 缺草稿行，与首页合计对不上账（ADR-0020 D1 方向相反）。

口径前提（ADR-0020 D1）：`draft` 项**照常计入**合计；只有 `archived` 自
`archived_effective_from` 起退出（生效日前的历史值保留）。故本文件的「计入项」用
`draft` 构造——比 active 更严格，D1 与 D4 一起被这组用例守住。

数据一律合成（「示例-」、虚构金额/机构）。
"""
from datetime import date
from decimal import Decimal

from models import AssetClass, AssetItem, Institution, Member
from services import (
    allocation_service, analysis_service, attribution_service, classification_service,
    export_service,
)
from services.asset_service import correct_asset, create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)


# ---------------------------------------------------------------- 构造助手


def mk_item(db, name, status="draft"):
    """建资产项；默认 draft（四维全空＝「分类未补齐」的合法形状）。

    `status='active'` 受 CHECK 约束要求四维齐全，故「计入项」默认走 draft：
    它同样在计量中（ADR-0020 D1），但不依赖分类种子。
    """
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.commit()
    return item


def add_row(db, item, amount, on_date, side="asset"):
    """经 ledger 写一条快照行（真实写路径，不直插）。"""
    return create_asset(db, item.display_name, Decimal(str(amount)), on_date,
                        side=side, item_id=item.id)


def archive(db, item, effective_from):
    """归档（R-37）：自该日起终止计量，历史值保留（ADR-0017 D2 修订）。"""
    item.status = "archived"
    item.archived_effective_from = effective_from
    db.commit()


def classify(db, item):
    """补齐四维 → 状态机把 draft 转 active（`_sync_status` 唯一迁移点）。

    仅配置分母用例需要：`_amounts_by_item_column` 按项级列分组，项上必须真的有
    分类取值才进分子。
    """
    klass = AssetClass(code="TX01", name="示例类别", level=1, side_scope="any")
    inst = Institution(name="示例机构", kind="bank")
    owner = Member(name="示例本人", role="self")
    db.add_all([klass, inst, owner])
    db.commit()
    classification_service.set_classification(db, item.id, {
        "asset_class_id": klass.id,
        "liquidity_days": 0,
        "institution_id": inst.id,
        "owner_id": owner.id,
    })
    return db.get(AssetItem, item.id)


# ---------------------------------------------------------------- 修复 1：归因

def test_net_at_excludes_archived_items_and_matches_sum_as_of(db):
    """归因始末净值只算在计量项：归档项不得进入期初/期末值（ADR-0017 D2）。

    计入项 1000 @1/31 → 1200 @2/28；归档项 5000 在两天都有行且在 `_active_rows`
    的「每项最新行」里（该函数不看 status）——修复前会分别得到 6000 / 6200。
    """
    counted = mk_item(db, "示例-在计量项")
    add_row(db, counted, 1000, D_JAN)
    add_row(db, counted, 1200, D_FEB)

    gone = mk_item(db, "示例-已归档项")
    add_row(db, gone, 5000, D_JAN)
    add_row(db, gone, 5000, D_FEB)
    archive(db, gone, D_JAN)

    net_start, start_used = attribution_service._net_at(db, D_JAN, None)
    net_end, end_used = attribution_service._net_at(db, D_FEB, None)
    assert (net_start, start_used) == (Decimal("1000.00"), D_JAN)
    assert (net_end, end_used) == (Decimal("1200.00"), D_FEB)

    # 同源不变量：与 `_sum_as_of` 精确相等（Decimal 相等，不是近似）
    assert net_start == analysis_service._sum_as_of(db, D_JAN)[0]
    assert net_end == analysis_service._sum_as_of(db, D_FEB)[0]

    # 出口层：归因页读数同样不含归档项
    res = attribution_service.get_attribution(db)
    assert res["net_worth_start"] == "1000.00"
    assert res["net_worth_end"] == "1200.00"
    assert res["delta"] == "200.00"          # 纯内生（无本金口径变更史）


# ---------------------------------------------------------------- 修复 2：配置分母

def test_allocation_total_excludes_superseded_and_archived(db):
    """配置分析分母：同日更正不重复计、归档项不进分母、当日无行的项按 as-of 沿用。

    2/28 的四条行来源：更正后的 800（active）＋被取代的 500（superseded）＋
    归档项 5000（当日生效）＋沿用项 400（仅 1/31 有行）。修复前分母＝6300
    （同一日三行相加）；修复后＝1200（800 ＋ 沿用 400）。
    """
    item = classify(db, mk_item(db, "示例-配置项"))
    old = add_row(db, item, 500, D_FEB)
    new, _ = correct_asset(db, old.id, 800,          # 同日追加＋旧行 supersede
                           reason="correction")
    assert new.asset_date == D_FEB and old.superseded_by == new.id

    gone = mk_item(db, "示例-配置归档项")
    add_row(db, gone, 5000, D_FEB)
    archive(db, gone, D_FEB)

    carried = mk_item(db, "示例-配置沿用项")
    add_row(db, carried, 400, D_JAN)                 # 该日无行 → 值没变，沿用

    total = allocation_service.get_total_assets(db, D_FEB)
    assert total == Decimal("1200.00")
    # 同源不变量：与 `_sum_as_of` 的资产侧合计精确相等（含 as-of 沿用项）
    assert total == analysis_service._sum_as_of(db, D_FEB)[1]

    # 分子同口径：项级列分组不含 superseded / 归档行；未分类的沿用项无桶可入
    amounts = allocation_service._amounts_by_item_column(
        db, D_FEB, AssetItem.asset_class_id)
    assert amounts == {item.asset_class_id: Decimal("800.00")}


# ---------------------------------------------------------------- 修复 3：导出

def test_export_includes_draft_items(db):
    """导出含草稿项（ADR-0020 D1）：否则 CSV 缺行，与首页合计对不上账。"""
    draft = mk_item(db, "示例-待分类项")
    add_row(db, draft, 300, D_FEB)

    records = export_service.get_export_records(db)
    assert [r["asset_name"] for r in records] == ["示例-待分类项"]
    assert records[0]["amount"] == "300.00"

    # 同源：导出行确实在权威合计里（合计＝300，不是 0）
    assert analysis_service._sum_as_of(db, D_FEB)[0] == Decimal("300.00")