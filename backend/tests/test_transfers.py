"""v5 二期·子步 2.3「转移事件」测试（spec §2.3 测试矩阵）。

等额不变量（同事务两条口径变更，两侧本金等额、净值不变）、登记日即快照日
409 SNAPSHOT_REQUIRED、组合级 TWR 排除内部转移（关键回归）＋item 级视转移为
现金流、反向更正（追加不改史）、transfer_events append-only 触发器、0013 双跑
幂等、列表筛选、审计 transfer_register 单点、参数校验。数据一律合成（「示例-」）。
"""
import importlib.util
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import Asset, AuditLog, PrincipalChange, TransferEvent
from routers.asset_items import PrincipalRequest, set_principal as route_set_principal
from routers.transfer_events import (
    TransferRequest, create_transfer as route_create,
    list_transfers as route_list,
)
from services import analysis_service, asset_service, principal_service

D = date


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _set(db, item_id, amount, eff, reason="期初投入"):
    return route_set_principal(item_id, PrincipalRequest(
        amount=str(amount), effective_date=eff.isoformat(), reason=reason), db)


def _mkitem(db, name, amount, d):
    a = asset_service.create_asset(db, name, _dec(amount), d)
    return a.asset_item_id


def _snap(db, item_id, name, amount, d):
    db.add(Asset(asset_name=name, asset_item_id=item_id, amount=_dec(amount),
                 side="asset", asset_date=d))
    db.commit()


def _xfer(db, src, dst, amt, d, reason="示例：卖甲买乙"):
    return route_create(TransferRequest(
        from_item_id=src, to_item_id=dst, amount=str(amt),
        event_date=d.isoformat(), reason=reason), db)


# ---------------------------------------------------------------- 等额不变量

def test_transfer_equal_amount_invariant(db):
    """同事务两条 principal_changes：from −amt / to +amt，净值不变、origin/回填齐。"""
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 100, D(2026, 2, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    _snap(db, a, "示例-甲", 1000, D(2026, 2, 1))      # 登记日快照前置（R-34 注）
    out = _xfer(db, a, b, 300, D(2026, 2, 1))
    ev_id = out["event"]["id"]

    rows = db.execute(select(PrincipalChange)
                      .where(PrincipalChange.origin == "transfer")
                      .order_by(PrincipalChange.asset_item_id)).scalars().all()
    assert len(rows) == 2
    assert {r.transfer_event_id for r in rows} == {ev_id}
    assert {r.effective_date for r in rows} == {D(2026, 2, 1)}
    assert all(r.reason == f"内部买卖转移 #{ev_id}" for r in rows)
    assert rows[0].asset_item_id == a and rows[1].asset_item_id == b
    assert rows[0].new_amount == _dec(700)          # 1000 − 300
    assert rows[1].new_amount == _dec(300)          # 未设视作 0 起链：0 + 300
    # 等额不变量：两侧合计移动＝0（净值不变、不产生流水）
    assert (rows[0].new_amount - _dec(1000)) + (rows[1].new_amount - _dec(0)) == _dec(0)
    assert principal_service.resolve_cost_basis(db, a, D(2026, 2, 1)) == _dec(700)
    assert principal_service.resolve_cost_basis(db, b, D(2026, 2, 1)) == _dec(300)


def test_transfer_same_date_chain_sees_latest_cumulative(db):
    """同日两笔转移：第二笔的累计基准必须看到第一笔（纯历史链，非行定格值）。"""
    a = _mkitem(db, "示例-丙", 1000, D(2026, 2, 1))
    b = _mkitem(db, "示例-丁", 0, D(2026, 2, 1))
    e = _mkitem(db, "示例-戊", 0, D(2026, 2, 1))
    _set(db, a, 1000, D(2026, 2, 1))
    _xfer(db, a, b, 300, D(2026, 2, 1), "示例转仓一")
    _xfer(db, a, e, 200, D(2026, 2, 1), "示例转仓二")
    assert principal_service.history_basis(db, a, D(2026, 2, 1)) == _dec(500)
    assert principal_service.current_principal(db, b) == _dec(300)
    assert principal_service.current_principal(db, e) == _dec(200)


# ---------------------------------------------------------------- 快照日校验

def test_transfer_requires_snapshot_both_sides(db):
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 100, D(2026, 1, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:
        _xfer(db, a, b, 300, D(2026, 3, 1))
    err = ei.value
    assert err.status_code == 409 and err.code == ErrorCode.SNAPSHOT_REQUIRED
    missing = {m["item_id"] for m in err.details["missing_items"]}
    assert missing == {a, b} and err.details["hint"]
    # 补甲侧 → 仍缺乙侧；补齐 → 登记成功（可重试语义）
    _snap(db, a, "示例-甲", 900, D(2026, 3, 1))
    with pytest.raises(ApiError) as ei2:
        _xfer(db, a, b, 300, D(2026, 3, 1))
    assert {m["item_id"] for m in ei2.value.details["missing_items"]} == {b}
    _snap(db, b, "示例-乙", 80, D(2026, 3, 1))
    out = _xfer(db, a, b, 300, D(2026, 3, 1))
    assert out["event"]["amount"] == "300.00"


# ---------------------------------------------------------------- 计算衔接（关键回归）

def _setup_transfer_returns(db):
    """A：外部 1000@1/1，转仓 −300@2/1（快照 700），3/1 市值 800；B：300@2/1→330@3/1。"""
    a = _mkitem(db, "示例-股A", 1000, D(2026, 1, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-股B", 300, D(2026, 2, 1))
    _snap(db, a, "示例-股A", 700, D(2026, 2, 1))
    _snap(db, a, "示例-股A", 800, D(2026, 3, 1))
    _snap(db, b, "示例-股B", 330, D(2026, 3, 1))
    _xfer(db, a, b, 300, D(2026, 2, 1))
    return a, b


def test_portfolio_twr_excludes_internal_transfer(db):
    """组合级：内部转移不产生外部流——期初 1000→期末 1130，段中无流（测试锁定漏过滤风险）。"""
    _setup_transfer_returns(db)
    res = analysis_service.get_returns(db)
    subs = res["twr"]["period"]["subperiods"]
    assert all(s["flows"] == [] for s in subs)         # 无任何转移流混入
    assert res["twr"]["period"]["period_return"] == "0.130000"


def test_item_level_twr_counts_transfer_as_flow(db):
    """item 级：转仓对该持仓即现金流——A 段尾零权（2/1 有快照 700），B 期初流于 t0 不重复计。"""
    a, b = _setup_transfer_returns(db)
    ra = analysis_service.get_returns(db, scope="item", item_id=a)
    assert ra["twr"]["period"]["period_return"] == "0.142857"   # 段1 0 × 段2 100/700
    assert [len(s["flows"]) for s in ra["twr"]["period"]["subperiods"]] == [1, 0]
    rb = analysis_service.get_returns(db, scope="item", item_id=b)
    assert rb["twr"]["period"]["period_return"] == "0.100000"   # 300→330


# ---------------------------------------------------------------- 更正＝反向转移

def test_transfer_reverse_correction_append_only(db):
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 100, D(2026, 2, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    _snap(db, a, "示例-甲", 1000, D(2026, 2, 1))
    first = _xfer(db, a, b, 300, D(2026, 2, 1))["event"]
    _snap(db, a, "示例-甲", 700, D(2026, 3, 1))
    _snap(db, b, "示例-乙", 300, D(2026, 3, 1))
    second = _xfer(db, b, a, 300, D(2026, 3, 1), "示例：更正——转回")["event"]
    # 追加不改史：两事件、四条转移口径行俱在；净额复原
    assert db.query(TransferEvent).count() == 2
    tr = db.execute(select(PrincipalChange)
                    .where(PrincipalChange.origin == "transfer")).scalars().all()
    assert len(tr) == 4
    assert principal_service.current_principal(db, a) == _dec(1000)
    assert principal_service.current_principal(db, b) == _dec(0)
    assert second["id"] != first["id"]
    # 反向转移同样落 transfer_event_id（可追两条事件链路）
    assert {r.transfer_event_id for r in tr} == {first["id"], second["id"]}


# ---------------------------------------------------------------- 触发器 / 0013

def _load_0013():
    from services.db_migration_service import _MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m0013", f"{_MIGRATIONS_DIR}/0013_transfer_events.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _stub_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE asset_items (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY)")
    return conn


def test_0013_double_run_idempotent(tmp_path):
    m = _load_0013()
    conn = _stub_conn(tmp_path)
    for _ in range(2):                      # 双跑：表/索引/触发器全守卫
        conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    objs = conn.execute("SELECT type, name FROM sqlite_master "
                        "WHERE name LIKE 'transfer_events%' OR name LIKE 'ix_transfer%'"
                        ).fetchall()
    # 表＋2 索引＋2 触发器＝5 个对象，双跑不倍增（守卫幂等）
    assert len(objs) == 5
    conn.close()


def test_transfer_events_append_only_and_positive(tmp_path):
    m = _load_0013()
    conn = _stub_conn(tmp_path)
    conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    conn.execute("INSERT INTO transfer_events (from_item_id, to_item_id, amount,"
                 " event_date, reason, created_at)"
                 " VALUES (1, 2, 100, '2026-02-01', '示例转仓', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transfer_events SET amount=9 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM transfer_events WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):   # 恒正 CHECK：方向由 from/to 表达
        conn.execute("INSERT INTO transfer_events (from_item_id, to_item_id, amount,"
                     " event_date, reason, created_at)"
                     " VALUES (2, 1, -50, '2026-02-01', '示例负额', 't')")
    conn.close()


# ---------------------------------------------------------------- 列表 / 审计 / 校验

def test_list_transfers_filters(db):
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 100, D(2026, 2, 1))
    c = _mkitem(db, "示例-丙", 50, D(2026, 3, 1))
    _snap(db, a, "示例-甲", 900, D(2026, 2, 1))
    _snap(db, a, "示例-甲", 800, D(2026, 3, 1))
    _xfer(db, a, b, 100, D(2026, 2, 1))
    _xfer(db, a, c, 100, D(2026, 3, 1), "示例：卖甲买丙")
    full = route_list(db=db)["items"]
    assert [t["id"] for t in full] == [1, 2]
    assert full[0]["from_name"] == "示例-甲" and full[0]["to_name"] == "示例-乙"
    assert route_list(item_id=c, db=db)["items"][0]["id"] == 2   # 任一侧命中
    assert route_list(item_id=b, date_from=D(2026, 3, 1), db=db)["items"] == []
    assert len(route_list(date_to=D(2026, 2, 28), db=db)["items"]) == 1


def test_transfer_audit_single_point(db):
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    b = _mkitem(db, "示例-乙", 100, D(2026, 2, 1))
    _snap(db, a, "示例-甲", 1000, D(2026, 2, 1))
    out = _xfer(db, a, b, 300, D(2026, 2, 1))
    rows = db.execute(select(AuditLog).where(
        AuditLog.entity == "transfer_event")).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "transfer_register"
    assert rows[0].row_id == out["event"]["id"]
    assert out["change"]["before"] is None            # 登记＝新建
    assert '"amount"' in (rows[0].after_json or "")  # 审计 after 携两侧与金额


def test_transfer_param_validation(db):
    a = _mkitem(db, "示例-甲", 1000, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:
        _xfer(db, a, a, 100, D(2026, 1, 1))
    assert ei.value.code == ErrorCode.BAD_REQUEST     # from==to
    with pytest.raises(ApiError) as ei:
        _xfer(db, a, 9999, 100, D(2026, 1, 1))
    assert ei.value.code == ErrorCode.NOT_FOUND
    b = _mkitem(db, "示例-乙", 100, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:
        _xfer(db, a, b, "-100", D(2026, 1, 1))
    assert ei.value.code == ErrorCode.BAD_REQUEST     # 恒正
    with pytest.raises(ApiError) as ei:
        _xfer(db, a, b, 100, D(2026, 1, 1), reason="  ")
    assert ei.value.code == ErrorCode.PRINCIPAL_REASON_REQUIRED
