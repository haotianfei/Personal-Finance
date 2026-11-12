"""as-of 聚合硬化（spec §4 P0-1..P0-3 / §6.1–6.2）+ P1 漂移诊断（spec §3，issue 02）。

数据全部合成（虚构机构、示例条目、示例金额），不含真实财务信息。

事故形状见 `.scratch/item-identity-drift/spec.md` §0：08-31 旧命名项有值 →
09-16 新命名项首次录入且旧项当日无行 → 09-17 旧项被显式写成 0。

1) P0：归档项（ADR-0017 D2）退出一切 as-of 聚合，分析侧与占比侧同口径；
   只有 superseded 行的日期不撑出趋势 x 轴点；`get_mixed_chart` 一次请求
   只跑一次 as-of 聚合（comparison 由 trend 派生）。
2) P1（issue 02）：趋势点带 `added` / `vanished` / `drift_suspect` / `drift_amount`
   逐点漂移诊断——只诊断、不改写（D3），09-16 形状被标出为可疑而非被静默修正。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import event, select

from models import Asset, AssetClass, AssetItem, Institution, Member
from services import analysis_service, asset_service, classification_service
from services.analysis_service import (
    DRIFT_THRESHOLD, get_period_comparison, get_total_trend,
)
from services.proportion_service import (
    get_available_periods, get_proportion_by_asset_class, get_proportion_by_asset_name,
    get_snapshot_date_by_period,
)

D_OLD = date(2026, 8, 31)      # day1：旧命名项 A 有值
D_NEW = date(2026, 9, 16)      # day2：新命名项 B 首录，A 当日无行
D_ZERO = date(2026, 9, 17)     # day3：A 被显式写成 0
D_ONLY_SUPERSEDED = date(2026, 9, 20)


def _mk(db, name, amount, on_date, side="asset", item_id=None):
    """合成一条快照行（经 ledger 单点解析资产项；item_id 已知时直接挂项）。

    ADR-0019：分类在**资产项**上，记录侧只读——本文件除归档用例外的行不需要分类，
    该用例用 `_classify` 在项上打点。
    """
    return asset_service.create_asset(
        db, name, Decimal(str(amount)), on_date,
        side=side, item_id=item_id,
    )


def _classify(db, item_id, klass, liquidity_days=0):
    """在资产项上设分类（项级唯一写入口）。"""
    institution = db.execute(select(Institution)).scalars().first()
    if institution is None:
        institution = Institution(name="示例银行", kind="bank")
        db.add(institution)
        db.commit()
    member = db.execute(select(Member)).scalars().first()
    if member is None:
        member = Member(name="本人", role="self")
        db.add(member)
        db.commit()
    classification_service.set_classification(db, item_id, {
        "asset_class_id": klass.id, "liquidity_days": liquidity_days,
        "institution_id": institution.id, "owner_id": member.id,
    })


def _archive(db, item_id):
    """R-37：人裁决归档（状态机 active→archived，此处直接置状态）。"""
    db.get(AssetItem, item_id).status = "archived"
    db.commit()


def _incident_shape(db):
    """复现 09-16 事故的三种行：旧项有值 / 新项首录 / 旧项次日归零。

    背景项 C（示例-活期存款）每天正常更新——真实数据里持续维护的项不该被当成
    幽灵值；只有「沿用且无后继」的旧项 A 才是事故的 vanished 项。
    """
    old = _mk(db, "示例银行-贵金属-黄金", "1200000.00", D_OLD)   # A（旧命名）
    other = _mk(db, "示例-活期存款", "100", D_OLD)                # C（无关项）
    new = _mk(db, "示例银行-黄金账户", "1180000.00", D_NEW)       # B（改名即新建 item）
    assert new.asset_item_id != old.asset_item_id
    _mk(db, "示例-活期存款", "100", D_NEW)                        # C 当日正常更新
    _mk(db, "示例-活期存款", "100", D_ZERO)
    _mk(db, "示例银行-贵金属-黄金", "0", D_ZERO,
        item_id=old.asset_item_id)                                # A 显式归零
    return old, new, other


# ---------------- 1. 事故形状：P0 口径可解释，双算留给 P1 ----------------
def test_incident_shape_series_equals_asof_sum_of_distinct_identities(db):
    """P0 保证「series == 各**不同身份**的 as-of 值之和」，不保证消除双算。"""
    old, new, other = _incident_shape(db)

    points = {p.period: p for p in get_total_trend(db, "day")}
    assert set(points) == {"2026-08-31", "2026-09-16", "2026-09-17"}

    day2 = points["2026-09-16"]
    # 三个不同身份：旧项被沿用 1,200,000.00 + 新项 1,180,000.00 + 无关项 100
    assert day2.total_amount == Decimal("2380100.00")
    assert day2.net_assets == Decimal("2380100.00")
    assert day2.liabilities == Decimal("0")

    rows = analysis_service._get_latest_records_as_of(db, D_NEW)
    assert len(rows) == 3                                    # 每个身份恰一行
    assert sum(Decimal(str(r[1])) for r in rows) == day2.total_amount
    assert {db.get(Asset, r[2]).asset_item_id for r in rows} == {
        old.asset_item_id, new.asset_item_id, other.asset_item_id}

    # 旧项次日 0 行生效 → 双算消失（这正是 09-17 回落 ~1.28M 的来源）
    assert points["2026-09-17"].total_amount == Decimal("1180100.00")
    assert points["2026-08-31"].total_amount == Decimal("1200100.00")


# ---------------- 1b. P1 漂移诊断（spec §3，issue 02） ----------------
def test_incident_shape_drift_flags_double_count(db):
    """09-16 形状：point2 报 added=[B] 与 vanished=[A]，point1/point3 干净。"""
    old, new, _other = _incident_shape(db)
    pts = {p.period: p for p in get_total_trend(db, "day")}
    day1, day2, day3 = pts["2026-08-31"], pts["2026-09-16"], pts["2026-09-17"]

    # point1（库内首个快照日）：无前序基线，不把初始持仓标成「今日新增」
    assert day1.added == [] and day1.vanished == []
    assert day1.drift_amount == Decimal("0") and day1.drift_suspect is False

    # point2（事故点）：B 首录 + A 沿用且次日归零 → 双算可疑
    assert [i.item_id for i in day2.added] == [new.asset_item_id]
    assert day2.added[0].amount == Decimal("1180000.00")
    assert [i.item_id for i in day2.vanished] == [old.asset_item_id]
    assert day2.vanished[0].amount == Decimal("1200000.00")
    assert day2.vanished[0].last_date == D_OLD
    # drift_amount = max(Σadded, Σvanished)；ratio ≈ 50.9% ≥ 10%
    assert day2.drift_amount == Decimal("1200000.00")
    assert day2.drift_amount / day2.total_amount >= DRIFT_THRESHOLD
    assert day2.drift_suspect is True

    # point3（A 当日有 0 行＝更新而非沿用；B 成为最新快照的当前值）：干净
    assert day3.added == [] and day3.vanished == []
    assert day3.drift_amount == Decimal("0") and day3.drift_suspect is False

    # meta 探测位（spec §3）与 JSON 形状（additive）
    chart = analysis_service.get_mixed_chart(db, "day")
    assert chart.meta.drift_enabled is True
    payload = chart.trend[1].model_dump(mode="json")
    assert payload["added"] == [{
        "item_id": new.asset_item_id, "name": "示例银行-黄金账户",
        "amount": "1180000.00"}]
    assert payload["vanished"] == [{
        "item_id": old.asset_item_id, "name": "示例银行-贵金属-黄金",
        "amount": "1200000.00", "last_date": "2026-08-31"}]
    assert payload["drift_suspect"] is True
    assert payload["drift_amount"] == "1200000.00"


def test_legit_new_item_below_threshold_is_not_suspect(db):
    """真·新增（首录后持续更新）：added 非空、vanished 空，未达阈值不报警。"""
    _mk(db, "示例-存量", "1000", D_OLD)
    _mk(db, "示例-存量", "1000", D_NEW)
    _mk(db, "示例-存量", "1000", D_ZERO)
    added_row = _mk(db, "示例-新增", "50", D_NEW)     # 首录当天
    _mk(db, "示例-新增", "50", D_ZERO)                # 之后持续更新（有后继）

    pts = {p.period: p for p in get_total_trend(db, "day")}
    day2 = pts["2026-09-16"]
    assert [i.item_id for i in day2.added] == [added_row.asset_item_id]
    assert day2.added[0].amount == Decimal("50")
    assert day2.vanished == []
    # 50 / 1050 ≈ 4.8% < 10% → 新增合法，不报警
    assert day2.drift_amount == Decimal("50")
    assert day2.drift_suspect is False
    assert pts["2026-09-17"].added == [] and pts["2026-09-17"].drift_suspect is False


def test_drift_zero_amount_candidates_dropped_and_sorted(db):
    """零值候选不入榜、不计数；榜单按金额降序（ADR-0015：0 是测量不是漂移）。"""
    _mk(db, "示例-存量", "1000", D_OLD)
    _mk(db, "示例-存量", "1000", D_NEW)
    _mk(db, "示例-存量", "1000", D_ZERO)          # 保证 D_NEW 之后仍有快照日
    big = _mk(db, "示例-旧-大", "9000", D_OLD)     # D_NEW 沿用且无后继（幽灵值）
    small = _mk(db, "示例-旧-小", "100", D_OLD)
    zero = _mk(db, "示例-旧-零", "0", D_OLD)       # 沿用值＝0 → 噪声
    n_big = _mk(db, "示例-新-大", "500", D_NEW)    # 首录（非零）
    n_small = _mk(db, "示例-新-小", "10", D_NEW)
    n_zero = _mk(db, "示例-新-零", "0", D_NEW)     # 首录值＝0 → 噪声

    day2 = {p.period: p for p in get_total_trend(db, "day")}["2026-09-16"]

    # 零值整条剔除（不是排在末尾）
    assert zero.asset_item_id not in {i.item_id for i in day2.vanished}
    assert n_zero.asset_item_id not in {i.item_id for i in day2.added}
    # 金额降序
    assert [i.item_id for i in day2.vanished] == [big.asset_item_id, small.asset_item_id]
    assert [i.item_id for i in day2.added] == [n_big.asset_item_id, n_small.asset_item_id]
    assert [i.amount for i in day2.vanished] == [Decimal("9000"), Decimal("100")]
    # drift_amount 在剔除零值之后求和：max(500+10, 9000+100)
    assert day2.drift_amount == Decimal("9100")
    assert day2.drift_suspect is True


def test_zero_only_drift_point_not_suspect(db):
    """若某点「漂移」只剩零值行，则降噪后为干净点（不 suspect）。"""
    _mk(db, "示例-存量", "1000", D_OLD)
    _mk(db, "示例-存量", "1000", D_NEW)
    _mk(db, "示例-零值新增", "0", D_NEW)          # 唯一新增＝0 值行

    day2 = {p.period: p for p in get_total_trend(db, "day")}["2026-09-16"]
    assert day2.added == [] and day2.vanished == []
    assert day2.drift_amount == Decimal("0")
    assert day2.drift_suspect is False


def test_drift_identical_via_trend_injection_and_default(db):
    """mixed-chart 注入路径（trend=）与默认自取路径的漂移字段完全一致。"""
    _incident_shape(db)
    default_trend = get_total_trend(db, "day")
    chart = analysis_service.get_mixed_chart(db, "day")

    shape = [(p.period, p.total_amount, p.added, p.vanished,
              p.drift_suspect, p.drift_amount) for p in chart.trend]
    assert shape == [(p.period, p.total_amount, p.added, p.vanished,
                      p.drift_suspect, p.drift_amount) for p in default_trend]
    assert any(p.drift_suspect for p in chart.trend)   # 注入路径确实带上了诊断
    # comparison 亦从注入 trend 派生（默认路径等价）
    assert get_period_comparison(db, "day") == get_period_comparison(
        db, "day", trend=default_trend)


# ---------------- 2. 归档排除（ADR-0017 D2）：分析 + 占比同口径 ----------------
def test_archived_item_excluded_from_analysis_and_proportion(db):
    """归档项即使留着有值行，也不得计入任一 as-of 聚合点（两个服务同一谓词）。"""
    klass = AssetClass(code="T1", name="示例类型", level=1, side_scope="any")
    db.add(klass)
    db.commit()

    keep = _mk(db, "示例-在持", "1000", D_OLD)
    gone = _mk(db, "示例-已归档", "500", D_OLD)
    for row in (keep, gone):          # 分类打在**项**上（ADR-0019）
        _classify(db, row.asset_item_id, klass)

    # 归档前：两项都计
    assert analysis_service._sum_as_of(db, D_OLD)[0] == Decimal("1500.00")
    assert get_proportion_by_asset_class(db, D_OLD)["total_assets"] == Decimal("1500.00")

    _archive(db, gone.asset_item_id)

    # 分析侧：as-of 求和、趋势点都不含归档项（沿用旧值也被掐断）
    total, assets, liab = analysis_service._sum_as_of(db, D_OLD)
    assert (total, assets, liab) == (Decimal("1000.00"), Decimal("1000.00"), Decimal("0"))
    rows = analysis_service._get_latest_records_as_of(db, D_OLD)
    assert len(rows) == 1 and rows[0][0] == "示例-在持"        # 归档项整支退出

    month_points = {p.period: p for p in get_total_trend(db, "month")}
    assert month_points["2026-08"].total_amount == Decimal("1000.00")

    # 占比侧：同一谓词接线（asset_class 聚合与 asset_name 聚合都对归档项无感）
    prop = get_proportion_by_asset_class(db, D_OLD)
    assert prop["total_assets"] == Decimal("1000.00")
    assert [i["name"] for i in prop["asset_items"]] == ["示例类型"]
    assert prop["asset_items"][0]["count"] == 1
    assert prop["asset_items"][0]["percent"] == 100.0

    by_name = get_proportion_by_asset_name(db, D_OLD)
    assert [i["name"] for i in by_name["asset_items"]] == ["示例-在持"]
    assert by_name["total_assets"] == Decimal("1000.00")

    # 逐项趋势（B1 收口）：归档项不再贡献，任何点都为 0（贷款/资产均不例外）
    by_item = {p.period: p
               for p in analysis_service.get_trend_by_item(db, "示例-已归档", "month")}
    assert by_item and all(p.amount == Decimal("0") for p in by_item.values())


def test_archived_item_in_current_values_listing_but_not_in_totals(db):
    """汇总口径 vs 陈列口径的分界（ADR-0017 D2 只约束求和）。

    `/api/assets/current` 是「每项一行＋其最后记录」的陈列读模型：归档项仍显示
    自己的残留值，但同一份数据在净值/趋势里必须为 0。
    """
    _mk(db, "示例-在持", "1000", D_OLD)
    gone = _mk(db, "示例-已归档", "3000", D_OLD)
    _archive(db, gone.asset_item_id)

    # 汇总侧：归档项退出（as-of 求和 / 趋势 / mixed-chart）
    assert analysis_service._sum_as_of(db, D_OLD)[0] == Decimal("1000.00")
    assert {p.period: p for p in get_total_trend(db, "month")}[
        "2026-08"].total_amount == Decimal("1000.00")
    chart = analysis_service.get_mixed_chart(db, "month")
    assert chart.trend[0].total_amount == Decimal("1000.00")
    assert chart.comparison[0].total == Decimal("1000.00")

    # 陈列侧（include_archived=True）：归档项一行，amount＝其最后记录值
    listing = asset_service.get_current_values(db, D_OLD, include_archived=True)
    row = {i["item_id"]: i for i in listing["items"]}[gone.asset_item_id]
    assert row["item_status"] == "archived"
    assert row["amount"] == "3000.00"
    assert row["record_id"] == gone.id
    assert row["asset_date"] == D_OLD.isoformat()

    # 默认陈列：归档项整行不出现（不是 amount=None 的占位行）
    default = asset_service.get_current_values(db, D_OLD)
    assert gone.asset_item_id not in {i["item_id"] for i in default["items"]}


# ---------------- 3. 仅有 superseded 行的日期不进 x 轴 ----------------
def test_superseded_only_date_absent_from_snapshot_dates(db):
    live = _mk(db, "示例-在持", "100", D_OLD)
    # 该日期只留一条被更正（superseded）的行 → 不得成为趋势点
    db.add(Asset(asset_name="示例-旧读数", amount=Decimal("999"),
                 asset_date=D_ONLY_SUPERSEDED, side="asset",
                 superseded_by=live.id))
    db.commit()

    dates = analysis_service._get_all_snapshot_dates(db)
    assert D_ONLY_SUPERSEDED not in dates
    assert dates == [D_OLD]

    trend = get_total_trend(db, "day")
    assert [p.period for p in trend] == ["2026-08-31"]
    assert trend[0].total_amount == Decimal("100.00")

    # 占比侧的账期列表（B2 收口）：同一 active 行口径，不出现只有 superseded 行的账期
    assert get_snapshot_date_by_period(db, "2026-09", "month") is None
    assert [p["label"] for p in get_available_periods(db, "month")] == ["2026-08"]


# ---------------- 4. 单次 as-of 取数（P0-3 改版）+ 漂移批量取数（issue 02） ----------------
def test_mixed_chart_runs_single_pass_trend_and_bulk_drift(db, monkeypatch):
    """一次请求只发一次趋势取数，且 SQL 条数与账期数解耦；漂移只加少量批量查询。

    改前此用例锁的是「`_get_latest_records_as_of` 每请求恰好一次」（逐期 as-of
    聚合）。改后趋势取数不再逐期查库——`get_total_trend` 调一次
    `_latest_records_by_as_of`（一次取全量 active 行 + 内存扫掠），逐期调用的
    入口已不存在。不变量因此升级为更强的一条：**day 粒度 3 期与 1 期发出同样多的
    SQL**（账期数不再乘往返次数），仍每请求恰好一次批量取数。
    """
    _mk(db, "示例-甲", "1000", D_OLD)
    _mk(db, "示例-甲", "1200", D_NEW)
    _mk(db, "示例-甲", "1300", D_ZERO)

    passes: list[tuple] = []
    original = analysis_service._latest_records_by_as_of
    original_drift = analysis_service._drift_for_points

    def spy(db_session, as_of_dates):
        passes.append(tuple(as_of_dates))
        return original(db_session, as_of_dates)

    monkeypatch.setattr(analysis_service, "_latest_records_by_as_of", spy)

    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", _count)
    try:
        # 基线：漂移关掉时的查询数（只是量差，不改产品行为）
        monkeypatch.setattr(analysis_service, "_drift_for_points",
                            lambda db_session, pts: pts)
        analysis_service.get_mixed_chart(db, "year")     # 单年 → 1 个期点
        one_period = len(statements)

        statements.clear()
        analysis_service.get_mixed_chart(db, "day")      # 3 个快照日 → 3 个期点
        three_periods = len(statements)

        statements.clear()
        monkeypatch.setattr(analysis_service, "_drift_for_points", original_drift)
        chart = analysis_service.get_mixed_chart(db, "day")
        with_drift = len(statements)
    finally:
        event.remove(bind, "before_cursor_execute", _count)

    assert len(chart.trend) == 3
    assert chart.comparison[0].total == chart.trend[0].total_amount
    assert three_periods == one_period          # 账期数不乘数据库往返次数
    assert len(passes) == 3                     # 每个请求恰好一次批量取数
    assert len(passes[0]) == 1                  # year：一个代表日
    assert set(passes[1]) == {D_OLD, D_NEW, D_ZERO}   # day：一次覆盖全部代表日
    assert with_drift - three_periods <= 2      # 漂移 = 1 条批量 SQL


def test_period_comparison_injection_backward_compatible(db):
    """`trend=` 注入与默认自取结果一致；旧调用方（不传 trend）行为不变。"""
    _mk(db, "示例-甲", "1000", D_OLD)
    _mk(db, "示例-甲", "1200", D_ZERO)

    default = get_period_comparison(db, "month")
    trend = get_total_trend(db, "month")
    injected = get_period_comparison(db, "month", trend=trend)

    shape = lambda pts: [(p.period, p.total, p.change_amount, p.change_percent,
                          p.carried_items, p.carried_max_days)
                         for p in pts]
    assert shape(default) == shape(injected)
    assert [p.change_amount for p in default] == [Decimal("0"), Decimal("200.00")]

# ---------------- 3. 分类维度趋势（ADR-0019：项级分类 + 旧名 422） ----------------
def test_trend_by_asset_class_includes_descendants(db):
    """`by-type` 的「含所有后代」语义保住：选中父类别＝父类别及其全部后代。"""
    root = AssetClass(code="T1", name="示例一级", level=1, side_scope="any")
    other = AssetClass(code="T2", name="示例其他", level=1, side_scope="any")
    db.add_all([root, other])
    db.commit()                                   # 先落库拿到父 id
    leaf_a = AssetClass(code="T1a", name="示例二级甲", parent_id=root.id, level=2,
                        side_scope="any")
    db.add(leaf_a)
    db.commit()

    a = _mk(db, "示例-在父类", "1000", D_OLD)
    b = _mk(db, "示例-在兄弟类", "400", D_OLD)
    _classify(db, a.asset_item_id, leaf_a)      # 分类打在**项**上（叶子类别）
    _classify(db, b.asset_item_id, other)

    # 父类别：含后代 → 1000；兄弟类别不被牵连
    parent = {p.period: p for p in analysis_service.get_trend_by_type(
        db, root.id, "month")}
    assert parent["2026-08"].amount == Decimal("1000")
    # 叶子：只算自己
    leaf = {p.period: p for p in analysis_service.get_trend_by_type(
        db, leaf_a.id, "month")}
    assert leaf["2026-08"].amount == Decimal("1000")
    # 未知类别 id：空序列（不抛错，与改前一致）
    assert analysis_service.get_trend_by_type(db, 9999, "month") == []


def test_trend_by_liquidity_takes_days_and_rejects_names(db):
    """流动性维度入参是**整数档位天数**：档位名称与非法天数一律 422。"""
    cash = _mk(db, "示例-活期", "1000", D_OLD)
    _classify(db, cash.asset_item_id, AssetClass(
        code="T3", name="示例现金", level=1, side_scope="any"), liquidity_days=0)
    db.commit()

    points = analysis_service.get_trend_by_liquidity(db, 0, "month")
    assert {p.period: p.amount for p in points}["2026-08"] == Decimal("1000.00")
    # 数字字符串照收（query 参数就是字符串）
    assert analysis_service.get_trend_by_liquidity(db, "0", "month")

    import pytest as _pytest

    from errors import ApiError, ErrorCode
    for bad in ("T+0 随时", 2, 12345):
        with _pytest.raises(ApiError) as ei:
            analysis_service.get_trend_by_liquidity(db, bad, "month")
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.status_code == 422


def test_deprecated_trend_entrypoints_raise_422(db):
    """旧维度读法不再静默返回空序列（ADR-0019）：422 并指路新端点/新参数。"""
    import pytest as _pytest

    from errors import ApiError, ErrorCode
    for fn, arg in ((analysis_service.get_trend_by_liquidity_rating, "T+0 随时"),
                    (analysis_service.get_trend_by_account, 1)):
        with _pytest.raises(ApiError) as ei:
            fn(db, arg, "month")
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
        assert ei.value.status_code == 422
        assert "已废弃" in ei.value.message


def test_trend_by_institution_and_tag(db):
    """机构/标签维度趋势读项级列（机构取代旧 account；标签是多值）。"""
    from models import Institution, Member, Tag

    inst = Institution(name="示例银行", kind="bank")
    owner = Member(name="本人", role="self")
    tag = Tag(name="示例标签", kind="free")
    klass = AssetClass(code="T4", name="示例类别", level=1, side_scope="any")
    db.add_all([inst, owner, tag, klass])
    db.commit()

    row = _mk(db, "示例-基金", "800", D_OLD)
    classification_service.set_classification(db, row.asset_item_id, {
        "asset_class_id": klass.id, "liquidity_days": 7,
        "institution_id": inst.id, "owner_id": owner.id,
    })
    classification_service.set_tags(db, row.asset_item_id, [tag.id])

    by_inst = {p.period: p.amount for p in analysis_service.get_trend_by_institution(
        db, inst.id, "month")}
    assert by_inst["2026-08"] == Decimal("800.00")
    by_tag = {p.period: p.amount for p in analysis_service.get_trend_by_tag(
        db, tag.id, "month")}
    assert by_tag["2026-08"] == Decimal("800.00")
    assert analysis_service.get_trend_by_channel(db, 1, "month") == []   # 无该渠道
