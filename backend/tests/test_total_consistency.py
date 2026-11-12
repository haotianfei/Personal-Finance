"""总额口径同源不变量（ADR-0020 D4）——所有「自称总额」的出口必须与 `get_summary` 相等。

口径（ADR-0020 D1，取代 ADR-0019 D9 的相反表述）：

1. `asset_items.status ∈ {active, archived, draft}`，`draft` ＝ 分类四维未补齐；
2. **draft 计入** as-of 合计；只有 `archived` 排除（ADR-0017 D2：自归档生效日起退出
   计量，生效日之前的真实历史值保留）；
3. 合计的**唯一权威实现**是 `analysis_service._sum_as_of`（经 `get_summary` 暴露）。
   凡是向外自称「总额/净值/总资产」的出口，数值必须与之**精确相等**（`Decimal`
   相等，不是近似——金额是定点数，`pytest.approx` 会把真漂移吃成噪声）；
4. 未补齐规模披露**单点产出**在 `get_summary` 的 `unclassified_count` /
   `unclassified_asset_amount` / `unclassified_liability_amount`。其他出口**不要求**
   复制这三个字段，但必须与 summary 的总额同源。

本文件只做对账，不搭 HTTP：全部调用服务层，形状与路由/前端实际取数一致。
数据一律合成（「示例-」、虚构机构/成员、整数合成金额），不含任何真实数据。

⚠️ 新增任何「总额/净值」出口**必须登记到 `DECLARED_TOTAL_EXPORTS`**（ADR-0020 D4
验收方式）。登记制的已知弱点：未登记的新出口即使不同源也不会让本文件失败，因此
code review 时须对照 `routers/` 是否新增了求和端点。
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from models import Asset, AssetClass, AssetItem, Institution, Member
from services import (
    allocation_service,
    analysis_service,
    attribution_service,
    classification_service,
    export_service,
    goal_service,
    metric_service,
    proportion_service,
)
from services.advisor.info_set import build_info_set
from services.advisor.read import DbReadModel
from services.asset_service import create_asset

# 快照日：相对「今天」定义，使目标进度（`effective_from`＝今天）与 as-of 对账同日可比
TODAY = date.today()
D_PREV = TODAY - timedelta(days=28)

MONEY_Q = Decimal("0.01")

# ---- 合成规模（虚构整数，非真实财务数据） -------------------------------------
DRAFT_ASSET_AMOUNT = Decimal("300.00")        # draft 资产（计入合计 + 披露）
DRAFT_LIABILITY_AMOUNT = Decimal("100.00")    # draft 负债（计入合计 + 披露）
ACTIVE_ASSET_AMOUNT = Decimal("1000.00")      # 已分类 active 资产（计入合计）
CARRIED_ASSET_AMOUNT = Decimal("400.00")      # 仅 D_PREV 有行 → 沿用到 TODAY
ARCHIVED_ASSET_AMOUNT = Decimal("5000.00")    # archived，生效日＝TODAY → TODAY 起退出
CORRECTED_ASSET_AMOUNT = Decimal("70.00")     # 行被 supersede → 不入任何 as-of 口径

EXPECTED_ASSETS = (DRAFT_ASSET_AMOUNT + ACTIVE_ASSET_AMOUNT
                   + CARRIED_ASSET_AMOUNT)
EXPECTED_LIABILITIES = DRAFT_LIABILITY_AMOUNT
EXPECTED_NET_WORTH = EXPECTED_ASSETS - EXPECTED_LIABILITIES

# D_PREV 当日：归档项尚未生效（仍在计量）＋ 沿用行＝该项自己的首行
EXPECTED_ASSETS_AT_PREV = CARRIED_ASSET_AMOUNT + ARCHIVED_ASSET_AMOUNT


# ---------------------------------------------------------------- 数据构造
# 构造风格照抄 `test_draft_disclosure.py`（同一套 mk_item / classify / add_row）。


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
    """draft 资产/负债 ＋ active 资产 ＋ 沿用项 ＋ 归档项 ＋ 被更正行 ＋ 一个目标。

    - draft 300/100：**计入** TODAY 合计，且进未补齐披露；
    - active 1000：普通项；
    - 沿用项 400：仅 D_PREV 有行 → TODAY 的 as-of 值沿用（检验「同日有行」类实现）；
    - archived 5000：行在 D_PREV，`archived_effective_from=TODAY` → D_PREV 仍计量、
      TODAY 起退出（ADR-0017 D2）；
    - 被更正行 70：`superseded_by` 非空 → 不入合计（检验重复计数）；
    - 目标：给「目标达成度」出口提供可调用的前置数据（目标本身不贡献金额）。
    """
    draft_asset = mk_item(db, "示例-待分类基金")
    add_row(db, draft_asset, DRAFT_ASSET_AMOUNT, TODAY)

    draft_liability = mk_item(db, "示例-待分类贷款")
    add_row(db, draft_liability, DRAFT_LIABILITY_AMOUNT, TODAY, side="liability")

    active = classify(db, mk_item(db, "示例-已分类股票").id)
    active_row = add_row(db, active, ACTIVE_ASSET_AMOUNT, TODAY)

    carried = classify(db, mk_item(db, "示例-上期录值未更新").id)
    add_row(db, carried, CARRIED_ASSET_AMOUNT, D_PREV)

    archived = mk_item(db, "示例-已归档旧账户")
    add_row(db, archived, ARCHIVED_ASSET_AMOUNT, D_PREV)
    archived.status = "archived"
    archived.archived_effective_from = TODAY
    db.commit()

    # 被更正行：superseded_by 指向同日另一有效行（只验证「superseded 不入合计」，
    # 不关心替换行的归属语义；与 test_draft_disclosure 的 fixture 捷径同款）
    corrected = classify(db, mk_item(db, "示例-被更正项").id)
    corrected_row = add_row(db, corrected, CORRECTED_ASSET_AMOUNT, TODAY)
    db.execute(update(Asset).where(Asset.id == corrected_row.id)
               .values(superseded_by=active_row.id))
    db.commit()

    goal_service.create_goal(
        db, name="示例目标 2030", target_amount="2000000.00",
        target_date=date(2030, 12, 31), reason="合成目标理由")

    return {
        "draft_asset": draft_asset,
        "draft_liability": draft_liability,
        "active": active,
        "carried": carried,
        "archived": archived,
        "corrected": corrected,
    }


# ---------------------------------------------------------------- 工具


def _money(value, *, source: str) -> Decimal:
    """出口金额 → Decimal；`None`（被阻断/字段缺失）直接判失败，不静默跳过。"""
    assert value is not None, f"{source}: 金额为 None（被闸门阻断或字段缺失）"
    return Decimal(str(value))


def _summary_baseline(db) -> dict:
    """权威总额：`get_summary` 在最新快照日上的三个合计。"""
    summary = analysis_service.get_summary(db)
    assert summary.latest_date == TODAY, "数据集自检：最新快照日应为 TODAY"
    return {
        "total_assets": summary.total_assets,
        "total_liabilities": summary.total_liabilities,
        "net_worth": summary.net_worth,
    }


# ---- 各出口取数（签名统一为 (db, as_of) → {字段: Decimal}） ---------------------


def _metric_status_totals(db, as_of):
    """指标卡 status 聚合（`metric_service.get_status` 的 B1 三项）。"""
    items = {i.id: i for i in metric_service.get_status(db, as_of).items}
    return {
        "total_assets": _money(items["asset_total"].value,
                               source="metric.asset_total"),
        "total_liabilities": _money(items["liability_total"].value,
                                    source="metric.liability_total"),
        "net_worth": _money(items["net_worth"].value, source="metric.net_worth"),
    }


def _goal_progress_totals(db, as_of):
    """目标达成度（`goal_service.get_goal_progress` 的 `net_worth`）。"""
    target = goal_service.resolve_target(db, as_of)
    assert target is not None, "fixture 未提供有效目标"
    goal, _version = target
    payload = goal_service.get_goal_progress(db, goal.id, as_of)
    return {"net_worth": _money(payload["net_worth"],
                                source="goal.progress.net_worth")}


def _proportion_owner_totals(db, as_of):
    """占比（归属维度）：全部项都进桶（LEFT JOIN 字典表，缺归属记「未指定」）。"""
    data = proportion_service.get_proportion_data(db, "owner", snapshot_date=as_of)
    return {
        "total_assets": _money(data["total_assets"],
                               source="proportion.owner.total_assets"),
        "total_liabilities": _money(data["total_liabilities"],
                                    source="proportion.owner.total_liabilities"),
        "net_worth": _money(data["net_worth"],
                            source="proportion.owner.net_worth"),
    }


def _proportion_asset_name_totals(db, as_of):
    """占比（名称维度）：无字典表 join，全部项都在。"""
    data = proportion_service.get_proportion_data(db, "asset_name",
                                                  snapshot_date=as_of)
    return {
        "total_assets": _money(data["total_assets"],
                               source="proportion.asset_name.total_assets"),
        "total_liabilities": _money(
            data["total_liabilities"], source="proportion.asset_name.total_liabilities"),
        "net_worth": _money(data["net_worth"],
                            source="proportion.asset_name.net_worth"),
    }


def _proportion_all_totals(db, as_of):
    """占比聚合端点 `get_all_proportions` 里归属维度的合计（同一 handler 再走一遍）。"""
    data = proportion_service.get_all_proportions(db, snapshot_date=as_of)
    owner = data["owner"]
    return {
        "total_assets": _money(owner["total_assets"],
                               source="proportion.all.owner.total_assets"),
        "total_liabilities": _money(owner["total_liabilities"],
                                    source="proportion.all.owner.total_liabilities"),
    }


def _attribution_totals(db, as_of):
    """归因（组合层）期末净值 `net_worth_end`。"""
    payload = attribution_service.get_attribution(db, scope="portfolio",
                                                  date_to=as_of)
    assert payload["date_to"] == as_of.isoformat(), "归因窗口末未落在 as_of"
    return {"net_worth": _money(payload["net_worth_end"],
                                source="attribution.net_worth_end")}


def _allocation_totals(db, as_of):
    """配置分析分母（`get_total_assets`：仅资产侧）。"""
    return {"total_assets": _money(
        allocation_service.get_total_assets(db, as_of),
        source="allocation.get_total_assets")}


def _advisor_read_totals(db, as_of):
    """顾问信息集数值来源（`DbReadModel.net_worth`）。"""
    nw = DbReadModel(db).net_worth(as_of)
    return {
        "total_assets": _money(nw["assets"], source="advisor.read.nw.assets"),
        "total_liabilities": _money(nw["liabilities"],
                                    source="advisor.read.nw.liabilities"),
        "net_worth": _money(nw["total"], source="advisor.read.nw.total"),
    }


def _advisor_info_set_totals(db, as_of):
    """冻结后的信息集条目（`nw.total` / `nw.assets` / `nw.liabilities`）。"""
    info = build_info_set(DbReadModel(db), as_of=as_of)
    entries = {e["key"]: e["value"] for e in info["entries"]}
    return {
        "total_assets": _money(entries.get("nw.assets"),
                               source="info_set.nw.assets"),
        "total_liabilities": _money(entries.get("nw.liabilities"),
                                    source="info_set.nw.liabilities"),
        "net_worth": _money(entries.get("nw.total"), source="info_set.nw.total"),
    }


def _export_totals(db, as_of):
    """导出：as-of 取值来源行必须 1:1 出现在导出记录里，按 side 求和＝合计。

    「行数与金额合计」都断言：缺任何一条 as-of 源行（如 draft 项的行）即失败，
    不靠「总和碰巧相等」蒙过。
    """
    records = export_service.get_export_records(db, period_type="all")
    available: dict[tuple, int] = {}
    for row in records:
        key = (row["asset_date"], row["asset_name"],
               str(Decimal(str(row["amount"])).quantize(MONEY_Q)),
               row["side"])
        available[key] = available.get(key, 0) + 1

    assets = Decimal("0")
    liabilities = Decimal("0")
    missing: list[str] = []
    for name, amount, _asset_id, side, row_date in (
            analysis_service._get_latest_records_as_of(db, as_of)):
        amt = Decimal(str(amount))
        key = (row_date.strftime("%Y-%m-%d"), name,
               str(amt.quantize(MONEY_Q)), side or "asset")
        if available.get(key, 0) <= 0:
            missing.append(f"{name} @ {key[0]} ({key[3]})")
            continue
        available[key] -= 1
        if (side or "asset") == "liability":
            liabilities += amt
        else:
            assets += amt

    assert not missing, (
        "导出记录缺少 as-of 取值来源行（导出与合计不同源）：" + "、".join(missing))
    return {"total_assets": assets, "total_liabilities": liabilities,
            "net_worth": assets - liabilities}


#: 出口登记表（ADR-0020 D4）——**新增任何总额/净值出口必须登记到此**。
#: `call(db, as_of)` 返回 {字段: Decimal}；字段名须与 `get_summary` 一致。
#: `historic`=能否在非最新快照日上对账（目标进度的 as-of 不得早于目标生效日，
#: 故标 False；其余出口都支持 as-of 取数）。
DECLARED_TOTAL_EXPORTS = (
    {"name": "metric.status", "call": _metric_status_totals, "historic": True},
    {"name": "goal.progress", "call": _goal_progress_totals, "historic": False},
    {"name": "proportion.owner", "call": _proportion_owner_totals, "historic": True},
    {"name": "proportion.asset_name", "call": _proportion_asset_name_totals,
     "historic": True},
    {"name": "proportion.all.owner", "call": _proportion_all_totals,
     "historic": True},
    {"name": "attribution.portfolio", "call": _attribution_totals, "historic": True},
    {"name": "allocation.total_assets", "call": _allocation_totals,
     "historic": True},
    {"name": "advisor.read.net_worth", "call": _advisor_read_totals,
     "historic": True},
    {"name": "advisor.info_set", "call": _advisor_info_set_totals,
     "historic": True},
    {"name": "export.csv", "call": _export_totals, "historic": True},
)

# 说明（不登记的原因，不是放宽断言）：占比的 `asset_class` / `liquidity` /
# `institution` / `channel` / `risk_level` / `tag` 维度是**分桶视图**——文档明确
# 「未分类行不进任何桶」（`proportion_service.get_proportion_by_asset_class`），
# draft 项四维未补齐，天然不在桶里。它们对外自称的是「该维度的分布」，不是总额，
# 故不属 D4 约束对象。若产品要求这些页面也显示总额，须改为从 `get_summary` 取数
# 而不是把桶求和当总额——那时才登记进本清单。


# ---------------------------------------------------------------- ① 同源不变量


@pytest.mark.parametrize("entry", DECLARED_TOTAL_EXPORTS,
                         ids=[e["name"] for e in DECLARED_TOTAL_EXPORTS])
def test_declared_export_matches_summary(entry, db, portfolio):
    """最新快照日：每个已登记出口的总额字段都必须**精确等于** `get_summary`。"""
    baseline = _summary_baseline(db)
    totals = entry["call"](db, TODAY)
    for field, value in totals.items():
        assert value == baseline[field], (
            f"{entry['name']}.{field} 与 summary 不同源："
            f"出口值={value}，权威值={baseline[field]}，"
            f"差值={value - baseline[field]:+}")


@pytest.mark.parametrize("entry", DECLARED_TOTAL_EXPORTS,
                         ids=[e["name"] for e in DECLARED_TOTAL_EXPORTS])
def test_declared_export_matches_as_of_baseline(entry, db, portfolio):
    """历史 as-of：同一批出口在 D_PREV 上须等于 `_sum_as_of`（权威实现）。

    目的：把「as-of 语义」本身也钉住——归档项在生效日之前仍在计量（ADR-0017 D2），
    任何「只看当日有行」或「无条件排除归档」的实现都会在这条上露馅。
    """
    if not entry["historic"]:
        pytest.skip("该出口不支持早于目标生效日的 as-of（目标进度 422）")
    expected_net, expected_assets, expected_liabilities = (
        analysis_service._sum_as_of(db, D_PREV))
    assert expected_assets == EXPECTED_ASSETS_AT_PREV, "数据集自检：D_PREV 资产合计"
    baseline = {"total_assets": expected_assets,
                "total_liabilities": expected_liabilities,
                "net_worth": expected_net}
    totals = entry["call"](db, D_PREV)
    for field, value in totals.items():
        assert value == baseline[field], (
            f"{entry['name']}.{field} 在 {D_PREV} 与权威实现不同源："
            f"出口值={value}，权威值={baseline[field]}，"
            f"差值={value - baseline[field]:+}")


def test_summary_is_a_thin_wrapper_of_sum_as_of(db, portfolio):
    """`get_summary` 自身不得是第二套求和：三个合计＝`_sum_as_of(最新日)`。"""
    summary = analysis_service.get_summary(db)
    net, assets, liabilities = analysis_service._sum_as_of(db, summary.latest_date)
    assert summary.total_assets == assets
    assert summary.total_liabilities == liabilities
    assert summary.net_worth == net


def test_allocation_total_includes_carry_forward_rows(db, portfolio):
    """配置分析分母必须走 as-of 沿用，而不是「只看当日有行的项」。

    账本语义是变更日志（ADR-0019 D12）：某日某项没有记录＝值没变，不是「没核对」。
    故该日总资产必须含沿用项的金额（本 fixture 的「示例-上期录值未更新」只在
    D_PREV 有行，TODAY 沿用其值）。任何按 `asset_date == 当日` 求和的实现都会
    静默少算——少算的量正是「当日没更新的持仓」，恰好是最常见的那部分。
    """
    total = allocation_service.get_total_assets(db, TODAY)
    expected = analysis_service._sum_as_of(db, TODAY)[1]
    assert total == expected, (
        f"配置分母与 as-of 合计不同源：出口值={total}，权威值={expected}，"
        f"差值={total - expected:+}（差额＝当日无行、按 as-of 沿用的项）")
    assert total == EXPECTED_ASSETS


# ---------------------------------------------------------------- ② draft 计入


def test_draft_items_count_in_totals(db, portfolio):
    """draft 资产 300 与 draft 负债 100 **计入**合计（ADR-0020 D1）。"""
    summary = analysis_service.get_summary(db)

    assert summary.latest_date == TODAY
    assert summary.total_assets == EXPECTED_ASSETS
    assert summary.total_liabilities == EXPECTED_LIABILITIES
    assert summary.net_worth == EXPECTED_NET_WORTH

    # 反向保护：draft 的钱若被排除，合计会正好少掉这 300/100（不近似比较）
    assert summary.total_assets != EXPECTED_ASSETS - DRAFT_ASSET_AMOUNT
    assert summary.total_liabilities != (
        EXPECTED_LIABILITIES - DRAFT_LIABILITY_AMOUNT)


def test_archived_item_leaves_totals_only_from_effective_date(db, portfolio):
    """归档项自生效日（TODAY）起退出计量；生效日之前照常计入（ADR-0017 D2）。"""
    prev_net, prev_assets, prev_liabilities = analysis_service._sum_as_of(db, D_PREV)
    assert prev_assets == EXPECTED_ASSETS_AT_PREV   # 含归档 5000
    assert prev_liabilities == Decimal("0")
    assert prev_net == EXPECTED_ASSETS_AT_PREV

    summary = analysis_service.get_summary(db)      # 最新日＝TODAY
    assert summary.total_assets == EXPECTED_ASSETS  # 不含归档 5000
    assert summary.net_worth == EXPECTED_NET_WORTH


# ---------------------------------------------------------------- ③ 披露正确性


def test_disclosure_matches_draft_scale_exactly(db, portfolio):
    """披露＝draft 项按 side 拆的毛额 + 计数，且**不含归档项**。

    `unclassified_count` 计「该日有有效记录」的 draft 项数：2（资产 1 + 负债 1）；
    归档项既不进合计也不进披露（它不是 draft，且已退出计量）。
    """
    summary = analysis_service.get_summary(db)

    assert summary.unclassified_count == 2
    assert summary.unclassified_asset_amount == DRAFT_ASSET_AMOUNT
    assert summary.unclassified_liability_amount == DRAFT_LIABILITY_AMOUNT

    # 归档项不受披露「顺手带上」：披露的资产侧毛额严格小于合计
    assert summary.unclassified_asset_amount < summary.total_assets
    assert (summary.total_assets - summary.unclassified_asset_amount
            == ACTIVE_ASSET_AMOUNT + CARRIED_ASSET_AMOUNT)
    # 披露的两侧毛额之差＝draft 项对净值的贡献（可被界面一句人话解释）
    assert (summary.unclassified_asset_amount
            - summary.unclassified_liability_amount
            == DRAFT_ASSET_AMOUNT - DRAFT_LIABILITY_AMOUNT)


def test_disclosure_is_scoped_to_as_of(db, portfolio):
    """披露随 as-of 走：D_PREV 没有 draft 项的有效记录 → 披露归零形状。

    （draft 项首行录在 TODAY；「无记录的项不进 count」——净值里本来也没有它的份额。）
    """
    count, asset_amount, liability_amount = analysis_service._unclassified_as_of(
        db, D_PREV)
    assert count == 0
    assert asset_amount == Decimal("0")
    assert liability_amount == Decimal("0")


# ---------------------------------------------------------------- ④ 导出不丢 draft


def test_export_csv_contains_draft_rows(db, portfolio):
    """导出 CSV 必须包含 draft 项的行（ADR-0020 D1：draft 计入合计 ⇒ 导出不得丢行）。

    导出是「全量行 dump」：只要某行是某天 as-of 的取值来源，它就必须在 CSV 里，
    否则拿导出对账首页合计必然对不上。
    """
    records = export_service.get_export_records(db, period_type="all")
    csv_text = export_service.generate_csv(records)
    lines = csv_text.strip().splitlines()

    # 行数：表头 + 每条记录一行（不合并、不截断）
    assert len(lines) == len(records) + 1

    for item in (portfolio["draft_asset"], portfolio["draft_liability"]):
        assert item.display_name in csv_text, (
            f"导出 CSV 缺少 draft 项的行：{item.display_name}"
            f"（该行是 as-of 取值来源，丢行即与 summary 合计不同源）")


def test_export_row_count_of_as_of_source_rows(db, portfolio):
    """as-of 源行与导出行的条数必须一致（逐行对齐，不看总和碰巧相等）。"""
    rows = analysis_service._get_latest_records_as_of(db, TODAY)
    records = export_service.get_export_records(db, period_type="all")
    exported = {(r["asset_date"], r["asset_name"], str(
        Decimal(str(r["amount"])).quantize(MONEY_Q)), r["side"]) for r in records}

    for name, amount, _asset_id, side, row_date in rows:
        key = (row_date.strftime("%Y-%m-%d"), name,
               str(Decimal(str(amount)).quantize(MONEY_Q)), side or "asset")
        assert key in exported, f"导出缺少 as-of 源行：{name} @ {key[0]}"