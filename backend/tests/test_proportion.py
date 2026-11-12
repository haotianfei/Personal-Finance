"""占比口径测试（ADR-0019：分类读资产项级）。

旧 fixture 用 `create_asset(attribute_ids=…)` 打**记录级** EAV 分类，已随 0026 迁移
废弃（记录侧写入路径直接 422）。现在的口径是：分类挂在 `asset_items` 上，占比按
`assets.asset_item_id → asset_items` 聚合，所以 fixture 一律「建记录 → 在项上设分类」。

数据全部合成（虚构类别/机构/成员/金额）。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from errors import ApiError, ErrorCode
from models import Asset, AssetClass, AssetItem, Institution, Member, Tag
from services import asset_service, classification_service
from services.proportion_service import (
    get_proportion_by_asset_class,
    get_proportion_by_asset_name,
    get_proportion_by_channel,
    get_proportion_by_institution,
    get_proportion_by_liquidity,
    get_proportion_by_owner,
    get_proportion_by_risk_level,
    get_proportion_by_tag,
    get_proportion_data,
    require_dimension,
)


# ---------------------------------------------------------------- fixture 助手


def mk_class(db, name="示例货币基金", parent=None, side_scope="any"):
    """叶子类别（无子节点才能被资产项引用，ADR-0019 D8 触发器语义）。"""
    node = db.execute(select(AssetClass).where(AssetClass.name == name)).scalar_one_or_none()
    if node is not None:
        return node
    n = db.execute(select(func.count()).select_from(AssetClass)).scalar() + 1
    node = AssetClass(code=f"T{n}", name=name, parent_id=parent.id if parent else None,
                      level=(parent.level + 1) if parent else 1,
                      side_scope=side_scope)
    db.add(node)
    db.commit()
    return node


def mk_institution(db, name="示例银行", kind="bank"):
    row = db.execute(
        select(Institution).where(Institution.name == name)).scalar_one_or_none()
    if row is not None:
        return row
    row = Institution(name=name, kind=kind)
    db.add(row)
    db.commit()
    return row


def mk_member(db, name="本人", role="self"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is not None:
        return row
    row = Member(name=name, role=role)
    db.add(row)
    db.commit()
    return row


def mk_tag(db, name="示例标签"):
    row = db.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
    if row is not None:
        return row
    row = Tag(name=name, kind="free")
    db.add(row)
    db.commit()
    return row


def mk_asset(db, name, amount, on_date, *, klass=None, side="asset",
             liquidity_days=0, institution=None, owner=None, channel_id=None,
             risk_level=None, tags=()):
    """建一条记录并在其资产项上设分类（记录只承载金额/日期/侧别）。"""
    row = asset_service.create_asset(db, name, Decimal(str(amount)), on_date, side=side)
    item_id = row.asset_item_id
    classification_service.set_classification(db, item_id, {
        "asset_class_id": (klass or mk_class(db)).id,
        "liquidity_days": liquidity_days,
        "institution_id": (institution or mk_institution(db)).id,
        "owner_id": (owner or mk_member(db)).id,
        "channel_id": channel_id,
        "risk_level": risk_level,
    })
    if tags:
        classification_service.set_tags(db, item_id, [t.id for t in tags])
    return row


def _mk_renamed_item(db, klass, old_name, new_name, old_amt, new_amt, d_old, d_new):
    """同一 asset_item：旧名一条行 → 改名 → 新名一条行（name 快照不回写）。"""
    row = mk_asset(db, old_name, old_amt, d_old, klass=klass)
    item_id = row.asset_item_id
    asset_service.rename_item(db, item_id, new_name)
    row2 = mk_asset(db, new_name, new_amt, d_new)
    assert row2.asset_item_id == item_id
    assert {row.asset_name, row2.asset_name} == {old_name, new_name}
    return item_id


# ---------------------------------------------------------------- 类别维度


def test_proportion_by_asset_class_asset_liability_net_worth(db):
    """按类别占比正确区分资产/负债并计算净资产（分类读资产项级）。"""
    stock = mk_class(db, "示例股票")
    credit = mk_class(db, "示例信用卡")

    mk_asset(db, "持仓A", 1000, date(2026, 1, 1), klass=stock)
    mk_asset(db, "负债B", 200, date(2026, 1, 1), klass=credit, side="liability")

    result = get_proportion_by_asset_class(db, date(2026, 1, 1))

    assert float(result["total_assets"]) == 1000.0
    assert float(result["total_liabilities"]) == 200.0
    assert float(result["net_worth"]) == 800.0
    assert [i["name"] for i in result["asset_items"]] == ["示例股票"]
    assert result["asset_items"][0]["percent"] == 100.0
    assert [i["name"] for i in result["liability_items"]] == ["示例信用卡"]
    assert abs(float(result["liability_items"][0]["amount"])) == 200.0


def test_proportion_latest_snapshot_per_holding(db):
    """只取每持仓在快照日期前的最新一条估值（不累计历史）。"""
    klass = mk_class(db, "示例股票")
    for d, amt in [(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1500)]:
        mk_asset(db, "持仓A", amt, d, klass=klass)

    result = get_proportion_by_asset_class(db, date(2026, 2, 1))
    # 最新估值应为 1500 而非 2500（1000+1500）
    assert float(result["total_assets"]) == 1500.0


def test_proportion_asset_class_level_rollup(db):
    """`level` 上卷：叶子聚合到目标层祖先（asset_classes.parent_id 链）。"""
    root = mk_class(db, "示例一级")
    leaf = mk_class(db, "示例二级-货币", parent=root)
    other = mk_class(db, "示例其他")

    mk_asset(db, "持仓A", 1000, date(2026, 1, 1), klass=leaf)
    mk_asset(db, "持仓B", 400, date(2026, 1, 1), klass=other)

    # 不指定层级：按叶子原样分组
    flat = get_proportion_by_asset_class(db, date(2026, 1, 1))
    assert {i["name"]: float(i["amount"]) for i in flat["asset_items"]} == {
        "示例二级-货币": 1000.0, "示例其他": 400.0}
    assert {i["level"] for i in flat["asset_items"]} == {1, 2}

    # level=1：叶子向上卷到根类别
    rolled = get_proportion_by_asset_class(db, date(2026, 1, 1), level=1)
    assert [(i["name"], float(i["amount"]), i["count"]) for i in rolled["asset_items"]] == [
        ("示例一级", 1000.0, 1), ("示例其他", 400.0, 1)]
    assert {i["level"] for i in rolled["asset_items"]} == {1}

    # 层级从 1 起（旧 EAV 的 0 基层级已随 attributes 表废弃）
    with pytest.raises(ApiError) as ei:
        get_proportion_by_asset_class(db, date(2026, 1, 1), level=0)
    assert ei.value.status_code == 422


def test_proportion_rename_same_item_counted_once(db):
    """改名回归：同一 item 的两个 name 快照只计一次，且取最新一条。

    改名只改 display_name、不回写历史行 asset_name；子查询若按 asset_name 分组，
    同一项会被拆成「旧名」「新名」两个资产 → 金额与 count 双计。分类在项上，
    改名不影响分类归属。
    """
    klass = mk_class(db, "示例股票")
    _mk_renamed_item(db, klass, "示例-旧名", "示例-新名", 1000, 1200,
                     date(2026, 1, 31), date(2026, 2, 28))

    result = get_proportion_by_asset_class(db, date(2026, 2, 28))
    # 取最新 1200 单计，而不是 1000+1200
    assert float(result["total_assets"]) == 1200.0
    assert result["asset_items"][0]["count"] == 1
    assert result["asset_items"][0]["percent"] == 100.0

    # 改名前的时点仍取旧名行，同样单计
    jan = get_proportion_by_asset_class(db, date(2026, 1, 31))
    assert float(jan["total_assets"]) == 1000.0
    assert jan["asset_items"][0]["count"] == 1


def test_proportion_by_asset_name_rename_not_double_counted(db):
    """按名称维度：改名后旧名行不再作为独立资产出现（最新 name 快照胜出）。"""
    klass = mk_class(db, "示例股票")
    _mk_renamed_item(db, klass, "示例-旧名", "示例-新名", 1000, 1200,
                     date(2026, 1, 31), date(2026, 2, 28))

    result = get_proportion_by_asset_name(db, date(2026, 2, 28))
    assert float(result["total_assets"]) == 1200.0
    assert [i["name"] for i in result["asset_items"]] == ["示例-新名"]


# ---------------------------------------------------------------- 六个项级维度


def test_proportion_by_liquidity_uses_days_and_label(db):
    """流动性维度按整数档位分组，展示名由 liquidity_label 派生。"""
    cash = mk_class(db, "示例现金")
    fund = mk_class(db, "示例基金")
    mk_asset(db, "活期", 1000, date(2026, 1, 1), klass=cash, liquidity_days=0)
    mk_asset(db, "货基", 500, date(2026, 1, 1), klass=fund, liquidity_days=1)

    result = get_proportion_by_liquidity(db, date(2026, 1, 1))
    by_days = {i["days"]: i for i in result["asset_items"]}
    assert set(by_days) == {0, 1}
    assert by_days[0]["name"] == classification_service.liquidity_label(0)
    assert by_days[1]["name"] == classification_service.liquidity_label(1)
    assert float(result["total_assets"]) == 1500.0


def test_proportion_by_institution_channel_owner_risk(db):
    """机构/渠道/归属/风险四维各读资产项上的列（与旧 account 字符串匹配无关）。"""
    bank = mk_institution(db, "示例银行")
    broker = mk_institution(db, "示例券商", kind="broker")
    me = mk_member(db, "本人")
    spouse = mk_member(db, "配偶", role="spouse")

    mk_asset(db, "存款", 1000, date(2026, 1, 1), institution=bank, owner=me,
             risk_level="R1")
    mk_asset(db, "股票", 400, date(2026, 1, 1), institution=broker, owner=spouse,
             risk_level="R4")

    inst = get_proportion_by_institution(db, date(2026, 1, 1))
    assert {i["name"]: float(i["amount"]) for i in inst["asset_items"]} == {
        "示例银行": 1000.0, "示例券商": 400.0}

    owner = get_proportion_by_owner(db, date(2026, 1, 1))
    assert {i["name"]: float(i["amount"]) for i in owner["asset_items"]} == {
        "本人": 1000.0, "配偶": 400.0}

    risk = get_proportion_by_risk_level(db, date(2026, 1, 1))
    assert {i["name"]: float(i["amount"]) for i in risk["asset_items"]} == {
        "R1": 1000.0, "R4": 400.0}

    # 渠道全为空 → 落到「未指定渠道」而不是整行消失（钱不能因为缺展示名不见）
    channel = get_proportion_by_channel(db, date(2026, 1, 1))
    assert {i["name"]: float(i["amount"]) for i in channel["asset_items"]} == {
        "未指定渠道": 1400.0}


def test_proportion_by_tag_is_multi_valued(db):
    """标签维度是项级多值：同一笔金额计入其每个标签（组内不重复累加）。

    另有一条「未打标」派生行（D9）：无标签的项不再整支消失，单列一行承接。
    """
    t1 = mk_tag(db, "示例标签甲")
    t2 = mk_tag(db, "示例标签乙")
    mk_asset(db, "基金", 1000, date(2026, 1, 1), tags=(t1, t2))
    mk_asset(db, "存款", 500, date(2026, 1, 1), tags=(t1,))
    mk_asset(db, "现金", 300, date(2026, 1, 1))          # 不打标 → 落「未打标」

    result = get_proportion_by_tag(db, date(2026, 1, 1))
    by_name = {i["name"]: i for i in result["asset_items"]}
    assert float(by_name["示例标签甲"]["amount"]) == 1500.0
    assert float(by_name["示例标签乙"]["amount"]) == 1000.0
    assert by_name["示例标签甲"]["count"] == 2
    # 新增派生行（原断言不变，只补期望行）
    assert float(by_name["未打标"]["amount"]) == 300.0
    assert by_name["未打标"]["count"] == 1
    assert by_name["未打标"]["key"] == "untagged"


# ---------------------------------------------------------------- 维度名与废弃值


def test_new_dimension_names_accepted_and_legacy_rejected(db):
    """新维度名全部可用；旧值（fund_type/liquidity_rating/account）一律 422 并指新名。"""
    from services.proportion_service import SUPPORTED_DIMENSIONS

    assert set(SUPPORTED_DIMENSIONS) == {
        "asset_class", "liquidity", "institution", "channel",
        "asset_name", "owner", "risk_level", "tag"}
    for name in SUPPORTED_DIMENSIONS:
        assert require_dimension(name) == name
    # 预警/配置目标维度的写法 risk 与本服务的 risk_level 同指一列（两个都是新名）
    assert require_dimension("risk") == "risk_level"

    for legacy, replacement in (("fund_type", "asset_class"),
                                ("liquidity_rating", "liquidity"),
                                ("account", "institution")):
        with pytest.raises(ApiError) as ei:
            get_proportion_data(db, legacy)
        assert ei.value.status_code == 422
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.details["replacement"].startswith(replacement)
        assert "已废弃" in ei.value.message

    with pytest.raises(ApiError) as ei:
        get_proportion_data(db, "不存在的维度")
    assert ei.value.status_code == 422
    assert "不支持的占比维度" in ei.value.message


def test_get_proportion_data_shape_for_new_dimension(db):
    """`get_proportion_data` 只换口径、不改返回结构（金额/百分比字段照旧）。"""
    mk_asset(db, "持仓A", 1000, date(2026, 1, 1))

    out = get_proportion_data(db, "asset_class", date(2026, 1, 1))
    assert out["dimension"] == "asset_class"
    assert out["level"] is None
    assert Decimal(out["total_assets"]) == Decimal("1000")
    assert Decimal(out["net_worth"]) == Decimal("1000")
    assert out["total_count"] == 1
    assert out["items"] == out["asset_items"] + out["liability_items"]
    assert out["asset_items"][0]["percent"] == 100.0
    assert out["meta"] is not None