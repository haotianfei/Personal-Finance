"""v5 二期·子步 2.1「本金口径与升值」测试（spec §2.1 测试/演练 + §3 跨子步约束）。

解析（多段/同日 id 决胜/NULL 回退/行值优先）、两分支等价性、建行冗余、历史定格、
追加更正、append-only 触发器、写端点（reason 必填/归档拒绝/审计 principal_set→
principal_change）、降级（RATE_UNAVAILABLE/PRINCIPAL_NOT_SET）、meta.basis.principal、
0012 双跑幂等、存量不回填。数据一律合成。
"""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from errors import ApiError, ErrorCode
from models import Asset, AuditLog, PrincipalChange
from routers.analysis import appreciation as route_appreciation
from routers.asset_items import get_principal as route_get_principal
from routers.asset_items import set_principal as route_set_principal
from routers.asset_items import PrincipalRequest
from services import asset_service, principal_service

D1 = date(2026, 1, 15)
D2 = date(2026, 2, 15)


def _set(db, item_id, amount, eff, reason="期初投入"):
    return route_set_principal(item_id, PrincipalRequest(
        amount=str(amount), effective_date=eff.isoformat(), reason=reason), db)


# ---------------------------------------------------------------- 解析函数

def test_resolve_multi_segment_history(db):
    a = asset_service.create_asset(db, "示例-基金", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 1000, date(2026, 1, 1))
    _set(db, iid, 1500, date(2026, 2, 1))
    _set(db, iid, 1200, date(2026, 3, 1))
    assert principal_service.resolve_cost_basis(db, iid, date(2026, 1, 15)) == Decimal("1000")
    assert principal_service.resolve_cost_basis(db, iid, date(2026, 2, 15)) == Decimal("1500")
    assert principal_service.resolve_cost_basis(db, iid, date(2026, 3, 5)) == Decimal("1200")
    # 早于首段 → 未设（None）
    assert principal_service.resolve_cost_basis(db, iid, date(2025, 12, 31)) is None


def test_resolve_same_date_id_tiebreak(db):
    """同日两条：后登记（id 大）生效——更正＝追加的解析基础。"""
    a = asset_service.create_asset(db, "示例-同日", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 1000, D1, "初次")
    _set(db, iid, 1100, D1, "更正：录错了")
    assert principal_service.resolve_cost_basis(db, iid, D1) == Decimal("1100")
    assert len(principal_service.principal_history(db, iid)) == 2   # 追加不改史


def test_resolve_signed_and_zero(db):
    a = asset_service.create_asset(db, "示例-净撤出", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 500, D1)
    _set(db, iid, -200, D2, "净撤出")   # 带符号：可为负
    assert principal_service.resolve_cost_basis(db, iid, D2) == Decimal("-200")


def test_row_value_priority_and_null_fallback_equivalence(db):
    """等价性锁定：同一生效序列下，行值优先分支与 NULL 回退分支结果一致。"""
    a = asset_service.create_asset(db, "示例-等价", 100, D1)      # 口径未设 → 行 NULL
    iid = a.asset_item_id
    _set(db, iid, 800, date(2026, 1, 1))                          # 回填式登记（早于快照日）
    # 存量行 cost_basis 保持 NULL（建行时无口径）→ 读端回退史
    row = db.get(Asset, a.id)
    assert row.cost_basis is None
    assert principal_service.resolve_cost_basis(db, iid, D1) == Decimal("800")
    # 新行（口径已设）→ 建行冗余写入行值
    b = asset_service.create_asset(db, "示例-等价", 900, D2)
    assert b.cost_basis == Decimal("800")
    # 两分支同日解析一致（a 走历史、b 走行值）
    assert (principal_service.resolve_cost_basis(db, iid, D1)
            == principal_service._resolve_from_history(db, iid, D1))
    # 行值定格语义：其后追加新口径不改写既有行 cost_basis
    _set(db, iid, 1600, D2, "加仓")
    assert db.get(Asset, b.id).cost_basis == Decimal("800")   # 行值＝写入时点定格
    assert principal_service.resolve_cost_basis(db, iid, D2) == Decimal("800")  # 行值优先


def test_ledger_write_points_redundant_basis(db):
    """create/correct 写单点冗余：口径已设后建行/更正行带 cost_basis。"""
    a = asset_service.create_asset(db, "示例-冗余", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 400, date(2026, 1, 1))
    new, _ = asset_service.correct_asset(db, a.id, "120",
        reason="correction")
    assert new.cost_basis == Decimal("400")
    b = asset_service.create_asset(db, "示例-冗余", 500, D2)
    assert b.cost_basis == Decimal("400")


def test_upsert_write_point_redundant_basis(db):
    """_upsert_asset（截图采纳）同样冗余写入当日生效口径。"""
    from services.screenshot_service import _upsert_asset
    from decimal import Decimal as D
    a = asset_service.create_asset(db, "示例-截图项", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 300, date(2026, 1, 1))
    status, row, _before = _upsert_asset(db, "示例-截图项", D("777"), D2, None)
    assert status == "created"
    assert row.cost_basis == Decimal("300")


# ---------------------------------------------------------------- 历史定格

def test_historical_rate_frozen_after_basis_change(db):
    a = asset_service.create_asset(db, "示例-定格", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 500, date(2026, 1, 1))
    # 新快照行：建行时冗余生效口径 500（行值＝写入时点定格）
    snap = asset_service.create_asset(db, "示例-定格", 600, D2)
    assert snap.cost_basis == Decimal("500.00")
    out1 = route_appreciation(as_of=D2, db=db)
    item1 = [i for i in out1["items"] if i["item_id"] == iid][0]
    assert item1["appreciation"] == "100.00" and item1["rate"] == "0.2000"
    # 追加变更口径为 400（D2 生效）→ 行值优先定格：历史 as_of 结果不变（R-36 爆炸半径控制）
    _set(db, iid, 400, D2, "更正/加仓后重定口径")
    out2 = route_appreciation(as_of=D2, db=db)
    item2 = [i for i in out2["items"] if i["item_id"] == iid][0]
    assert item2["rate"] == "0.2000" and item2["cost_basis"] == "500.00"
    # 变更后新行（D3）取新口径 400
    snap3 = asset_service.create_asset(db, "示例-定格", 500, date(2026, 3, 15))
    assert snap3.cost_basis == Decimal("400.00")
    out3 = route_appreciation(as_of=date(2026, 3, 15), db=db)
    item3 = [i for i in out3["items"] if i["item_id"] == iid][0]
    assert item3["appreciation"] == "100.00" and item3["rate"] == "0.2500"


# ---------------------------------------------------------------- 端点/审计

def test_principal_requires_reason(db):
    a = asset_service.create_asset(db, "示例-理由", 1, D1)
    with pytest.raises(ApiError) as ei:
        _set(db, a.asset_item_id, 100, D1, reason="  ")
    assert ei.value.status_code == 400
    assert ei.value.code == ErrorCode.PRINCIPAL_REASON_REQUIRED


def test_principal_archived_item_rejected(db):
    a = asset_service.create_asset(db, "示例-归档口径", 1, D1)
    asset_service.set_item_status(db, a.asset_item_id, "archived",
                                  reason="normalization")
    with pytest.raises(ApiError) as ei:
        _set(db, a.asset_item_id, 100, D1)
    assert ei.value.status_code == 409
    assert ei.value.code == ErrorCode.ITEM_ARCHIVED


def test_principal_audit_set_then_change_with_before_after(db):
    a = asset_service.create_asset(db, "示例-审计口径", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 1000, D1)
    _set(db, iid, 1300, D2, "加仓")
    rows = db.execute(select(AuditLog).where(
        AuditLog.entity == "principal_change").order_by(AuditLog.id)).scalars().all()
    assert [r.action for r in rows] == ["principal_set", "principal_change"]
    import json
    assert rows[0].before_json is None                                   # 首次＝创建语义
    assert json.loads(rows[1].before_json) == "1000.00"                 # before=旧生效值
    assert json.loads(rows[1].after_json) == "1300.00"


def test_get_principal_history_and_current(db):
    a = asset_service.create_asset(db, "示例-史查询", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 700, D1)
    out = route_get_principal(iid, db)
    assert out["current"] == "700.00"
    assert out["history"][0]["origin"] == "external"
    assert out["history"][0]["reason"] == "期初投入"


# ---------------------------------------------------------------- 降级/组合

def test_appreciation_carry_forward_r2(db):
    """R-2 回溯口径：as_of 当日无行的项以更早最新行在场，不整支跌落。"""
    a = asset_service.create_asset(db, "示例-在场", 600, D2)
    b = asset_service.create_asset(db, "示例-缺席", 300, D1)      # 仅有早于 as_of 的行
    iid_a, iid_b = a.asset_item_id, b.asset_item_id
    _set(db, iid_a, 500, date(2026, 1, 1))
    _set(db, iid_b, 200, date(2026, 1, 1))
    out = route_appreciation(as_of=D2, db=db)
    rows = {i["display_name"]: i for i in out["items"]}
    assert set(rows) == {"示例-在场", "示例-缺席"}                 # B 不跌落
    assert rows["示例-缺席"]["market"] == "300.00"                # 以更早行计入
    assert rows["示例-缺席"]["cost_basis"] == "200.00"            # 口径仍取 as_of 生效值（R-36）
    assert rows["示例-缺席"]["appreciation"] == "100.00"
    p = out["portfolio"]
    assert p["market_total"] == "900.00"                          # 组合合计含 B
    assert p["basis_total"] == "700.00" and p["appreciation_total"] == "200.00"
    # 组合＝逐项之和一致性
    assert sum(Decimal(i["appreciation"]) for i in out["items"]
               if i["appreciation"] is not None) == Decimal(p["appreciation_total"])
    # 早于所有行的 as_of → 空表（无 ≤as_of 行的项不出现，同现状语义）
    assert route_appreciation(as_of=date(2025, 12, 31), db=db)["items"] == []


def test_appreciation_carry_forward_counts_only_active_row(db):
    """回溯行上发生更正：同日仍仅 1 条 active 入表（superseded 旧行不双计）。"""
    a = asset_service.create_asset(db, "示例-更正回溯", 100, D1)
    iid = a.asset_item_id
    _set(db, iid, 100, date(2026, 1, 1))
    asset_service.correct_asset(db, a.id, "150",
        reason="correction")                 # 同日追加更正
    out = route_appreciation(as_of=D2, db=db)                    # D2 无行 → 回溯 D1
    row = [i for i in out["items"] if i["item_id"] == iid][0]
    assert row["market"] == "150.00"                             # 只取 active 新行
    assert len([i for i in out["items"] if i["item_id"] == iid]) == 1
    assert out["portfolio"]["market_total"] == "150.00"


def test_appreciation_portfolio_and_degradations(db):
    a = asset_service.create_asset(db, "示例-有口径", 600, D2)
    b = asset_service.create_asset(db, "示例-零口径", 400, D2)
    c = asset_service.create_asset(db, "示例-无口径", 100, D2)
    _set(db, a.asset_item_id, 500, date(2026, 1, 1))
    _set(db, b.asset_item_id, 0, date(2026, 1, 1), "零口径（边界）")   # ≤0 → 不算率
    # c 不登记口径 → PRINCIPAL_NOT_SET（组合级）＋逐项 RATE_UNAVAILABLE
    out = route_appreciation(as_of=None, db=db)
    assert out["as_of"] == "2026-02-15"
    p = out["portfolio"]
    assert p["market_total"] == "1100.00"        # 资产侧市值合计（a+b+c）
    assert p["basis_total"] == "500.00"          # 仅口径>0 项入合计
    assert p["appreciation_total"] == "100.00"
    assert p["rate"] == "0.2000"
    assert p["items_missing_principal"] == ["示例-无口径"]
    dcodes = {(d["code"], d["scope"]) for d in out["meta"]["degradations"]}
    assert ("PRINCIPAL_NOT_SET", "示例-无口径") in dcodes
    rows = {i["display_name"]: i for i in out["items"]}
    assert rows["示例-有口径"]["rate"] == "0.2000"
    assert rows["示例-有口径"]["appreciation"] == "100.00"
    assert rows["示例-零口径"]["rate"] is None                       # ≤0 不算率
    assert rows["示例-零口径"]["appreciation"] == "400.00"           # market-0 可算
    assert rows["示例-零口径"]["degradations"][0]["code"] == "RATE_UNAVAILABLE"
    assert rows["示例-无口径"]["cost_basis"] is None
    assert rows["示例-无口径"]["degradations"][0]["code"] == "RATE_UNAVAILABLE"


def test_appreciation_meta_basis_version(db):
    a = asset_service.create_asset(db, "示例-meta", 100, D1)
    out = route_appreciation(as_of=D1, db=db)
    assert out["meta"]["basis"]["principal"] == "pc-1"
    assert out["meta"]["basis"]["identity"] == "asset_item"


def test_appreciation_empty_db_no_data(db):
    out = route_appreciation(as_of=None, db=db)
    assert out["as_of"] is None and out["items"] == []
    assert any(d["code"] == "NO_DATA" for d in out["meta"]["degradations"])


# ---------------------------------------------------------------- 0012/触发器

def _load_0012():
    import importlib.util
    from services.db_migration_service import _MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m0012", f"{_MIGRATIONS_DIR}/0012_principal_basis.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_0012_double_run_idempotent(tmp_path):
    m = _load_0012()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_name TEXT)")
    conn.execute("INSERT INTO assets (id, asset_name) VALUES (1, '示例-旧行')")
    conn.execute("CREATE TABLE asset_items (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY)")
    for _ in range(2):                      # 双跑：表/索引/触发器/ALTER 全守卫
        conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    # 存量行不回填：cost_basis 保持 NULL
    row = conn.execute("SELECT cost_basis FROM assets WHERE id=1").fetchone()
    assert row[0] is None
    conn.close()


def test_principal_changes_append_only_triggers(tmp_path):
    m = _load_0012()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE asset_items (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY)")
    conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    conn.execute("INSERT INTO principal_changes (asset_item_id, new_amount, effective_date,"
                 " reason, origin, created_at) VALUES (1, 100, '2026-01-01', 'r', 'external', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE principal_changes SET new_amount=9 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM principal_changes WHERE id=1")
    assert conn.execute("SELECT COUNT(*) FROM principal_changes").fetchone()[0] == 1
    conn.close()
