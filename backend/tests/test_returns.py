"""v5 二期·子步 2.2「TWR/XIRR 双口径收益」测试（spec §2.2 测试矩阵）。

纯函数（零 I/O）：Modified Dietz 手工算例对拍、段尾零权、几何链接、大额流标注
与 15% 阈值、期初非正/单日降级；XIRR 已知解对拍、无正负对/单点/深负根。
组装/端点：窗口解析（period/显式区间/全史）、负债不计、逐项 scope、NO_DATA、
TWR_LARGE_FLOW_APPROX／RATE_UNAVAILABLE 降级、meta.basis.principal、参数校验。
数据一律合成（「示例-」）。
"""
from datetime import date
from decimal import Decimal

import pytest

from errors import ApiError, ErrorCode
from models import Asset
from routers.analysis import returns as route_returns
from routers.asset_items import PrincipalRequest, set_principal as route_set_principal
from services import analysis_service, asset_service, returns_service

D = date


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _set(db, item_id, amount, eff, reason="期初投入"):
    return route_set_principal(item_id, PrincipalRequest(
        amount=str(amount), effective_date=eff.isoformat(), reason=reason), db)


def _snap(db, item_id, name, amount, d):
    db.add(Asset(asset_name=name, asset_item_id=item_id, amount=_dec(amount),
                 side="asset", asset_date=d))
    db.commit()


# ============================================================ 纯函数·TWR

def test_md_manual_case_single_segment():
    """手工算例：V0=1000，+100 于第 30 天，V1=1150（60 天）→ (1150−1000−100)/(1000+0.5·100)。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 3, 2), _dec(1150))],
        [(D(2026, 1, 31), _dec(100))])
    assert out["period_return"] == "0.047619"      # 50/1050
    assert len(out["subperiods"]) == 1
    assert out["subperiods"][0]["flows"] == [{"date": "2026-01-31", "amount": "100"}]
    assert out["annualized"] is not None and out["reason"] is None


def test_md_flow_on_valuation_date_zero_weight():
    """段尾（=估值日）现金流：权重 0，不计基数但计入市值剥离。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 2, 1), _dec(1100))],
        [(D(2026, 2, 1), _dec(100))])
    assert out["period_return"] == "0.000000"      # (1100−1000−100)/1000
    assert out["large_flow_approx"] is False       # 当日有重估值 → 不标近似


def test_md_geometric_linking():
    """两段各 +10% → 链接 (1.1)²−1＝0.21；无现金流。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 2, 1), _dec(1100)),
         (D(2026, 3, 2), _dec(1210))], [])
    assert [s["return"] for s in out["subperiods"]] == ["0.100000", "0.100000"]
    assert out["period_return"] == "0.210000"


def test_md_large_flow_flag_and_threshold():
    """≥15% 期初且当日无重估值 → 标记；<15% 或当日有估值 → 不标。"""
    big = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 3, 1), _dec(1600))],
        [(D(2026, 2, 1), _dec(200))])
    assert big["large_flow_approx"] is True
    assert big["large_flow_dates"] == ["2026-02-01"]
    edge = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 3, 1), _dec(1600))],
        [(D(2026, 2, 1), _dec(150))])              # 恰 15% → 含
    assert edge["large_flow_approx"] is True
    small = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 3, 1), _dec(1600))],
        [(D(2026, 2, 1), _dec(149))])              # <15% → 不含
    assert small["large_flow_approx"] is False
    valued = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 2, 1), _dec(1300)),
         (D(2026, 3, 1), _dec(1600))],
        [(D(2026, 2, 1), _dec(200))])
    assert valued["large_flow_approx"] is False


def test_md_degrade_reasons():
    """期初 ≤0／单一估值日／空序列 → rate=None＋reason（绝不编数）。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(0)), (D(2026, 3, 1), _dec(500))], [])
    assert out["period_return"] is None and "期初市值" in out["reason"]
    single = returns_service.twr_modified_dietz([(D(2026, 1, 1), _dec(1000))], [])
    assert single["annualized"] is None and single["subperiods"] == []
    empty = returns_service.twr_modified_dietz([], [])
    assert empty["period_return"] is None and empty["reason"] == "无估值序列"


def test_md_annualization_formula():
    """60 天 +4.7619% → 年化 (1+R)^(365/60)−1 ≈ 0.3271。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 3, 2), _dec(1150))],
        [(D(2026, 1, 31), _dec(100))])
    assert out["annualized"] == "0.3271"


def test_md_flow_on_intermediate_valuation_date():
    """中段估值日现金流（2.3 转移日必有快照→常踩此形）：归属**以其为段尾**的段、
    零权——段尾市值已含流，不得划入下一段重复计基（回归锁定）。"""
    out = returns_service.twr_modified_dietz(
        [(D(2026, 1, 1), _dec(1000)), (D(2026, 2, 1), _dec(1150)),
         (D(2026, 3, 1), _dec(1180))],
        [(D(2026, 2, 1), _dec(150))])
    assert [s["return"] for s in out["subperiods"]] == ["0.000000", "0.026087"]
    assert out["period_return"] == "0.026087"      # 段1 (1150−1000−150)/1000 × 段2 30/1150


# ============================================================ 纯函数·XIRR

def test_xirr_known_solution_single_year():
    """−1000@0d，+1100@365d → 精确解 0.1。"""
    r = returns_service.xirr_rate(
        [(D(2026, 1, 1), _dec(-1000)), (D(2027, 1, 1), _dec(1100))])
    assert r is not None and abs(r - 0.1) < 1e-6


def test_xirr_known_solution_two_coupons():
    """−1000@0，+550@1y，+550@2y → 二次方程正根 ≈0.065965。"""
    r = returns_service.xirr_rate(
        [(D(2026, 1, 1), _dec(-1000)), (D(2027, 1, 1), _dec(550)),
         (D(2028, 1, 1), _dec(550))])
    assert r is not None and abs(r - 0.065965) < 1e-4


def test_xirr_requires_pos_neg_pair():
    """无一正一负／不足两点 → None（降级由调用方转 RATE_UNAVAILABLE）。"""
    assert returns_service.xirr_rate(
        [(D(2026, 1, 1), _dec(-1000)), (D(2027, 1, 1), _dec(-500))]) is None
    assert returns_service.xirr_rate(
        [(D(2026, 1, 1), _dec(1000)), (D(2027, 1, 1), _dec(1500))]) is None
    assert returns_service.xirr_rate([(D(2026, 1, 1), _dec(-1000))]) is None
    assert returns_service.xirr_rate([]) is None


def test_xirr_deep_negative_root():
    """−1000@0，+100@151d → 根 ≈−0.996（二分回退域内求解）。"""
    r = returns_service.xirr_rate(
        [(D(2026, 1, 1), _dec(-1000)), (D(2026, 6, 1), _dec(100))])
    assert r is not None and abs(r - (-0.996174)) < 1e-3


# ============================================================ 组装层

def _setup_portfolio(db):
    """示例-股：市值 1000(1/1)→1600(3/1)；口径 1000(1/1)→1500(2/1) ⇒ 流 +500。"""
    a = asset_service.create_asset(db, "示例-股", _dec(1000), D(2026, 1, 1))
    iid = a.asset_item_id
    _set(db, iid, 1000, D(2026, 1, 1))
    _set(db, iid, 1500, D(2026, 2, 1), "加仓")
    _snap(db, iid, "示例-股", 1600, D(2026, 3, 1))
    return iid


def test_returns_portfolio_happy(db):
    iid = _setup_portfolio(db)
    res = analysis_service.get_returns(db)
    assert res["scope"] == "portfolio" and res["item_id"] is None
    assert res["window"]["start"] == "2026-01-01" and res["window"]["end"] == "2026-03-01"
    assert res["twr"]["period"]["period_return"] == "0.080822"   # 手工对拍：100/(1000+28/59·500)
    assert res["twr"]["annualized"] is not None
    assert len(res["twr"]["period"]["subperiods"][0]["flows"]) == 1
    assert res["xirr"] is not None
    assert len(res["assumptions"]) == 5
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert "TWR_LARGE_FLOW_APPROX" in codes                      # +500＝期初 50%，2/1 无重估值
    assert res["meta"]["basis"]["principal"] == "pc-1"


def test_returns_liability_side_not_counted(db):
    """负债项：市值不入组合序列、口径增量不作现金流。"""
    _setup_portfolio(db)
    b = asset_service.create_asset(db, "示例-贷", _dec(300), D(2026, 1, 1),
                                   side="liability")
    _set(db, b.asset_item_id, 300, D(2026, 1, 1), "借入")
    _set(db, b.asset_item_id, 350, D(2026, 2, 1), "加借")
    res = analysis_service.get_returns(db)
    # 期初组合＝资产侧 1000（不含负债 300）；负债 +50 增量被剔除（无 RATE 类意外降级）
    assert res["twr"]["period"]["period_return"] == "0.080822"
    assert res["twr"]["annualized"] is not None


def test_returns_liability_only_degrades(db):
    b = asset_service.create_asset(db, "示例-独贷", _dec(300), D(2026, 1, 1),
                                   side="liability")
    res = analysis_service.get_returns(db)
    assert res["twr"]["annualized"] is None and res["xirr"] is None
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert "RATE_UNAVAILABLE" in codes


def test_returns_item_scope_and_not_found(db):
    iid = _setup_portfolio(db)
    _snap(db, iid, "示例-股", 5000, D(2026, 4, 1))   # 另一资产混入组合口径
    res = analysis_service.get_returns(db, scope="item", item_id=iid)
    assert res["scope"] == "item" and res["item_id"] == iid
    assert res["twr"]["period"]["period_return"] is not None
    with pytest.raises(ApiError) as ei:
        analysis_service.get_returns(db, scope="item", item_id=9999)
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_returns_period_window_single_point(db):
    """period＝账期标签：窗口切至 1 月（仅 1/1 一点）→ TWR/XIRR 率不可得降级。"""
    _setup_portfolio(db)
    res = analysis_service.get_returns(db, period="2026-01", period_type="month")
    assert res["window"]["start"] == "2026-01-01" and res["window"]["end"] == "2026-01-31"
    assert res["twr"]["annualized"] is None and res["xirr"] is None
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert codes.count("RATE_UNAVAILABLE") == 2      # TWR 无有效子区间＋XIRR 时点不足


def test_returns_explicit_window(db):
    _setup_portfolio(db)
    res = analysis_service.get_returns(db, date_from=D(2026, 2, 15), date_to=D(2026, 2, 28))
    assert res["window"] == {"start": "2026-02-15", "end": "2026-02-28", "period": None}
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert "RATE_UNAVAILABLE" in codes               # 窗口内无任何快照日


def test_returns_empty_db(db):
    res = analysis_service.get_returns(db)
    codes = [d["code"] for d in res["meta"]["degradations"]]
    assert "NO_DATA" in codes
    assert res["twr"]["annualized"] is None and res["xirr"] is None


def test_returns_route_validation(db):
    with pytest.raises(ApiError) as ei:
        route_returns(scope="bogus", id=None, period=None, period_type="month",
                      date_from=None, date_to=None, db=db)
    assert ei.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(ApiError) as ei2:
        route_returns(scope="item", id=None, period=None, period_type="month",
                      date_from=None, date_to=None, db=db)
    assert ei2.value.code == ErrorCode.BAD_REQUEST
    iid = _setup_portfolio(db)
    out = route_returns(scope="portfolio", id=None, period=None, period_type="month",
                        date_from=None, date_to=None, db=db)
    assert out["twr"]["annualized"] is not None      # 合法参数直调透传
