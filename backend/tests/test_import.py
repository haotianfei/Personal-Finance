"""CSV 导入（ADR-0019 新列）。

列集合＝`asset_date, asset_name, amount, side, asset_class, liquidity_days,
institution, channel, owner, risk_level, tags`；分类写在**资产项**上（记录行只读），
且导入只补空维度、绝不覆盖既有分类，同值同日不建行。
数据全部合成（虚构机构/示例标的），不含真实财务信息。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models import Asset, AssetClass, AssetItem, Channel, Institution, Member, Tag
from services import classification_service, import_service

from conftest import legacy_eav_tables


CSV = (
    "asset_date,asset_name,amount,side,asset_class,liquidity_days,institution,"
    "channel,owner,risk_level,tags\n"
    "2026-01-01,某股票,1000.50,asset,A 现金与等价物/A1 活期存款,1,示例银行,示例App,成员甲,R3,核心|长期\n"
    "2026-01-01,某活期,500,,A 现金与等价物/A1 活期存款,0,示例银行,,成员甲,,\n"
    "2026-01-01,某信用卡,-800,,A 现金与等价物/A1 活期存款,0,示例银行,,成员甲,,\n"
)


def _seed_dict(db):
    root = AssetClass(code="A", name="现金与等价物", parent_id=None, level=1,
                      side_scope="any")
    db.add(root)
    db.flush()
    leaf = AssetClass(code="A1", name="活期存款", parent_id=root.id, level=2,
                      side_scope="any")
    db.add(leaf)
    db.flush()
    inst = Institution(name="示例银行", kind="bank")
    chan = Channel(name="示例App")
    mem = Member(name="成员甲", role="self")
    tag1 = Tag(name="核心", kind="controlled")
    tag2 = Tag(name="长期", kind="controlled")
    db.add_all([inst, chan, mem, tag1, tag2])
    db.commit()
    return leaf, inst, chan, mem, tag1, tag2


def test_import_writes_rows_and_item_level_classification(db):
    """落 3 行快照；分类写到资产项（四维＋标签），记录行不挂任何属性。"""
    leaf, inst, chan, mem, tag1, tag2 = _seed_dict(db)

    batch = import_service.import_csv_records(db, CSV, "t.csv", "skip", {})
    assert batch.record_count == 3
    assert batch.status == "success"
    assert batch.skipped == [] and batch.skipped_unchanged == [] and batch.warnings == []

    assets = db.query(Asset).all()
    assert len(assets) == 3
    amounts = {a.asset_name: a.amount for a in assets}
    assert float(amounts["某股票"]) == 1000.5
    assert float(amounts["某活期"]) == 500.0
    assert float(amounts["某信用卡"]) == 800.0        # 负金额归一为正数
    assert db.query(Asset).filter_by(asset_name="某信用卡").one().side == "liability"
    assert db.query(Asset).filter_by(asset_name="某股票").one().side == "asset"

    # 每行都挂上资产项；分类只在项上
    for a in assets:
        assert a.asset_item_id is not None
        assert legacy_eav_tables(db) == set()        # 记录级载体已随 0027 删除
        item = db.get(AssetItem, a.asset_item_id)
        assert item.status == "active"               # 四维补齐 → draft 转 active
        assert item.asset_class_id == leaf.id
        assert item.institution_id == inst.id
        assert item.owner_id == mem.id
        assert item.liquidity_days == (1 if a.asset_name == "某股票" else 0)
        assert item.channel_id == (chan.id if a.asset_name == "某股票" else None)
        assert item.risk_level == ("R3" if a.asset_name == "某股票" else None)
        assert sorted(t.id for t in item.tags) == (
            sorted([tag1.id, tag2.id]) if a.asset_name == "某股票" else [])


def test_import_does_not_overwrite_existing_classification(db):
    """已有分类与 CSV 不一致 → 只记 warnings，绝不覆盖（导入是批量入口）。"""
    leaf, inst, chan, mem, _t1, _t2 = _seed_dict(db)
    other = AssetClass(code="A2", name="定期存款", parent_id=db.query(AssetClass)
                       .filter_by(code="A").one().id, level=2, side_scope="any")
    db.add(other)
    db.commit()

    # 先建一个已完整分类的项（类别=定期存款）
    item = AssetItem(display_name="某股票", status="active",
                     asset_class_id=other.id, liquidity_days=7,
                     institution_id=inst.id, owner_id=mem.id)
    db.add(item)
    db.commit()

    batch = import_service.import_csv_records(
        db, "asset_date,asset_name,amount,asset_class,liquidity_days\n"
            "2026-02-01,某股票,100,A 现金与等价物/A1 活期存款,30\n",
        "t.csv", "skip", {})

    assert batch.record_count == 1
    db.expire_all()
    item = db.query(AssetItem).filter_by(display_name="某股票").one()
    assert item.asset_class_id == other.id          # 未被覆盖
    assert item.liquidity_days == 7                 # 未被覆盖
    reasons = {(w["field"], w["reason"]) for w in batch.warnings}
    assert ("asset_class", "classification_conflict") in reasons
    assert ("liquidity_days", "classification_conflict") in reasons


def test_import_only_fills_empty_dims(db):
    """只补空维度：机构已填且与 CSV 一致 → 不写、不告警；其余空维照补。"""
    leaf, inst, chan, mem, _t1, _t2 = _seed_dict(db)
    db.add(AssetItem(display_name="某基金", status="draft",
                     institution_id=inst.id))
    db.commit()

    batch = import_service.import_csv_records(
        db, "asset_date,asset_name,amount,asset_class,liquidity_days,institution,"
            "channel,owner\n"
            "2026-03-01,某基金,100,A 现金与等价物/A1 活期存款,30,示例银行,示例App,成员甲\n",
        "t.csv", "skip", {})
    assert batch.warnings == []
    db.expire_all()
    item = db.query(AssetItem).filter_by(display_name="某基金").one()
    assert item.status == "active"
    assert (item.asset_class_id, item.liquidity_days, item.channel_id,
            item.owner_id) == (leaf.id, 30, chan.id, mem.id)


def test_import_skips_unchanged_same_date(db):
    """账本即变更日志：同项同日已有行且 (amount, side) 相同 → 不建行、进 skipped_unchanged。"""
    _seed_dict(db)
    one = ("asset_date,asset_name,amount,side\n"
           "2026-04-01,某活期,500,asset\n")
    first = import_service.import_csv_records(db, one, "a.csv", "skip", {})
    assert first.record_count == 1 and first.skipped_unchanged == []

    replay = import_service.import_csv_records(db, one, "a.csv", "skip", {})
    assert replay.record_count == 0
    assert replay.skipped == []
    assert len(replay.skipped_unchanged) == 1
    row = replay.skipped_unchanged[0]
    assert row["asset_date"] == "2026-04-01"
    assert row["amount"] == "500.00" and row["side"] == "asset"
    assert row["asset_item_id"] is not None and row["existing_id"] is not None
    assert db.query(Asset).count() == 1             # 未新增行


def test_import_same_date_different_amount_is_conflict(db):
    """同日已有行但值不同 → write-once：进 skipped(duplicate_active)，不覆盖。"""
    _seed_dict(db)
    import_service.import_csv_records(
        db, "asset_date,asset_name,amount\n2026-05-01,某活期,500\n", "a.csv", "skip", {})
    batch = import_service.import_csv_records(
        db, "asset_date,asset_name,amount\n2026-05-01,某活期,600\n", "b.csv", "skip", {})

    assert batch.record_count == 0
    assert batch.skipped_unchanged == []
    assert batch.skipped[0]["reason"] == "duplicate_active"
    assert db.query(Asset).one().amount == Decimal("500.00")


def test_import_unknown_dictionary_value_warns_and_leaves_dim_empty(db):
    """字典项查不到 → 不自动建（D7），该维留空并记 warning（draft 允许缺维）。"""
    _seed_dict(db)
    batch = import_service.import_csv_records(
        db, "asset_date,asset_name,amount,institution,channel,owner,tags\n"
            "2026-06-01,某活期,1,不存在的机构,不存在的渠道,不存在的成员,免费标签\n",
        "t.csv", "skip", {})
    reasons = {w["reason"] for w in batch.warnings}
    assert {"institution_unknown", "channel_unknown", "owner_unknown",
            "tag_unknown"} <= reasons
    item = db.query(AssetItem).filter_by(display_name="某活期").one()
    assert item.status == "draft"                   # 缺四维 → 草稿，不伪造
    assert db.query(Institution).count() == 1       # 未新建字典项
    assert db.query(Tag).count() == 2


def test_import_rejects_legacy_columns_with_new_names(db):
    """旧 EAV 列出现即报错，并指路新列（不做静默映射）。"""
    for legacy in ("liquidity_rating", "fund_type", "account"):
        csv_text = f"asset_date,asset_name,amount,{legacy}\n2026-01-01,某活期,1,x\n"
        with pytest.raises(ValueError) as ei:
            import_service.parse_csv_content(csv_text, "old.csv")
        msg = str(ei.value)
        assert legacy in msg
        assert "asset_class" in msg or "liquidity_days" in msg or "institution" in msg


def test_parse_rejects_bad_liquidity_and_risk(db):
    rows = import_service.parse_csv_content(
        "asset_date,asset_name,amount,liquidity_days,risk_level\n"
        "2026-01-01,某活期,1,42,R3\n"
        "2026-01-01,某活期,1,30,R9\n", "t.csv")
    assert "白名单" in rows[0]["error"]
    assert "risk_level" in rows[1]["error"]


def test_normalize_amount_strips_currency_and_thousands(db):
    """验证金额解析：去货币符号与千分位。"""
    assert import_service.normalize_amount("￥1,234.56") == Decimal("1234.56")
    assert import_service.normalize_amount("-") == Decimal("0.00")


def test_normalize_liquidity_days_whitelist(db):
    """流动性＝整数天数档位（ADR-0019 D4：不再有档位名字典）。"""
    assert import_service.normalize_liquidity_days("30") == 30
    assert import_service.normalize_liquidity_days("") is None
    assert import_service.normalize_liquidity_days("9999") == 9999
    with pytest.raises(ValueError):
        import_service.normalize_liquidity_days("T+7 赎回到账")


def test_split_tags_uses_pipe_separator(db):
    assert import_service.split_tags("核心 | 长期|核心") == ["核心", "长期"]
    assert import_service.split_tags("") == []


def test_export_then_import_round_trip(db):
    """导出列＝导入列：一次导出再回灌，值、分类、标签逐字段还原（同值同日 → 幂等 skip）。"""
    from services import export_service

    leaf, inst, chan, mem, tag1, _tag2 = _seed_dict(db)
    import_service.import_csv_records(db, CSV, "t.csv", "skip", {})

    csv_out = export_service.generate_csv(export_service.get_export_records(db))
    header = csv_out.splitlines()[0].split(",")
    assert header == list(import_service.CSV_COLUMNS)
    stock = [ln for ln in csv_out.splitlines() if "某股票" in ln][0].split(",")
    assert stock[header.index("asset_class")] == "A 现金与等价物/A1 活期存款"
    assert stock[header.index("liquidity_days")] == "1"
    assert stock[header.index("institution")] == "示例银行"
    assert stock[header.index("channel")] == "示例App"
    assert stock[header.index("owner")] == "成员甲"
    assert stock[header.index("risk_level")] == "R3"
    assert stock[header.index("tags")] == "核心|长期"
    assert stock[header.index("amount")] == "1000.50"

    # 回灌：四维/标签都已在项上 → 不告警、不改分类；同日同值 → 全部 skipped_unchanged
    batch = import_service.import_csv_records(db, csv_out, "roundtrip.csv", "skip", {})
    assert batch.record_count == 0
    assert batch.warnings == []
    assert len(batch.skipped_unchanged) == 3
    assert db.query(Asset).count() == 3


def test_export_includes_draft_items(db):
    """ADR-0020 D1：草稿项计入合计，导出必须同源——否则 CSV 与首页对不上账。

    旧用例断言「导出不含 draft」（ADR-0019 D9），编码的是已被 ADR-0020 D1 取代的口径。
    """
    from services import export_service

    _seed_dict(db)
    import_service.import_csv_records(
        db, "asset_date,asset_name,amount\n2026-07-01,某完全未分类,100\n", "t.csv", "skip", {})
    assert db.query(AssetItem).filter_by(display_name="某完全未分类").one().status == "draft"
    draft_records = export_service.get_export_records(db)
    assert [r["asset_name"] for r in draft_records] == ["某完全未分类"]
    # 未分类（草稿/存量行）分类列留空串，但行本身必须在
    assert draft_records[0]["asset_class"] == ""
    assert draft_records[0]["amount"] == "100.00"

    # 补齐四维（active）后分类列填上，行数不变
    from models import Member
    item = db.query(AssetItem).filter_by(display_name="某完全未分类").one()
    classification_service.set_classification(db, item.id, {
        "asset_class_id": db.query(AssetClass).filter_by(code="A1").one().id,
        "liquidity_days": 0,
        "institution_id": db.query(Institution).one().id,
        "owner_id": db.query(Member).one().id})
    records = export_service.get_export_records(db)
    assert [r["asset_name"] for r in records] == ["某完全未分类"]
    assert records[0]["asset_class"] == "A 现金与等价物/A1 活期存款"