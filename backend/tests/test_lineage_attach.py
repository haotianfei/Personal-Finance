"""既有项之间补登记血缘 + 身份族读数（`.scratch/lineage-attach/spec.md` §1）。

覆盖：revision/merge/split 三种补登记的「零迁移零数值变化」保证、不写
`principal_changes`、归档生效日；双向传递闭包（A→B→C）与成环拒绝；分项趋势
族合计在切换日连续、`self` 与 `family=false` 行为；校验矩阵全部预检失败且
零写入；`asset_name` 旧结构向后兼容。数据一律合成（「示例-」、虚构金额/日期）。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database import get_db
from errors import ApiError, ErrorCode
from models import Asset, AssetItem, AuditLog, LineageLink, PrincipalChange
from routers import analysis as analysis_router
from routers.lineage_links import (AttachRequest, attach_lineage as route_attach,
                                   lineage_family)
from services import analysis_service, asset_service
from services.asset_service import get_or_create_item

D = date


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _mkitem(db, name) -> int:
    item = get_or_create_item(db, name)
    db.commit()
    return item.id


def _snap(db, item_id, name, amount, d):
    db.add(Asset(asset_name=name, asset_item_id=item_id, amount=_dec(amount),
                 side="asset", asset_date=d))
    db.commit()


def _attach(db, from_ids, to_ids, link_type, *, ratios=None,
            eff=D(2026, 9, 16), reason="补登记：示例口径切换"):
    return route_attach(AttachRequest(
        from_item_ids=from_ids, to_item_ids=to_ids, link_type=link_type,
        ratios=ratios, effective_date=eff.isoformat() if eff else None,
        reason=reason), db)


def _counts(db) -> tuple[int, int, int, int]:
    return (
        db.execute(select(func.count()).select_from(AssetItem)).scalar(),
        db.execute(select(func.count()).select_from(LineageLink)).scalar(),
        db.execute(select(func.count()).select_from(PrincipalChange)).scalar(),
        db.execute(select(func.count()).select_from(AuditLog)).scalar(),
    )


def _statuses(db) -> dict[int, str]:
    return {it.id: it.status for it in
            db.execute(select(AssetItem)).scalars().all()}


def _assert_zero_writes(db, counts, statuses):
    assert _counts(db) == counts
    assert _statuses(db) == statuses


def _series(db, dates) -> list[tuple[Decimal, Decimal, Decimal]]:
    return [analysis_service._sum_as_of(db, d) for d in dates]


def _trend(db, item_id, **kw):
    return analysis_service.get_item_trend(db, item_id, **kw)


# ---------------------------------------------------------------- revision

def test_attach_revision_existing_target_zero_delta(db):
    old = _mkitem(db, "示例-旧券")
    new = _mkitem(db, "示例-新券")
    _snap(db, old, "示例-旧券", 1000, D(2026, 6, 1))
    _snap(db, old, "示例-旧券", 1200, D(2026, 9, 15))
    _snap(db, old, "示例-旧券", 0, D(2026, 9, 16))       # 历史切换的「归零」行
    _snap(db, new, "示例-新券", 1200, D(2026, 9, 16))     # 承接方早已存在

    dates = [D(2026, 6, 1), D(2026, 9, 15), D(2026, 9, 16)]
    before = _series(db, dates)

    out = _attach(db, [old], [new], "revision")

    assert _series(db, dates) == before                   # 零迁移：数值不变
    assert out["principal_written"] is False
    assert out["archived_effective_from"] == "2026-09-16"
    assert out["from_items"][0]["status"] == "archived"
    assert out["from_items"][0]["archived_effective_from"] == "2026-09-16"
    assert out["to_items"][0]["id"] == new
    assert len(out["links"]) == 1
    assert out["links"][0]["link_type"] == "revision"
    assert out["links"][0]["split_ratio"] is None

    item = db.get(AssetItem, old)
    assert item.status == "archived" and item.archived_effective_from == D(2026, 9, 16)
    assert db.execute(select(func.count()).select_from(PrincipalChange)).scalar() == 0


# ---------------------------------------------------------------- merge

def test_attach_merge_existing_target_zero_delta(db):
    a = _mkitem(db, "示例-合并甲")
    b = _mkitem(db, "示例-合并乙")
    dst = _mkitem(db, "示例-合并仓")
    _snap(db, a, "示例-合并甲", 700, D(2026, 6, 1))
    _snap(db, b, "示例-合并乙", 300, D(2026, 6, 1))
    _snap(db, a, "示例-合并甲", 0, D(2026, 9, 16))
    _snap(db, b, "示例-合并乙", 0, D(2026, 9, 16))
    _snap(db, dst, "示例-合并仓", 1000, D(2026, 9, 16))

    dates = [D(2026, 6, 1), D(2026, 9, 16)]
    before = _series(db, dates)

    out = _attach(db, [a, b], [dst], "merge")

    assert _series(db, dates) == before
    assert out["principal_written"] is False
    assert len(out["links"]) == 2
    assert {lk["from_item_id"] for lk in out["links"]} == {a, b}
    assert {lk["to_item_id"] for lk in out["links"]} == {dst}
    assert all(lk["link_type"] == "merge" for lk in out["links"])
    assert all(lk["split_ratio"] is None for lk in out["links"])
    assert {it["id"] for it in out["from_items"]} == {a, b}
    assert all(it["status"] == "archived" for it in out["from_items"])
    assert db.execute(select(func.count()).select_from(PrincipalChange)).scalar() == 0


# ---------------------------------------------------------------- split

def test_attach_split_existing_targets_zero_delta_and_ratios(db):
    parent = _mkitem(db, "示例-拆分父")
    c1 = _mkitem(db, "示例-拆分甲")
    c2 = _mkitem(db, "示例-拆分乙")
    _snap(db, parent, "示例-拆分父", 1000, D(2026, 6, 1))
    _snap(db, parent, "示例-拆分父", 1200, D(2026, 9, 15))
    _snap(db, parent, "示例-拆分父", 0, D(2026, 9, 16))
    _snap(db, c1, "示例-拆分甲", 780, D(2026, 9, 16))
    _snap(db, c2, "示例-拆分乙", 420, D(2026, 9, 16))

    dates = [D(2026, 6, 1), D(2026, 9, 15), D(2026, 9, 16)]
    before = _series(db, dates)

    out = _attach(db, [parent], [c1, c2], "split",
                  ratios=["0.65", "0.35"])

    assert _series(db, dates) == before
    assert out["principal_written"] is False
    assert len(out["links"]) == 2
    ratios = {lk["to_item_id"]: Decimal(lk["split_ratio"]) for lk in out["links"]}
    assert ratios == {c1: _dec("0.6500"), c2: _dec("0.3500")}
    assert db.get(AssetItem, parent).status == "archived"
    assert db.execute(select(func.count()).select_from(PrincipalChange)).scalar() == 0
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.entity == "lineage_attach")).scalar() == 1
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.entity == "lineage_link")).scalar() == 2


# ---------------------------------------------------------------- family

def test_family_closure_two_level_chain(db):
    a = _mkitem(db, "示例-链A")
    b = _mkitem(db, "示例-链B")
    c = _mkitem(db, "示例-链C")

    _attach(db, [a], [b], "revision", eff=D(2026, 2, 1))
    _attach(db, [b], [c], "revision", eff=D(2026, 3, 1))

    fam = lineage_family(item_id=b, db=db)
    by_id = {m["item_id"]: m for m in fam["members"]}
    assert set(by_id) == {a, b, c}
    assert by_id[b]["relationship"] == "self"
    assert by_id[b]["link_type"] is None
    assert by_id[a]["relationship"] == "predecessor"
    assert by_id[a]["link_type"] == "revision"
    assert by_id[a]["status"] == "archived"
    assert by_id[a]["effective_from"] == "2026-02-01"
    assert by_id[c]["relationship"] == "successor"
    assert by_id[c]["link_type"] == "revision"
    assert by_id[c]["status"] != "archived"
    assert by_id[c]["effective_from"] is None
    assert len(fam["links"]) == 2

    # 从任意一端出发，闭包都是同一族
    assert {m["item_id"] for m in lineage_family(item_id=a, db=db)["members"]} == {a, b, c}
    assert {m["item_id"] for m in lineage_family(item_id=c, db=db)["members"]} == {a, b, c}


def test_attach_cycle_rejected_zero_writes(db):
    a = _mkitem(db, "示例-环A")
    b = _mkitem(db, "示例-环B")
    db.add(LineageLink(from_item_id=a, to_item_id=b, link_type="revision",
                       created_at="2026-01-01T00:00:00"))
    db.commit()
    counts, statuses = _counts(db), _statuses(db)

    with pytest.raises(ApiError) as ei:
        _attach(db, [b], [a], "revision")
    assert ei.value.code == ErrorCode.LINEAGE_CYCLE
    assert ei.value.status_code == 400
    _assert_zero_writes(db, counts, statuses)


# ---------------------------------------------------------------- trend

def _split_family_fixture(db):
    parent = _mkitem(db, "示例-支付-贵金属-黄金")
    c1 = _mkitem(db, "示例-支付-黄金-A")
    c2 = _mkitem(db, "示例-支付-黄金-B")
    _snap(db, parent, "示例-支付-贵金属-黄金", 1000, D(2026, 6, 1))
    _snap(db, parent, "示例-支付-贵金属-黄金", 1200, D(2026, 9, 15))
    _snap(db, c1, "示例-支付-黄金-A", 780, D(2026, 9, 16))
    _snap(db, c2, "示例-支付-黄金-B", 420, D(2026, 9, 16))
    _attach(db, [parent], [c1, c2], "split", ratios=["0.65", "0.35"])
    return parent, c1, c2


def test_item_trend_family_continuous_no_double_count(db):
    parent, c1, c2 = _split_family_fixture(db)
    out = _trend(db, parent, family=True, period_type="day")

    assert out["item"] == {"item_id": parent,
                           "name": "示例-支付-贵金属-黄金",
                           "family_size": 3}
    points = {p["period"]: p for p in out["points"]}
    pre = points["2026-09-15"]
    switch = points["2026-09-16"]

    # 切换前：父项真实历史值（不塌到 0）
    assert Decimal(pre["total"]) == _dec(1200)
    assert Decimal(pre["self"]) == _dec(1200)
    assert {m["item_id"] for m in pre["members"]} == {parent}

    # 切换日：族合计＝子项之和（父项已退出，不出现父+子双算）
    assert Decimal(switch["total"]) == _dec(1200)
    assert Decimal(switch["self"]) == _dec(0)            # 父项自身已归零/退出
    members = {m["item_id"]: Decimal(m["amount"]) for m in switch["members"]}
    assert members[parent] == _dec(0)                    # 前身显式 0（tooltip 构成）
    assert members[c1] == _dec(780)
    assert members[c2] == _dec(420)


def test_item_trend_family_false_is_single_item(db):
    parent, _c1, _c2 = _split_family_fixture(db)
    out = _trend(db, parent, family=False, period_type="day")
    points = {p["period"]: p for p in out["points"]}
    assert Decimal(points["2026-09-15"]["total"]) == _dec(1200)
    assert Decimal(points["2026-09-16"]["total"]) == _dec(0)
    assert Decimal(points["2026-09-16"]["self"]) == _dec(0)
    # 后一位（子项）在 family=false 时按自身身份取数
    child_view = _trend(db, _c1, family=False, period_type="day")
    child_points = {p["period"]: p for p in child_view["points"]}
    assert Decimal(child_points["2026-09-16"]["total"]) == _dec(780)


# ---------------------------------------------------------------- validation

def _seed_three(db):
    a = _mkitem(db, "示例-校验A")
    b = _mkitem(db, "示例-校验B")
    c = _mkitem(db, "示例-校验C")
    return a, b, c


def test_validation_shape_violations_zero_writes(db):
    a, b, c = _seed_three(db)
    counts, statuses = _counts(db), _statuses(db)

    cases = [
        ([a], [b, c], "merge", "merge 必须只有一个后继项"),
        ([a, b], [c], "split", "split 必须只有一个前身项"),
        ([a, b], [c], "revision", "revision 必须是 1:1"),
        ([a], [b, c], "revision", "revision 必须是 1:1"),
        ([], [b], "revision", "from_item_ids 为空"),
        ([a], [], "revision", "to_item_ids 为空"),
    ]
    for from_ids, to_ids, link_type, msg in cases:
        with pytest.raises(ApiError) as ei:
            _attach(db, from_ids, to_ids, link_type)
        assert ei.value.code == ErrorCode.BAD_REQUEST
        if msg:
            assert msg in ei.value.message
    # 2→2 一律拒绝
    with pytest.raises(ApiError) as ei:
        _attach(db, [a, b], [b, c], "merge")
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, counts, statuses)


def test_validation_intersection_missing_and_archived(db):
    a, b, c = _seed_three(db)
    counts, statuses = _counts(db), _statuses(db)

    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [a], "revision")
    assert ei.value.code == ErrorCode.BAD_REQUEST

    with pytest.raises(ApiError) as ei:
        _attach(db, [a, 999991], [b], "merge")
    assert ei.value.code == ErrorCode.NOT_FOUND

    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [999992], "revision")
    assert ei.value.code == ErrorCode.NOT_FOUND

    asset_service.set_item_status(db, a, "archived",
                                  effective_from=D(2026, 1, 1),
                                  reason="normalization")
    counts, statuses = _counts(db), _statuses(db)
    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [b], "revision")
    assert ei.value.code == ErrorCode.ITEM_ARCHIVED
    assert ei.value.status_code == 409
    _assert_zero_writes(db, counts, statuses)


def test_validation_duplicate_pair(db):
    a, b, _c = _seed_three(db)
    db.add(LineageLink(from_item_id=a, to_item_id=b, link_type="revision",
                       created_at="2026-01-01T00:00:00"))
    db.commit()
    counts, statuses = _counts(db), _statuses(db)

    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [b], "revision")
    assert ei.value.code == ErrorCode.LINEAGE_EXISTS
    assert ei.value.status_code == 409
    _assert_zero_writes(db, counts, statuses)


def test_validation_effective_date_and_reason_required(db):
    a, b, _c = _seed_three(db)
    counts, statuses = _counts(db), _statuses(db)

    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [b], "revision", eff=None)
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert ei.value.details["field"] == "effective_date"

    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [b], "revision", reason="   ")
    assert ei.value.code == ErrorCode.PRINCIPAL_REASON_REQUIRED
    _assert_zero_writes(db, counts, statuses)


def test_validation_bad_ratios_zero_writes(db):
    a, b, c = _seed_three(db)
    counts, statuses = _counts(db), _statuses(db)

    bad = [
        (["0.5", "0.4"], "sum"),          # 合计 0.9
        (["0", "1"], "range"),            # 0 越界
        (["1.1", "-0.1"], "range"),       # >1
        (["1.0"], "length"),              # 数量不匹配
        (["0.5", "0.5", "0.0"], "length"),
    ]
    for ratios, _kind in bad:
        with pytest.raises(ApiError) as ei:
            _attach(db, [a], [b, c], "split", ratios=ratios)
        assert ei.value.code == ErrorCode.BAD_REQUEST
        assert ei.value.details["field"] == "ratios"

    # ratios 仅 split 多后继可用
    with pytest.raises(ApiError) as ei:
        _attach(db, [a], [b], "revision", ratios=["1.0"])
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, counts, statuses)


# ---------------------------------------------------------------- backward compat

def _analysis_client(db):
    from main import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(analysis_router.router, prefix="/api/analysis")
    return TestClient(app)


def test_asset_name_item_trend_keeps_old_array_shape(db):
    item = _mkitem(db, "示例-兼容项")
    _snap(db, item, "示例-兼容项", 100, D(2026, 1, 1))
    _snap(db, item, "示例-兼容项", 150, D(2026, 2, 1))

    client = _analysis_client(db)
    resp = client.get("/api/analysis/by-item",
                      params={"asset_name": "示例-兼容项", "period_type": "day"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert set(body[0]) == {"period", "date", "amount", "change_amount"}
    assert Decimal(body[-1]["amount"]) == _dec(150)
    # 旧数组必须逐字节保持：金额/变动额为字符串，date 为 ISO 字符串
    assert resp.text == (
        '[{"period":"2026-01-01","date":"2026-01-01","amount":"100.00",'
        '"change_amount":null},{"period":"2026-02-01","date":"2026-02-01",'
        '"amount":"150.00","change_amount":"50.00"}]')

    # item_id 族模式返回新结构（字符串金额）
    resp2 = client.get("/api/analysis/by-item",
                       params={"item_id": item, "period_type": "day"})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert set(body2) == {"item", "points"}
    assert body2["item"]["family_size"] == 1
    assert Decimal(body2["points"][-1]["total"]) == _dec(150)