"""v5 二期增强「净资产变动归因瀑布」测试（规格 .scratch/v5-attribution/spec.md §4 全组）。

恒等式对拍（Decimal 精确相等）、转移段（组合 0/gross、item 净额、退化版无 2.3
数据自然为 0）、借贷场景 side 定号（Δ=0/external=0）、窗口半开区间边界（start 当日
不计、end 当日计）、降级（PRINCIPAL_NOT_SET＋ratio／NO_DATA）、carry-forward 始末
实际快照日回显、只读零变更、端点参数信封。数据一律合成（「示例-」）。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from errors import ApiError, ErrorCode
from models import Asset, AssetItem
from routers.analysis import attribution as route_attribution
from routers.asset_items import PrincipalRequest, set_principal as route_set_principal
from routers.transfer_events import TransferRequest, create_transfer as route_create
from services import analysis_service, asset_service, attribution_service

D = date


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _set(db, item_id, amount, eff, reason="期初投入"):
    return route_set_principal(item_id, PrincipalRequest(
        amount=str(amount), effective_date=eff.isoformat(), reason=reason), db)


def _mkitem(db, name, amount, d, side="asset"):
    a = asset_service.create_asset(db, name, _dec(amount), d, side=side)
    return a.asset_item_id


def _snap(db, item_id, name, amount, d, side="asset"):
    db.add(Asset(asset_name=name, asset_item_id=item_id, amount=_dec(amount),
                 side=side, asset_date=d))
    db.commit()


def _check_identity(res):
    """恒等式：delta ＝ Σ段（Decimal 精确相等，定义性成立）。"""
    total = sum(Decimal(s["amount"]) for s in res["segments"])
    assert Decimal(res["delta"]) == total
    return total


# ---------------------------------------------------------------- 恒等式对拍

def _setup_mixed(db):
    """A/B 间转移＋D 外部注入：Δ730 ＝ 外 500 ＋ 转移 0 ＋ 内生 230。"""
    a = _mkitem(db, "示例-股A", 1000, D(2026, 1, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-股B", 300, D(2026, 2, 1))
    _snap(db, a, "示例-股A", 700, D(2026, 2, 1))
    _snap(db, a, "示例-股A", 800, D(2026, 3, 1))
    _snap(db, b, "示例-股B", 330, D(2026, 3, 1))
    route_create(TransferRequest(
        from_item_id=a, to_item_id=b, amount="300",
        event_date="2026-02-01", reason="示例：卖甲买乙"), db)
    d = _mkitem(db, "示例-货基D", 0, D(2026, 1, 1))
    _set(db, d, 500, D(2026, 1, 15), "示例申购")
    _snap(db, d, "示例-货基D", 500, D(2026, 1, 15))
    _snap(db, d, "示例-货基D", 600, D(2026, 3, 15))
    return a, b, d


def test_identity_decimal_exact(db):
    _setup_mixed(db)
    res = attribution_service.get_attribution(db)
    assert [s["key"] for s in res["segments"]] == ["external", "transfer", "endogenous"]
    segsum = _check_identity(res)
    assert res["net_worth_start"] == "1000.00" and res["net_worth_end"] == "1730.00"
    assert res["delta"] == "730.00" and segsum == Decimal("730.00")
    ext, tr, en = res["segments"]
    assert ext["amount"] == "500.00" and ext["inflows"] == "500.00" and ext["outflows"] == "0.00"
    assert tr["amount"] == "0.00" and tr["gross"] == "300.00"
    assert en["amount"] == "230.00"
    assert res["meta"]["basis"]["principal"] == "pc-1"


def test_transfer_segment_item_level_net(db):
    a, b, _ = _setup_mixed(db)
    rb = attribution_service.get_attribution(db, scope="item", item_id=b)
    assert rb["delta"] == "330.00"                      # 无期初在场→0 起
    assert rb["segments"][1]["amount"] == "300.00"      # item 层净额≠0 正常
    assert rb["segments"][1]["gross"] == "300.00"
    assert rb["segments"][2]["amount"] == "30.00"
    _check_identity(rb)
    ra = attribution_service.get_attribution(db, scope="item", item_id=a)
    assert ra["delta"] == "-200.00"
    assert ra["segments"][1]["amount"] == "-300.00"
    assert ra["segments"][2]["amount"] == "100.00"      # 残差吸收市值效应
    _check_identity(ra)


def test_transfer_degenerate_without_23_data(db):
    """无转移数据时 transfer 段自然为 0（additive 前向兼容）。"""
    x = _mkitem(db, "示例-单基", 100, D(2026, 1, 1))
    _set(db, x, 100, D(2026, 1, 1))
    _snap(db, x, "示例-单基", 150, D(2026, 3, 1))
    res = attribution_service.get_attribution(db)
    tr = res["segments"][1]
    assert tr["amount"] == "0.00" and tr["gross"] == "0.00"
    assert res["delta"] == "50.00" and res["segments"][2]["amount"] == "50.00"
    _check_identity(res)


# ---------------------------------------------------------------- 借贷场景

def test_borrowing_scenario_net_zero_side_signed(db):
    """现金+X／负债口径+X：Δ＝0、外部净投入＝0（side 定号锁定）；借贷＝现金注入＋负债增加。"""
    cash = _mkitem(db, "示例-现金", 0, D(2026, 1, 1))
    loan = _mkitem(db, "示例-借款", 0, D(2026, 1, 1), side="liability")
    _set(db, cash, 500, D(2026, 1, 10), "示例：入账借款")
    _snap(db, cash, "示例-现金", 500, D(2026, 1, 10))
    _set(db, loan, 500, D(2026, 1, 10), "示例：确认未偿本金")
    _snap(db, loan, "示例-借款", 500, D(2026, 1, 10), side="liability")
    res = attribution_service.get_attribution(db)
    assert res["net_worth_end"] == "0.00" and res["delta"] == "0.00"
    ext = res["segments"][0]
    assert ext["amount"] == "0.00"
    assert ext["inflows"] == "500.00" and ext["outflows"] == "-500.00"
    _check_identity(res)


# ---------------------------------------------------------------- 窗口边界

def test_window_half_open_interval(db):
    """(start, end]：恰在 start 当日的流不计（已含于始值）、恰在 end 当日的流计入。"""
    x = _mkitem(db, "示例-边", 0, D(2026, 1, 1))
    _set(db, x, 400, D(2026, 2, 1), "示例：start 当日注入")
    _snap(db, x, "示例-边", 400, D(2026, 2, 1))
    _set(db, x, 1100, D(2026, 3, 1), "示例：end 当日注入")     # Δ＝700
    _snap(db, x, "示例-边", 1100, D(2026, 3, 1))
    res = attribution_service.get_attribution(db, date_from=D(2026, 2, 1),
                                              date_to=D(2026, 3, 1))
    assert res["date_from"] == "2026-02-01" and res["date_to"] == "2026-03-01"
    assert res["net_worth_start"] == "400.00" and res["net_worth_end"] == "1100.00"
    assert res["delta"] == "700.00"
    assert res["segments"][0]["amount"] == "700.00"           # 400 不计、700 计入
    assert res["segments"][2]["amount"] == "0.00"
    _check_identity(res)


# ---------------------------------------------------------------- 覆盖率/降级

def test_coverage_excludes_archived_items_from_denominator(db):
    """归档项自生效日起不在场：不进覆盖率分母，也不进 uncovered（ADR-0017 D2）。

    修复前该段的遍历只看有效行、不看资产项状态，已退场的归档项仍被算作「在场但
    未登记本金口径」，既把 ratio 拉低，又在 uncovered_items 里点名一个已经不在场的项。
    """
    alive = _mkitem(db, "示例-在场", 1000, D(2026, 1, 1))
    _set(db, alive, 1000, D(2026, 1, 1))
    gone = _mkitem(db, "示例-已归档", 500, D(2026, 1, 1))   # 刻意不登记本金口径
    item = db.get(AssetItem, gone)
    item.status = "archived"
    item.archived_effective_from = D(2026, 1, 1)
    db.commit()

    res = attribution_service.get_attribution(db)
    assert res["coverage"]["principal_set_ratio"] == "1.0000"
    assert res["coverage"]["uncovered_items"] == []
    assert "PRINCIPAL_NOT_SET" not in [d["code"] for d in res["meta"]["degradations"]]


def test_coverage_principal_not_set(db):
    _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 200, D(2026, 1, 1))
    _set(db, b, 200, D(2026, 1, 1))
    _mkitem(db, "示例-丙", 300, D(2026, 1, 1))
    c2 = _mkitem(db, "示例-丁", 400, D(2026, 1, 1))
    _set(db, c2, 400, D(2026, 1, 1))
    res = attribution_service.get_attribution(db)
    assert res["coverage"]["principal_set_ratio"] == "0.5000"
    assert res["coverage"]["uncovered_items"] == ["示例-甲", "示例-丙"]
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert "PRINCIPAL_NOT_SET" in codes
    pns = next(d for d in res["meta"]["degradations"] if d["code"] == "PRINCIPAL_NOT_SET")
    assert "示例-甲" in pns["scope"]
    assert any("残差" in a for a in res["assumptions"])


def test_empty_db_no_data(db):
    res = attribution_service.get_attribution(db)
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert codes.count("NO_DATA") == 1                       # 显式一条，不双挂
    assert res["delta"] == "0.00" and res["net_worth_start_date"] is None
    _check_identity(res)


def test_carry_forward_actual_dates_echo(db):
    """始末值缺当日行 → 回显实际取值快照日（carry-forward 时滞显式化）。"""
    x = _mkitem(db, "示例-滞", 1000, D(2026, 1, 1))
    _snap(db, x, "示例-滞", 1200, D(2026, 3, 1))
    res = attribution_service.get_attribution(
        db, date_from=D(2026, 2, 10), date_to=D(2026, 2, 20))
    assert res["net_worth_start_date"] == "2026-01-01"        # 2/10 无行→回连 1/1
    assert res["net_worth_end_date"] == "2026-01-01"          # 2/20 同样滞后
    assert res["delta"] == "0.00"
    res2 = attribution_service.get_attribution(
        db, date_from=D(2026, 2, 10), date_to=D(2026, 3, 15))
    assert res2["net_worth_end_date"] == "2026-03-01"
    assert res2["delta"] == "200.00"                          # 纯内生（残差吸收）
    assert res2["segments"][2]["amount"] == "200.00"


# ---------------------------------------------------------------- 只读零变更

def _row_counts(db):
    existing = {r[0] for r in db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'")).all()}
    out = {}
    for t in ("assets", "principal_changes", "transfer_events", "audit_log",
              "idempotency_keys"):            # 后者为迁移专有表，夹具库可缺→跳过
        if t in existing:
            out[t] = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
    return out


def test_attribution_readonly_zero_change(db):
    _setup_mixed(db)
    before = _row_counts(db)
    for scope, iid in (("portfolio", None), ("item", 1)):
        analysis_service_get = attribution_service.get_attribution(
            db, scope=scope, item_id=iid)
        assert analysis_service_get["segments"]
    assert _row_counts(db) == before                         # 零写入（含审计/幂等表）


# ---------------------------------------------------------------- 端点信封

def _call(db, **kw):
    base = dict(scope="portfolio", id=None, period=None, period_type="month",
                date_from=None, date_to=None)
    base.update(kw)
    return route_attribution(db=db, **base)


def test_route_validation_envelope(db):
    with pytest.raises(ApiError) as ei:
        _call(db, scope="bogus")
    assert ei.value.code == ErrorCode.VALIDATION_ERROR and ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "scope"
    with pytest.raises(ApiError) as ei:
        _call(db, scope="item")
    assert any(f["field"] == "id" for f in ei.value.details["fields"])
    with pytest.raises(ApiError) as ei:
        _call(db, date_from=D(2026, 5, 1), date_to=D(2026, 1, 1))
    assert ei.value.details["fields"][0]["field"] == "date_from"
    with pytest.raises(ApiError) as ei:
        _call(db, scope="item", id=9999)
    assert ei.value.code == ErrorCode.NOT_FOUND
    x = _mkitem(db, "示例-端", 100, D(2026, 1, 1))
    _set(db, x, 100, D(2026, 1, 1))
    out = _call(db, period="2026-01", period_type="month")
    assert out["date_from"] == "2026-01-01" and out["date_to"] == "2026-01-31"
    assert out["delta"] == "0.00"
