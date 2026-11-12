"""一对多血缘拆分（1→N）测试（spec `.scratch/lineage-split/spec.md` §1）。

覆盖：happy path（序列不塌陷、拆分日无双算、父项归档生效日、比例与承接落库）、
六类预检失败且零写入（ratio 合计/区间、名称重复/占用含历史名、父项状态/存在、
reason、targets 数量、principal 负值）、幂等重放、审计行计数。
数据一律合成（「示例支付-」、虚构金额与日期）。
"""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from errors import ApiError, ErrorCode
from models import (Asset, AssetItem, AssetItemName, AuditLog, LineageLink,
                    PrincipalChange)
from routers import lineage_links as lineage_links_router
from routers.lineage_links import SplitRequest, SplitTarget
from routers.lineage_links import split_lineage as route_split
from services import analysis_service, asset_service
from services.asset_service import get_or_create_item
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module
from services.idempotency_service import IdempotencyCaptureMiddleware

M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")
D = date


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _targets(a=("示例支付-黄金-A", "0.7000", "840000.00"),
             b=("示例支付-黄金-B", "0.3000", "360000.00"),
             note=None):
    return [{"name": a[0], "ratio": a[1], "principal": a[2], "note": note},
            {"name": b[0], "ratio": b[1], "principal": b[2], "note": note}]


def _split(db, parent_id, targets=None, eff=None, reason="拆分：按渠道拆出"):
    return route_split(SplitRequest(
        from_item_id=parent_id,
        targets=[SplitTarget(**t) for t in (
            _targets() if targets is None else targets)],
        effective_date=eff.isoformat() if eff else None,
        reason=reason), db)


def _mkparent(db, name="示例支付-汇总"):
    item = get_or_create_item(db, name)
    db.commit()
    return item.id


def _snap(db, item_id, name, amount, d):
    db.add(Asset(asset_name=name, asset_item_id=item_id, amount=_dec(amount),
                 side="asset", asset_date=d))
    db.commit()


def _counts(db) -> tuple[int, int, int, int]:
    return (
        db.execute(select(func.count()).select_from(AssetItem)).scalar(),
        db.execute(select(func.count()).select_from(LineageLink)).scalar(),
        db.execute(select(func.count()).select_from(PrincipalChange)).scalar(),
        db.execute(select(func.count()).select_from(AssetItemName)).scalar(),
    )


def _assert_zero_writes(db, before):
    assert _counts(db) == before


# ---------------------------------------------------------------- happy path

def test_split_one_to_two_happy(db):
    pid = _mkparent(db)
    _snap(db, pid, "示例支付-汇总", 1000, D(2026, 6, 1))
    _snap(db, pid, "示例支付-汇总", 1200, D(2026, 9, 15))
    pre_0601 = analysis_service._sum_as_of(db, D(2026, 6, 1))
    pre_0915 = analysis_service._sum_as_of(db, D(2026, 9, 15))
    assert pre_0915 == (_dec(1200), _dec(1200), _dec(0))

    out = _split(db, pid, eff=D(2026, 9, 16))

    # 拆分前的 as-of 点不塌陷（父项历史值保留）
    assert analysis_service._sum_as_of(db, D(2026, 6, 1)) == pre_0601
    assert analysis_service._sum_as_of(db, D(2026, 9, 15)) == pre_0915

    # 子项自拆分日起计量 → 当日只有子项，无父/子双算
    for name, amount in (("示例支付-黄金-A", 840), ("示例支付-黄金-B", 360)):
        child = get_or_create_item(db, name)
        _snap(db, child.id, name, amount, D(2026, 9, 16))
    assert analysis_service._sum_as_of(db, D(2026, 9, 16)) == (
        _dec(1200), _dec(1200), _dec(0))

    # 父项归档生效日＝拆分日
    parent = db.get(AssetItem, pid)
    assert parent.status == "archived"
    assert parent.archived_effective_from == D(2026, 9, 16)
    assert out["from_item"]["id"] == pid
    assert out["from_item"]["status"] == "archived"
    assert out["archived_effective_from"] == "2026-09-16"
    assert out["from_item"]["archived_effective_from"] == "2026-09-16"

    # 两条 split 链：父→子，比例正确
    links = db.execute(select(LineageLink)
                       .where(LineageLink.from_item_id == pid)
                       .order_by(LineageLink.id)).scalars().all()
    assert len(links) == 2
    assert all(lk.link_type == "split" for lk in links)
    assert [lk.to_item_id for lk in links] == [
        get_or_create_item(db, "示例支付-黄金-A").id,
        get_or_create_item(db, "示例支付-黄金-B").id,
    ]
    assert [lk.split_ratio for lk in links] == [_dec("0.7000"), _dec("0.3000")]
    assert [Decimal(lk["split_ratio"]) for lk in out["links"]] == [
        _dec("0.7000"), _dec("0.3000")]

    # 承接行：origin='lineage'、金额/生效日落库
    entries = db.execute(select(PrincipalChange)
                         .where(PrincipalChange.origin == "lineage")
                         .order_by(PrincipalChange.id)).scalars().all()
    assert len(entries) == 2
    assert [e.new_amount for e in entries] == [_dec("840000.00"), _dec("360000.00")]
    assert all(e.effective_date == D(2026, 9, 16) for e in entries)
    assert [e["origin"] for e in out["entries"]] == ["lineage", "lineage"]
    assert [Decimal(e["amount"]) for e in out["entries"]] == [
        _dec("840000.00"), _dec("360000.00")]
    assert all(e["effective_date"] == "2026-09-16" for e in out["entries"])


# ---------------------------------------------------------------- 校验：ratio

def test_split_ratio_sum_must_be_one_zero_writes(db):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-黄金-A", "0.7000", "840000.00"),
                                 ("示例支付-黄金-B", "0.4000", "360000.00")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


@pytest.mark.parametrize("bad_ratio", ["0", "0.0000", "1.0001", "1.5", "-0.1"])
def test_split_ratio_out_of_range_zero_writes(db, bad_ratio):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-黄金-A", bad_ratio, "800"),
                                 ("示例支付-黄金-B", "0.3000", "200")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert ei.value.details["field"] == "targets"
    _assert_zero_writes(db, before)


# ---------------------------------------------------------------- 校验：名称

def test_split_duplicate_target_name_zero_writes(db):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-同名", "0.5000", "600"),
                                 ("示例支付-同名", "0.5000", "600")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


def test_split_target_name_taken_by_existing_item_zero_writes(db):
    pid = _mkparent(db)
    get_or_create_item(db, "示例支付-现有项")
    db.commit()
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-现有项", "0.7000", "800"),
                                 ("示例支付-黄金-B", "0.3000", "200")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


def test_split_target_name_taken_by_historical_alias_zero_writes(db):
    pid = _mkparent(db)
    other = get_or_create_item(db, "示例支付-已改名项")
    db.add(AssetItemName(item_id=other.id, name="示例支付-历史别名",
                         effective_from=D(2026, 1, 1), origin="rename"))
    db.commit()
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-历史别名", "0.7000", "800"),
                                 ("示例支付-黄金-B", "0.3000", "200")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


# ---------------------------------------------------------------- 校验：父项/reason

def test_split_parent_archived_409(db):
    pid = _mkparent(db)
    asset_service.set_item_status(db, pid, "archived",
                                  effective_from=D(2026, 1, 1),
                                  reason="normalization")
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, eff=D(2026, 9, 16))
    assert ei.value.code == ErrorCode.ITEM_ARCHIVED
    assert ei.value.status_code == 409
    _assert_zero_writes(db, before)


def test_split_parent_missing_404(db):
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, 9999)
    assert ei.value.code == ErrorCode.NOT_FOUND
    _assert_zero_writes(db, before)


def test_split_reason_required(db):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, reason="  ")
    assert ei.value.code == ErrorCode.PRINCIPAL_REASON_REQUIRED
    _assert_zero_writes(db, before)


# ---------------------------------------------------------------- 校验：数量/金额

@pytest.mark.parametrize("targets", [
    [],
    [{"name": "示例支付-唯一", "ratio": "1.0000", "principal": "100", "note": None}],
])
def test_split_requires_at_least_two_targets(db, targets):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, targets)
    assert ei.value.code == ErrorCode.BAD_REQUEST
    assert ei.value.details["field"] == "targets"
    _assert_zero_writes(db, before)


def test_split_negative_principal_zero_writes(db):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-黄金-A", "0.7000", "-1"),
                                 ("示例支付-黄金-B", "0.3000", "200")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


def test_split_invalid_principal_zero_writes(db):
    pid = _mkparent(db)
    before = _counts(db)
    with pytest.raises(ApiError) as ei:
        _split(db, pid, _targets(("示例支付-黄金-A", "0.7000", "abc"),
                                 ("示例支付-黄金-B", "0.3000", "200")))
    assert ei.value.code == ErrorCode.BAD_REQUEST
    _assert_zero_writes(db, before)


# ---------------------------------------------------------------- 审计

def test_split_audit_rows(db):
    pid = _mkparent(db)
    out = _split(db, pid, eff=D(2026, 9, 16))
    rows = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()

    links = [r for r in rows if r.entity == "lineage_link" and r.action == "lineage_link"]
    entries = [r for r in rows if r.entity == "principal_change"
               and r.action == "principal_set"]
    split_rows = [r for r in rows if r.entity == "lineage_split"
                  and r.action == "create"]
    archives = [r for r in rows if r.entity == "asset_item" and r.action == "archive"]

    assert len(links) == 2
    assert len(entries) == 2
    assert len(split_rows) == 1
    assert len(archives) == 1

    assert {r.row_id for r in links} == {lk["id"] for lk in out["links"]}
    entry_ids = {row.id for row in db.execute(
        select(PrincipalChange).where(PrincipalChange.origin == "lineage")
    ).scalars().all()}
    assert {r.row_id for r in entries} == entry_ids
    assert archives[0].row_id == pid
    assert "2026-09-16" in (split_rows[0].after_json or "")
    assert '"ratio": "0.7000"' in (split_rows[0].after_json or "")


# ---------------------------------------------------------------- 幂等重放

@pytest.fixture()
def split_app(tmp_path):
    path = str(tmp_path / "split.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        M0010.upgrade(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    Session = sessionmaker(bind=engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(IdempotencyCaptureMiddleware)
    app.dependency_overrides[get_db] = _override
    app.include_router(lineage_links_router.router, prefix="/api/lineage-links")
    return {"app": app, "Session": Session}


def test_split_idempotent_replay(split_app):
    Session = split_app["Session"]
    db = Session()
    pid = _mkparent(db)
    db.close()

    body = {
        "from_item_id": pid,
        "targets": _targets(),
        "effective_date": "2026-09-16",
        "reason": "拆分：按渠道拆出",
    }
    client = TestClient(split_app["app"])
    headers = {"Idempotency-Key": "split-replay-1"}
    first = client.post("/api/lineage-links/split", json=body, headers=headers)
    second = client.post("/api/lineage-links/split", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert first.text == second.text

    db = Session()
    assert db.execute(select(func.count()).select_from(AssetItem)).scalar() == 3
    assert db.execute(select(func.count()).select_from(LineageLink)).scalar() == 2
    assert db.execute(select(func.count()).select_from(PrincipalChange)
                      .where(PrincipalChange.origin == "lineage")).scalar() == 2
    assert db.execute(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.entity == "lineage_split")).scalar() == 1
    db.close()


def test_split_endpoint_bad_ratio_400(split_app):
    Session = split_app["Session"]
    db = Session()
    pid = _mkparent(db)
    db.close()

    body = {
        "from_item_id": pid,
        "targets": _targets(("示例支付-黄金-A", "0.7000", "840000.00"),
                            ("示例支付-黄金-B", "0.4000", "360000.00")),
        "reason": "拆分：按渠道拆出",
    }
    client = TestClient(split_app["app"])
    response = client.post("/api/lineage-links/split", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"

    db = Session()
    assert db.execute(select(func.count()).select_from(LineageLink)).scalar() == 0
    db.close()


# ---------------------------------------------------------------- 默认生效日

def test_split_defaults_effective_date_to_today(db):
    pid = _mkparent(db)
    out = _split(db, pid)
    assert out["archived_effective_from"] == date.today().isoformat()
    assert db.get(AssetItem, pid).archived_effective_from == date.today()