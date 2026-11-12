"""v5 二期·子步 2.4「血缘链接」测试（spec §2.4 测试矩阵）。

三类型用例（revision/split/merge）、承接额（split ratio、merge 多旧项、人确认值）、
归档联动（旧项 archived＋新项口径生效）、目标名占用、改名不走血缘（rename 拒绝）、
链不可变（0014 触发器 ABORT＋link_type CHECK）、0014 双跑幂等、组合收益不受影响
（回归：登记前后 returns 三率全等＋无 lineage 流）、前身/后继双向查询、审计
lineage_link＋伴生 principal_set＋archive。数据一律合成（「示例-」）。
"""
import importlib.util
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from errors import ApiError, ErrorCode
from models import Asset, AssetItem, AuditLog, PrincipalChange
from routers.asset_items import PrincipalRequest, set_principal as route_set_principal
from routers.lineage_links import LineageRequest, create_lineage as route_create
from routers.lineage_links import list_lineage as route_list
from services import analysis_service, asset_service, attribution_service

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


def _lin(db, from_ids, to_name, ltype, principal, reason="示例：改版承接",
         ratio=None, eff=None):
    return route_create(LineageRequest(
        from_item_ids=from_ids, to_item_name=to_name, link_type=ltype,
        split_ratio=ratio, principal=principal,
        effective_date=eff.isoformat() if eff else None,
        reason=reason), db)


# ---------------------------------------------------------------- revision

def test_revision_happy(db):
    old = _mkitem(db, "示例-旧券", 1000, D(2026, 1, 1))
    _set(db, old, 1000, D(2026, 1, 1))
    out = _lin(db, [old], "示例-新券", "revision", "1000", eff=D(2026, 2, 1))

    assert db.get(AssetItem, old).status == "archived"          # 归档联动
    link = out["link"]
    assert link["link_type"] == "revision" and link["split_ratio"] is None
    assert link["from_item_id"] == old
    new_id = link["to_item_id"]
    assert new_id != old
    assert db.get(AssetItem, new_id).display_name == "示例-新券"
    assert db.get(AssetItem, new_id).status != "archived"
    # 承接行：origin='lineage'、reason 引链 id、值＝人确认
    row = db.get(PrincipalChange, out["entry"]["id"])
    assert row.origin == "lineage" and row.asset_item_id == new_id
    assert f"#{link['id']}" in row.reason and "revision" in row.reason
    assert row.effective_date == D(2026, 2, 1)
    assert row.transfer_event_id is None
    assert _dec(row.new_amount) == _dec(1000)
    # 审计：lineage_link＋伴生 principal_set（承接行专用）＋archive（既有单点自带）
    rows_a = db.execute(select(AuditLog)).scalars().all()
    acts = [r.action for r in rows_a]
    assert acts.count("lineage_link") == 1 and acts.count("archive") == 1
    ps = [r for r in rows_a if r.action == "principal_set"
          and r.entity == "principal_change"]
    assert len(ps) == 2                       # 期初 _set 一条＋血缘伴生一条
    assert out["entry"]["id"] in {r.row_id for r in ps}


# ---------------------------------------------------------------- split

def test_split_ratio(db):
    old = _mkitem(db, "示例-整仓", 1000, D(2026, 1, 1))
    _set(db, old, 1000, D(2026, 1, 1))
    out = _lin(db, [old], "示例-拆出A", "split", "600", ratio="0.6",
               reason="示例：拆分承接六成", eff=D(2026, 2, 1))
    assert Decimal(out["link"]["split_ratio"]) == Decimal("0.6000")
    assert _dec(out["entry"]["amount"]) == _dec(600)
    with pytest.raises(ApiError) as ei:                         # ratio 越界
        _lin(db, [_mkitem(db, "示例-边", 1, D(2026, 1, 1))], "示例-新X", "split",
             "1", ratio="1.5")
    assert ei.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(ApiError) as ei:                         # split 缺 ratio
        _lin(db, [_mkitem(db, "示例-缺率", 1, D(2026, 1, 1))], "示例-新Y",
             "split", "1")
    assert ei.value.details["field"] == "split_ratio"


# ---------------------------------------------------------------- merge

def test_merge_multi_sources(db):
    a = _mkitem(db, "示例-基甲", 700, D(2026, 1, 1))
    b = _mkitem(db, "示例-基乙", 300, D(2026, 1, 1))
    _set(db, a, 700, D(2026, 1, 1))
    _set(db, b, 300, D(2026, 1, 1))
    out = _lin(db, [a, b], "示例-合并仓", "merge", "1000",
               reason="示例：两只合并", eff=D(2026, 2, 1))
    links = out["links"]
    assert len(links) == 2 and {lk["to_item_id"] for lk in links} == {links[0]["to_item_id"]}
    assert {lk["from_item_id"] for lk in links} == {a, b}
    assert all(lk["link_type"] == "merge" for lk in links)
    assert db.get(AssetItem, a).status == "archived"
    assert db.get(AssetItem, b).status == "archived"
    reason = out["entry"]["reason"]
    assert f"#{links[0]['id']}" in reason and f"#{links[1]['id']}" in reason
    # 前身双向：新项有两条前身
    got = route_list(item_id=links[0]["to_item_id"], db=db)
    assert len(got["predecessors"]) == 2 and got["successors"] == []


# ---------------------------------------------------------------- 守护与边界

def test_rename_not_via_lineage(db):
    """拍板 10：改名走显示名史，血缘无 rename 类型（防蔓延）。"""
    old = _mkitem(db, "示例-勿改名", 100, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:
        _lin(db, [old], "示例-任何新名", "rename", "100")
    assert ei.value.code == ErrorCode.BAD_REQUEST and ei.value.details["field"] == "link_type"


def test_guards(db):
    old = _mkitem(db, "示例-占用甲", 100, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:                          # 目标名占用
        _lin(db, [old], "示例-占用甲", "revision", "100")
    assert ei.value.details["field"] == "to_item_name"
    with pytest.raises(ApiError) as ei:                          # 旧项不存在
        _lin(db, [9999], "示例-新Z", "revision", "100")
    assert ei.value.code == ErrorCode.NOT_FOUND
    asset_service.set_item_status(db, old, "archived", reason="normalization")
    with pytest.raises(ApiError) as ei:                          # 旧项已归档
        _lin(db, [old], "示例-新W", "revision", "100")
    assert ei.value.code == ErrorCode.ITEM_ARCHIVED
    fresh = _mkitem(db, "示例-理由", 100, D(2026, 1, 1))
    with pytest.raises(ApiError) as ei:                          # reason 必填（R-9）
        _lin(db, [fresh], "示例-新V", "revision", "100", reason=" ")
    assert ei.value.code == ErrorCode.PRINCIPAL_REASON_REQUIRED
    with pytest.raises(ApiError) as ei:                          # 空旧项列表
        _lin(db, [], "示例-新U", "revision", "100", reason="示例")
    assert ei.value.details["field"] == "from_item_ids"


# ---------------------------------------------------------------- 0014 迁移

def _load_0014():
    from services.db_migration_service import _MIGRATIONS_DIR
    spec = importlib.util.spec_from_file_location(
        "m0014", f"{_MIGRATIONS_DIR}/0014_lineage_links.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _stub_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE asset_items (id INTEGER PRIMARY KEY)")
    return conn


def test_0014_double_run_idempotent(tmp_path):
    m = _load_0014()
    conn = _stub_conn(tmp_path)
    for _ in range(2):
        conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    objs = conn.execute("SELECT type, name FROM sqlite_master "
                        "WHERE name LIKE 'lineage_links%' OR name LIKE 'ix_lineage%'"
                        ).fetchall()
    assert len(objs) == 5                                       # 表＋2 索引＋2 触发器不倍增
    conn.close()


def test_lineage_links_append_only_and_type_check(tmp_path):
    m = _load_0014()
    conn = _stub_conn(tmp_path)
    conn.execute("BEGIN"); m.upgrade(conn); conn.execute("COMMIT")
    conn.execute("INSERT INTO lineage_links (from_item_id, to_item_id, link_type,"
                 " created_at) VALUES (1, 2, 'revision', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE lineage_links SET link_type='merge' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM lineage_links WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):                 # rename 不可入表
        conn.execute("INSERT INTO lineage_links (from_item_id, to_item_id,"
                     " link_type, created_at) VALUES (2, 1, 'rename', 't')")
    conn.close()


# ---------------------------------------------------------------- 收益回归（关键）

def test_lineage_does_not_distort_portfolio_returns(db):
    """登记血缘前后：组合三率全等、无 lineage 流混入（origin 过滤回归锁定）。"""
    a = _mkitem(db, "示例-单仓", 1000, D(2026, 1, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    _snap(db, a, "示例-单仓", 1100, D(2026, 3, 1))
    before = analysis_service.get_returns(db)

    _lin(db, [a], "示例-续仓", "revision", "1000", eff=D(2026, 2, 1))
    after = analysis_service.get_returns(db)
    assert after["twr"]["period"]["period_return"] == before["twr"]["period"]["period_return"]
    assert after["twr"]["annualized"] == before["twr"]["annualized"]
    assert after["xirr"] == before["xirr"]
    assert all(s["flows"] == [] for s in after["twr"]["period"]["subperiods"])


def test_lineage_excluded_from_item_and_attribution_flows(db):
    """item 级现金流与归因外部段均不含 origin='lineage' 承接。"""
    a = _mkitem(db, "示例-甲仓", 1000, D(2026, 1, 1))
    _set(db, a, 1000, D(2026, 1, 1))
    out = _lin(db, [a], "示例-乙仓", "revision", "1000", eff=D(2026, 2, 1))
    new_id = out["link"]["to_item_id"]
    _snap(db, new_id, "示例-乙仓", 1000, D(2026, 2, 1))
    _snap(db, new_id, "示例-乙仓", 1050, D(2026, 3, 1))
    # item 收益：承接行在 t0（2/1）当日→按半开排除；段中无流
    r = analysis_service.get_returns(db, scope="item", item_id=new_id)
    assert all(s["flows"] == [] for s in r["twr"]["period"]["subperiods"])
    assert r["twr"]["period"]["period_return"] == "0.050000"
    # 归因（新项窗口）：外部段不含 lineage 承接＝0；内生＝残差
    at = attribution_service.get_attribution(
        db, scope="item", item_id=new_id,
        date_from=D(2026, 2, 1), date_to=D(2026, 3, 1))
    assert at["segments"][0]["amount"] == "0.00"
    assert at["segments"][2]["amount"] == "50.00"


# ---------------------------------------------------------------- 列表/端点

def test_list_lineage_directions(db):
    a = _mkitem(db, "示例-链始", 100, D(2026, 1, 1))
    out = _lin(db, [a], "示例-链中", "revision", "100", reason="示例：一")
    mid = out["link"]["to_item_id"]
    _snap(db, mid, "示例-链中", 100, D(2026, 2, 1))
    out2 = _lin(db, [mid], "示例-链尾", "split", "60", ratio="0.6",
                reason="示例：二", eff=D(2026, 3, 1))
    tail = out2["link"]["to_item_id"]
    got_mid = route_list(item_id=mid, db=db)
    assert [p["from_name"] for p in got_mid["predecessors"]] == ["示例-链始"]
    assert [s["to_name"] for s in got_mid["successors"]] == ["示例-链尾"]
    got_all = route_list(item_id=None, db=db)["items"]
    assert [lk["id"] for lk in got_all] == [1, 2]
    empty = route_list(item_id=tail, db=db)
    assert empty["successors"] == [] and len(empty["predecessors"]) == 1


def test_route_bad_date_and_happy_shape(db):
    a = _mkitem(db, "示例-日检", 100, D(2026, 1, 1))
    with pytest.raises(ApiError):
        route_create(LineageRequest(
            from_item_ids=[a], to_item_name="示例-新日", link_type="revision",
            principal="100", effective_date="2026-13-45", reason="示例"), db)
    # 承接值非法
    with pytest.raises(ApiError) as ei:
        _lin(db, [a], "示例-新日2", "revision", "abc")
    assert ei.value.details["field"] == "principal"
    # revision 携带 ratio 拒绝
    with pytest.raises(ApiError) as ei:
        _lin(db, [a], "示例-新日3", "revision", "100", ratio="0.5")
    assert ei.value.details["field"] == "split_ratio"
