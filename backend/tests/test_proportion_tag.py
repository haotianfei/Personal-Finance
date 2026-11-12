"""按标签聚合的「未打标」派生行（`.scratch/asset-record-edit/spec.md` D9）。

覆盖四条验收：

1. 同一 `(项, 标签)` 不重复累加——一笔记 1000 挂两个标签，两个桶各收 1000 一次；
2. 多标签合集之和**可以**超过按项总额（多值语义，断言其为真而非截断）；
3. 归档项不出现于任何桶（走 `snapshot_identity.active_item_predicate`：
   自 `archived_effective_from` 起退出，生效日之前的历史账期照常计入）；
4. 「未打标」与各标签行按**并集**口径合并后 = 按项总额（自洽等式，容差 0.01）。

标签维度是**分桶维度**，不进 `test_total_consistency.py` 的出口登记表（D9）：那里
登记的是「合计出口」，而这里各桶之和本就允许超过总额。

数据全部合成（虚构标签/名称/金额）。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from models import AssetItem, AssetItemTag, Tag
from services import analysis_service, asset_service, classification_service
from services.proportion_service import (
    UNTAGGED_ROW_KEY,
    UNTAGGED_ROW_LABEL,
    get_proportion_by_tag,
)

#: 归档生效日（与记录日分开，用于「生效日之前仍计量」的对照）
D_ROW = date(2026, 1, 1)
D_ARCHIVE = date(2026, 3, 1)
D_AS_OF = date(2026, 3, 1)


# ---------------------------------------------------------------- fixture 助手


def mk_tag(db, name):
    tag = Tag(name=name, kind="free")
    db.add(tag)
    db.commit()
    return tag


def mk_row(db, name, amount, *, tags=(), on=D_ROW, side="asset"):
    """经 ledger 写一条快照行，并按需在资产项上打标（真实写路径，不直插）。"""
    row = asset_service.create_asset(db, name, Decimal(str(amount)), on, side=side)
    if tags:
        classification_service.set_tags(db, row.asset_item_id, [t.id for t in tags])
    return row


def archive(db, item_id, effective_from):
    """归档（R-37 / 0025）：自该日起终止计量，历史值保留（ADR-0017 D2 修订）。"""
    item = db.get(AssetItem, item_id)
    item.status = "archived"
    item.archived_effective_from = effective_from
    db.commit()


def amounts(result, side_key="asset_items"):
    """{展示名: 金额}——标签名与「未打标」同处一张表，便于逐桶对账。"""
    return {i["name"]: Decimal(i["amount"]) for i in result[side_key]}


def by_name(result, side_key="asset_items"):
    return {i["name"]: i for i in result[side_key]}


# ---------------------------------------------------------------- 断言一：组内不重复累加


def test_tag_bucket_counts_each_item_once(db):
    """同一 (项, 标签) 只累加一次：1000 挂两个标签 → 每个标签各收 1000 一次。"""
    t1 = mk_tag(db, "示例标签甲")
    t2 = mk_tag(db, "示例标签乙")
    mk_row(db, "示例基金", 1000, tags=(t1, t2))
    mk_row(db, "示例存款", 500, tags=(t1,))

    result = get_proportion_by_tag(db, D_AS_OF)

    # 甲桶 1500 = 1000 + 500（不是 1000×2 + 500）：关联表 join 没有把同一行放大
    assert amounts(result) == {
        "示例标签甲": Decimal("1500.00"),
        "示例标签乙": Decimal("1000.00"),
    }
    # 项数同样是「按项」而不是「按关联行」——两处都证明组内唯一
    counts = {i["name"]: i["count"] for i in result["asset_items"]}
    assert counts == {"示例标签甲": 2, "示例标签乙": 1}
    # 全部项都有标签 → 不出「未打标」空行
    assert UNTAGGED_ROW_LABEL not in amounts(result)


# ---------------------------------------------------------------- 断言二：多标签之和可超总额


def test_multi_tag_sum_may_exceed_by_item_total(db):
    """多值语义：各标签之和 2500 > 按项总额 1500，且不被截断/归一到 100%。"""
    t1 = mk_tag(db, "示例标签甲")
    t2 = mk_tag(db, "示例标签乙")
    mk_row(db, "示例基金", 1000, tags=(t1, t2))
    mk_row(db, "示例存款", 500, tags=(t1,))

    result = get_proportion_by_tag(db, D_AS_OF)
    rows = amounts(result)

    tag_sum = rows["示例标签甲"] + rows["示例标签乙"]
    by_item_total = Decimal("1500.00")          # 1000 + 500，按项计一次
    assert tag_sum == Decimal("2500.00")
    assert tag_sum > by_item_total              # 为真，不作截断

    # 每行金额仍是该项的全额（不是按桶之和摊分/截断）
    assert rows == {"示例标签甲": Decimal("1500.00"), "示例标签乙": Decimal("1000.00")}
    # 响应里的 total_* 仍是各桶之和（本维度不归一到 100%，前端须标注「可重叠」）
    assert Decimal(result["total_assets"]) == tag_sum


# ---------------------------------------------------------------- 断言三：归档项排除


def test_archived_items_excluded_from_every_bucket(db):
    """归档项自生效日起不出现于任何桶（含「未打标」行），生效日之前照常计入。"""
    t1 = mk_tag(db, "示例标签甲")
    keep = mk_row(db, "示例在册", 1000, tags=(t1,))
    gone = mk_row(db, "示例归档", 700, tags=(t1,))
    gone_untagged = mk_row(db, "示例归档未打标", 400)
    assert keep.asset_item_id != gone.asset_item_id != gone_untagged.asset_item_id

    archive(db, gone.asset_item_id, D_ARCHIVE)
    archive(db, gone_untagged.asset_item_id, D_ARCHIVE)

    # 生效日当天：标签桶只剩在册项，「未打标」行连空壳都不出现
    at_archive = get_proportion_by_tag(db, D_ARCHIVE)
    assert amounts(at_archive) == {"示例标签甲": Decimal("1000.00")}
    assert UNTAGGED_ROW_LABEL not in amounts(at_archive)

    # 生效日之前：归档项的历史测量值照常计入（active_item_predicate 的 as-of 语义，
    # 不是「整支剔除」——那会追溯抹除已有序列）
    before = get_proportion_by_tag(db, date(2026, 2, 1))
    assert amounts(before) == {
        "示例标签甲": Decimal("1700.00"),
        UNTAGGED_ROW_LABEL: Decimal("400.00"),
    }


def test_archived_untagged_item_does_not_keep_untagged_row_alive(db):
    """唯一的未打标项被归档 → 「未打标」行随之消失（补集口径，不是恒存在的空桶）。"""
    t1 = mk_tag(db, "示例标签甲")
    mk_row(db, "示例基金", 1000, tags=(t1,))
    only_untagged = mk_row(db, "示例现金", 300)
    assert UNTAGGED_ROW_LABEL in amounts(get_proportion_by_tag(db, D_ARCHIVE))

    archive(db, only_untagged.asset_item_id, D_ARCHIVE)
    assert UNTAGGED_ROW_LABEL not in amounts(get_proportion_by_tag(db, D_ARCHIVE))


# ---------------------------------------------------------------- 断言四：并集自洽


def test_untagged_plus_tagged_union_equals_authoritative_total(db):
    """「未打标」+ 各标签并集 = 按项总额（资产/负债两侧同构）。

    参考值**独立复算**：as-of 计入项由 `analysis_service._get_latest_records_for_items`
    给出（与占比共用 `active_item_predicate` 同一份口径），标签成员关系直接查
    `asset_item_tags`。两侧都对上 `analysis_service._sum_as_of` 的权威总额。
    """
    t1 = mk_tag(db, "示例标签甲")
    t2 = mk_tag(db, "示例标签乙")
    mk_row(db, "示例基金", 1000, tags=(t1, t2))                 # 重叠：两个标签
    mk_row(db, "示例存款", 500, tags=(t1,))
    mk_row(db, "示例现金", 300)                                 # 未打标
    mk_row(db, "示例借款", 200, side="liability", tags=(t1,))
    mk_row(db, "示例信用卡", 100, side="liability")              # 未打标

    result = get_proportion_by_tag(db, D_AS_OF)

    item_ids = db.execute(select(AssetItem.id)).scalars().all()
    records = analysis_service._get_latest_records_for_items(db, D_AS_OF, item_ids)
    tagged_ids = set(
        db.execute(select(AssetItemTag.item_id).distinct()).scalars().all())
    reference = {"asset": {"tagged": Decimal("0"), "untagged": Decimal("0")},
                 "liability": {"tagged": Decimal("0"), "untagged": Decimal("0")}}
    for item_id, _name, amount, side, _d in records:
        key = "tagged" if item_id in tagged_ids else "untagged"
        reference[side][key] += Decimal(str(amount))

    _, total_assets, total_liabilities = analysis_service._sum_as_of(db, D_AS_OF)
    authoritative = {"asset": total_assets, "liability": total_liabilities}
    tolerance = Decimal("0.01")

    for side, side_key in (("asset", "asset_items"), ("liability", "liability_items")):
        rows = amounts(result, side_key)
        # ① 未打标行 = 补集（没有任何标签的在计量项）
        assert rows[UNTAGGED_ROW_LABEL] == reference[side]["untagged"]
        # ② 未打标 + 标签并集 = 按项总额（并集口径：重叠标签只算一次）
        merged = rows[UNTAGGED_ROW_LABEL] + reference[side]["tagged"]
        assert abs(merged - authoritative[side]) <= tolerance
        # ③ 不重：并集金额恰好等于「按项计一次」之和，没有把标签倍率带进来
        assert merged == reference[side]["tagged"] + reference[side]["untagged"]

    # 重叠证据：资产侧各标签朴素相加 > 并集，故上面的 ②③ 不是同义反复
    naive_asset_sum = sum(
        amount for name, amount in amounts(result).items() if name != UNTAGGED_ROW_LABEL)
    assert naive_asset_sum > reference["asset"]["tagged"]
    assert reference["asset"]["tagged"] == Decimal("1500.00")


# ---------------------------------------------------------------- 行标识与边界


def test_untagged_row_carries_machine_key_and_label(db):
    """派生行的行标识是 `untagged`、展示文案是「未打标」（前端按键识别，不匹配中文）。"""
    t1 = mk_tag(db, "示例标签甲")
    mk_row(db, "示例基金", 1000, tags=(t1,))
    mk_row(db, "示例现金", 300)

    row = by_name(get_proportion_by_tag(db, D_AS_OF))[UNTAGGED_ROW_LABEL]
    assert UNTAGGED_ROW_KEY == "untagged"
    assert UNTAGGED_ROW_LABEL == "未打标"
    assert row["key"] == UNTAGGED_ROW_KEY
    assert Decimal(row["amount"]) == Decimal("300.00")
    assert row["count"] == 1
    assert row["type"] == "asset"
    # 标签行不带 key（不改既有行形状，只在 data 内多一行）
    assert "key" not in by_name(get_proportion_by_tag(db, D_AS_OF))["示例标签甲"]


def test_untagged_row_only_on_liability_side(db):
    """两侧同构：只有负债侧有未打标项时，资产侧不出该行。"""
    t1 = mk_tag(db, "示例标签甲")
    mk_row(db, "示例基金", 1000, tags=(t1,))
    mk_row(db, "示例借款", 200, side="liability")

    result = get_proportion_by_tag(db, D_AS_OF)
    assert UNTAGGED_ROW_LABEL not in amounts(result, "asset_items")
    assert amounts(result, "liability_items") == {UNTAGGED_ROW_LABEL: Decimal("200.00")}