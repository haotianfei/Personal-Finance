"""「分类未补齐」（`asset_items.status='draft'`）项的净值口径披露。

产品决策：draft 项**照常计入净值**——钱是真实存在的，不该少算；界面上必须显式
标出「合计里有多少来自尚未补齐分类的项」。这组用例把三件事钉死：

1. 合计**包含** draft 项金额（`_sum_as_of` 从不排除它们，只排除归档项）；
2. `summary.unclassified_*` 如实披露这些项的**规模**（按 side 拆毛额 + 计数）；
3. 归档项**既不计入合计、也不进披露**；draft 且当日无有效记录（无行或行全被
   supersede）的项**不进披露**——净值里本来就没有它的份额。

数据一律合成（「示例-」、虚构金额/机构）。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import update

from models import Asset, AssetClass, AssetItem, Institution, Member
from services import analysis_service, classification_service
from services.asset_service import create_asset

D_JAN = date(2026, 1, 31)
D_FEB = date(2026, 2, 28)


# ---------------------------------------------------------------- fixtures


def mk_item(db, name, status="draft"):
    """建资产项；默认 draft（四维全空正是「分类未补齐」的形状）。"""
    item = AssetItem(display_name=name, status=status)
    db.add(item)
    db.flush()
    db.commit()
    return item


def classify(db, item_id):
    """补齐四个必填维度 → 状态机把 draft 转 active（`_sync_status` 唯一迁移点）。"""
    klass = db.query(AssetClass).filter_by(name="示例类型").first()
    if klass is None:
        klass = AssetClass(code="DX01", name="示例类型", level=1, side_scope="any")
        db.add(klass)
        db.commit()
    inst = db.query(Institution).filter_by(name="示例机构").first()
    if inst is None:
        inst = Institution(name="示例机构", kind="bank")
        db.add(inst)
        db.commit()
    owner = db.query(Member).filter_by(name="示例本人").first()
    if owner is None:
        owner = Member(name="示例本人", role="self")
        db.add(owner)
        db.commit()
    classification_service.set_classification(db, item_id, {
        "asset_class_id": klass.id,
        "liquidity_days": 0,
        "institution_id": inst.id,
        "owner_id": owner.id,
    })
    return db.get(AssetItem, item_id)


def add_row(db, item, amount, on_date, side="asset"):
    return create_asset(db, item.display_name, Decimal(str(amount)), on_date,
                        side=side, item_id=item.id)


@pytest.fixture()
def portfolio(db):
    """draft 资产 300 ＋ draft 负债 100 ＋ active 资产 1000 ＋ archived 资产 5000。

    archived 行落在 D_JAN 且 `archived_effective_from=D_JAN`：按 ADR-0017 D2 修订，
    它在两个快照日都已退出计量 → 既不该出现在合计里，也不该出现在披露里。
    """
    draft_asset = mk_item(db, "示例-待分类基金")
    add_row(db, draft_asset, 300, D_FEB)

    draft_liability = mk_item(db, "示例-待分类贷款")
    add_row(db, draft_liability, 100, D_FEB, side="liability")

    active = classify(db, mk_item(db, "示例-已分类股票").id)
    add_row(db, active, 1000, D_FEB)

    archived = mk_item(db, "示例-已归档旧账户")
    add_row(db, archived, 5000, D_JAN)
    archived.status = "archived"
    archived.archived_effective_from = D_JAN
    db.commit()

    return {
        "draft_asset": draft_asset,
        "draft_liability": draft_liability,
        "active": active,
        "archived": archived,
    }


# ---------------------------------------------------------------- 计入＋披露


def test_draft_items_count_in_net_worth_and_are_disclosed(db, portfolio):
    summary = analysis_service.get_summary(db)

    # ① 合计**包含** draft 项（300 资产 / 100 负债）与 active 项（1000）；
    #    archived 的 5000 不在其中（归档≠少算，而是已终止计量）。
    assert summary.latest_date == D_FEB
    assert summary.total_assets == Decimal("1300.00")
    assert summary.total_liabilities == Decimal("100.00")
    assert summary.net_worth == Decimal("1200.00")

    # ② 披露：规模按 side 拆毛额（不相抵），计数＝有 as-of 记录的 draft 项数
    assert summary.unclassified_count == 2
    assert summary.unclassified_asset_amount == Decimal("300.00")
    assert summary.unclassified_liability_amount == Decimal("100.00")

    # ③ 归档项既不在合计、也不在披露里（5000 两头都不出现）
    assert summary.unclassified_asset_amount != Decimal("5300.00")


def test_disclosure_fields_serialize_as_strings(db, portfolio):
    """HTTP 形态与 `get_summary` 既有金额字段一致（Decimal → str）。"""
    payload = analysis_service.get_summary(db).model_dump(mode="json")
    assert payload["unclassified_asset_amount"] == "300.00"
    assert payload["unclassified_liability_amount"] == "100.00"
    assert payload["unclassified_count"] == 2


def test_disclosure_is_zero_shaped_on_empty_db(db):
    """空库：披露维持零值形状（`NO_DATA`），不因缺数据而缺字段。"""
    summary = analysis_service.get_summary(db)
    assert summary.unclassified_count == 0
    assert summary.unclassified_asset_amount == Decimal("0")
    assert summary.unclassified_liability_amount == Decimal("0")


# ---------------------------------------------------------------- 不进披露


def test_draft_item_without_records_not_disclosed(db, portfolio):
    """draft 项当日无有效记录 → 不进 count、不贡献金额（净值里也没有它）。"""
    mk_item(db, "示例-刚建项还没录值")

    summary = analysis_service.get_summary(db)
    assert summary.unclassified_count == 2
    assert summary.unclassified_asset_amount == Decimal("300.00")
    assert summary.unclassified_liability_amount == Decimal("100.00")


def test_draft_item_with_only_superseded_record_not_disclosed(db, portfolio):
    """draft 项的行全被 supersede → 不再是「该日的有效记录」，不进披露。

    fixture 捷径：直接把该行指向 portfolio 里的一条有效行。本用例只验证
    `superseded_by IS NULL` 这个过滤条件，不关心替换行的归属语义。
    """
    ghost = mk_item(db, "示例-被更正掉的待分类项")
    row = add_row(db, ghost, 777, D_FEB)
    db.execute(update(Asset).where(Asset.id == row.id)
               .values(superseded_by=portfolio["active"].id))
    db.commit()

    summary = analysis_service.get_summary(db)
    assert summary.unclassified_count == 2
    assert summary.unclassified_asset_amount == Decimal("300.00")
    # 供更正的行本身也不进净值：合计仍是 1300，不含 777
    assert summary.total_assets == Decimal("1300.00")


def test_archived_effective_date_respected_by_disclosure(db, portfolio):
    """归档生效日 > as_of 的项仍在计量：`active_item_predicate` 同口径作用于披露。

    即「归档项」不是无条件排除——只有生效日及之后才退出（历史测量值保留）。
    """
    later = mk_item(db, "示例-下月才归档")   # archived，但生效日（3/31）晚于 as_of（2/28）
    add_row(db, later, 50, D_FEB)
    later.status = "archived"
    later.archived_effective_from = date(2026, 3, 31)
    db.commit()

    summary = analysis_service.get_summary(db)
    # 该行仍在计量（D_FEB < 生效日）→ 计入合计，但它是 archived 而非 draft → 不披露
    assert summary.total_assets == Decimal("1350.00")
    assert summary.unclassified_count == 2
    assert summary.unclassified_asset_amount == Decimal("300.00")