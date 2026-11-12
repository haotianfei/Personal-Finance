"""纠正类操作的原因采集（`.scratch/audit-reason-capture/spec.md` R1–R5/R7）。

覆盖 spec §3 的 1–6：
1. `correct` 带 `reason`/`reason_note` → 审计行两列有值（query 与请求体两种位置各一例；
   本端点额外兼容 body，见 `AssetCorrectRequest` 的 docstring）；
2. 同一端点不带 `reason` → `reason IS NULL`，HTTP 仍 200（R3：不强制）；
3. `reason` 非法枚举 / `reason_note` 201 字 → **422 且零审计行**（校验在进入 service
   之前失败，天然不留半条审计）；
4. `DELETE` 带 `reason` → 行内值；`batch-delete` → **只头行有值**，逐行明细 NULL（R5）；
5. `owner` / `classification` 带 `reason` → 行内值（另附 `archive` 一例）；
6. `rename` 不新增参数：`reason` 由 `effect` 映射（`history_backfill`→`correction`，
   `current`→`evolution`），`reason_note` 透传。

附带一条契约守卫：`schemas.AuditReason`（端点侧 Literal）与
`services.audit_contract.REASON` 必须同集，否则端点会放进 service 拒收的值。

数据全部合成（「示例-」、虚构金额与日期），不触真实 data/。
"""
import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base, get_db
from models import (Asset, AssetClass, AssetItem, AuditLog, Institution, Member)
from schemas import AUDIT_REASON_NOTE_MAX, AuditReason
from services.audit_contract import REASON
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

TODAY = date.today()
M0010 = _load_script_module(f"{_MIGRATIONS_DIR}/0010_idempotency.py")


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def api(tmp_path):
    """真实文件库 ＋ assets / asset_items / classification(项级) 三组路由。"""
    db_path = tmp_path / "audit_reason.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0010.upgrade(conn)                    # 幂等表（中间件依赖）
    conn.execute("COMMIT")
    conn.close()

    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from main import register_exception_handlers
    from routers import asset_items, assets, classification

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = _override_db
    app.include_router(assets.router, prefix="/api/assets")
    app.include_router(asset_items.router, prefix="/api/asset-items")
    app.include_router(classification.item_router, prefix="/api/asset-items")
    return SimpleNamespace(client=TestClient(app), Session=Session)


# ---------------------------------------------------------------- 造数


def _member(db, name="示例成员甲", role="self"):
    row = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if row is not None:
        return row
    row = Member(name=name, role=role)
    db.add(row)
    db.commit()
    return row


def _get_or_create(db, model, *, name, defaults):
    row = db.execute(select(model).where(model.name == name)).scalar_one_or_none()
    if row is not None:
        return row
    row = model(name=name, **defaults)
    db.add(row)
    db.commit()
    return row


def _item(db, name="示例-资产", *, owner=None):
    """四维齐全的 active 项（classification / owner / archive 都需要合法状态机）。"""
    klass = _get_or_create(db, AssetClass, name="示例类别",
                           defaults={"code": "T1", "level": 1,
                                     "side_scope": "any"})
    inst = _get_or_create(db, Institution, name="示例机构",
                          defaults={"kind": "bank"})
    if owner is None:
        owner = _member(db)
    item = AssetItem(display_name=name, status="active",
                     asset_class_id=klass.id, liquidity_days=0,
                     institution_id=inst.id, owner_id=owner.id)
    db.add(item)
    db.commit()
    return item


def _asset(db, item, amount="100"):
    row = Asset(asset_name=item.display_name, asset_item_id=item.id,
                amount=Decimal(str(amount)), asset_date=TODAY, side="asset")
    db.add(row)
    db.commit()
    return row


def _audits(db, entity=None, action=None):
    q = select(AuditLog).order_by(AuditLog.id)
    if entity is not None:
        q = q.where(AuditLog.entity == entity)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q).scalars().all())


def _audit_count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar_one()


# ---------------------------------------------------------------- 契约守卫


def test_reason_literal_matches_audit_contract():
    """端点侧 Literal 与契约枚举同集：单边增删都会在这里红。"""
    assert set(get_args(AuditReason)) == REASON
    assert AUDIT_REASON_NOTE_MAX == 200


# ---------------------------------------------------------------- 1/2. correct


def test_correct_with_reason_writes_audit_columns(api):
    """验收 1：带 reason/reason_note → 审计行两列有值。"""
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    db.close()

    resp = api.client.post(
        f"/api/assets/{asset_id}/correct",
        params={"reason": "correction", "reason_note": "OCR 多打一个 0"},
        json={"amount": "10"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "asset_snapshot", "correct")
    assert len(rows) == 1
    assert rows[0].reason == "correction"
    assert rows[0].reason_note == "OCR 多打一个 0"
    # 业务事实仍照常写入（reason 不改业务语义）
    assert db.get(Asset, asset_id).superseded_by is not None
    db.close()


def test_correct_with_reason_in_body_is_also_captured(api):
    """spec §3-① 的验收示例形状（请求体带 reason/reason_note）同样落审计列。"""
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    db.close()

    resp = api.client.post(f"/api/assets/{asset_id}/correct",
                           json={"amount": "10", "reason": "correction",
                                 "reason_note": "OCR 多打一个 0"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "asset_snapshot", "correct")
    assert len(rows) == 1
    assert rows[0].reason == "correction"
    assert rows[0].reason_note == "OCR 多打一个 0"
    db.close()


def test_correct_without_reason_is_422_and_leaves_nothing(api):
    """spec 验收 1（audit-reason-enforce E1）：不传 → 422，**业务与审计都不落**。

    本票（audit-reason-capture 的 R3「可选」）已被这一票翻转：可选通道早已通了，
    强制点收口到 `audit_service.record()`，缺 reason 不再静默写 NULL 行。
    """
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    before = _audit_count(db)
    db.close()

    resp = api.client.post(f"/api/assets/{asset_id}/correct", json={"amount": "11"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "AUDIT_REASON_REQUIRED"

    db = api.Session()
    assert _audit_count(db) == before
    assert db.get(Asset, asset_id).superseded_by is None     # 业务行没动
    assert len(_audits(db, "asset_snapshot", "correct")) == 0
    db.close()


# ---------------------------------------------------------------- 3. 校验 422


def test_invalid_reason_is_422_and_leaves_no_audit(api):
    """验收 3：非法枚举 → 422，且零审计行（校验在进入 service 之前）。"""
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    before = _audit_count(db)
    db.close()
    assert before == 0

    resp = api.client.post(f"/api/assets/{asset_id}/correct",
                           params={"reason": "乱填"}, json={"amount": "10"})
    assert resp.status_code == 422, resp.text

    # 请求体形态同样被 schema 挡住（本端点两种位置都接受）
    resp = api.client.post(f"/api/assets/{asset_id}/correct",
                           json={"amount": "10", "reason": "乱填"})
    assert resp.status_code == 422, resp.text

    db = api.Session()
    assert _audit_count(db) == 0
    assert db.get(Asset, asset_id).superseded_by is None   # 业务行也没动
    db.close()


def test_reason_note_over_200_is_422_and_leaves_no_audit(api):
    """验收 3：reason_note 201 字 → 422，且零审计行。"""
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    db.close()

    resp = api.client.post(
        f"/api/assets/{asset_id}/correct",
        params={"reason": "correction", "reason_note": "说" * 201},
        json={"amount": "10"})
    assert resp.status_code == 422, resp.text

    db = api.Session()
    assert _audit_count(db) == 0
    db.close()


# ---------------------------------------------------------------- 4. delete 家族


def test_delete_with_reason_writes_audit_row(api):
    """验收 4：DELETE 带 reason（query 形态，DELETE 不带 body）。"""
    db = api.Session()
    asset = _asset(db, _item(db))
    asset_id = asset.id
    db.close()

    resp = api.client.delete(f"/api/assets/{asset_id}",
                             params={"reason": "normalization",
                                     "reason_note": "重复录入已清理"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "asset_snapshot", "delete")
    assert len(rows) == 1
    assert rows[0].reason == "normalization"
    assert rows[0].reason_note == "重复录入已清理"
    assert db.get(Asset, asset_id) is None
    db.close()


def test_batch_delete_reason_on_header_and_details(api):
    """spec E1：头行与逐行明细**同 reason**（R5「逐行不带」被 E1 收口取代）。

    理由：E1 之后审计里不允许存在 `actor='owner'` 的无理由 delete 行——否则整批
    删除的明细就是无法解释的离场证据；头行只解释「这一批」，明细各自也是证据。
    """
    db = api.Session()
    ids = [_asset(db, _item(db, "示例-资产甲"), amount="1").id,
           _asset(db, _item(db, "示例-资产乙"), amount="2").id]
    db.close()

    resp = api.client.post("/api/assets/batch-delete",
                           params={"reason": "correction",
                                   "reason_note": "批量清掉错录的三行"},
                           json={"ids": ids})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_count"] == 2

    db = api.Session()
    header = _audits(db, "batch", "execute")
    assert len(header) == 1
    assert header[0].reason == "correction"
    assert header[0].reason_note == "批量清掉错录的三行"

    details = _audits(db, "asset", "delete")
    assert len(details) == 2
    assert [d.reason for d in details] == ["correction", "correction"]
    assert [d.reason_note for d in details] == ["批量清掉错录的三行"] * 2
    db.close()


# ---------------------------------------------------------------- 5. owner / classification / archive


def test_owner_with_reason_writes_audit_row(api):
    """验收 5：归属改判带 reason（query）——请求体 `reason` 仍是变更史自由文本。"""
    db = api.Session()
    item = _item(db)
    item_id = item.id
    other = _member(db, "示例成员乙", "spouse")
    other_id = other.id
    db.close()

    resp = api.client.post(
        f"/api/asset-items/{item_id}/owner",
        params={"reason": "evolution", "reason_note": "婚前财产转共同"},
        json={"owner_kind": "member", "owner_id": other_id,
              "reason": "人工改归属"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "asset_item_owner", "change")
    assert len(rows) == 1
    assert rows[0].reason == "evolution"
    assert rows[0].reason_note == "婚前财产转共同"
    # 两套「原因」互不串台：变更史记自由文本，审计记封闭枚举
    from models import AssetItemOwnerChange
    history = db.execute(select(AssetItemOwnerChange)).scalars().all()
    assert history[-1].reason == "人工改归属"
    db.close()


def test_classification_with_reason_writes_audit_row(api):
    """验收 5：项级分类改判带 reason（query）；请求体 `reason` 仍进变更史。"""
    db = api.Session()
    item = _item(db)
    item_id = item.id
    db.close()

    resp = api.client.put(
        f"/api/asset-items/{item_id}/classification",
        params={"reason": "correction", "reason_note": "风险等级填反了"},
        json={"risk_level": "R3", "reason": "分类复核"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "classification", "update")
    assert len(rows) == 1
    assert rows[0].row_id == item_id
    assert rows[0].reason == "correction"
    assert rows[0].reason_note == "风险等级填反了"
    db.close()


def test_archive_with_reason_writes_audit_row(api):
    """R1 第 4 个端点：归档带 reason → 审计行带值。"""
    db = api.Session()
    item = _item(db)
    item_id = item.id
    db.close()

    resp = api.client.post(f"/api/asset-items/{item_id}/archive",
                           params={"reason": "evolution",
                                   "reason_note": "标的已清仓"})
    assert resp.status_code == 200, resp.text

    db = api.Session()
    rows = _audits(db, "asset_item", "archive")
    assert len(rows) == 1
    assert rows[0].reason == "evolution"
    assert rows[0].reason_note == "标的已清仓"
    db.close()


# ---------------------------------------------------------------- 6. rename 映射


def _rename(api, item_id: int, *, new_name: str, reason: str,
            effect: str | None = None, note: str | None = None) -> str:
    preview = api.client.post(
        f"/api/asset-items/{item_id}/rename/preview",
        json={"name": new_name, "reason": reason,
              "effect": effect, "reason_note": note})
    assert preview.status_code == 200, preview.text
    token = preview.json()["confirm_token"]
    resp = api.client.post(f"/api/asset-items/{item_id}/rename",
                           json={"name": new_name, "reason": reason,
                                 "effect": effect, "reason_note": note,
                                 "confirm_token": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["effect"]


def test_rename_backfill_maps_to_correction_and_passes_note(api):
    """验收 6：回写历史（纠正过去）→ reason='correction'，reason_note 透传。"""
    db = api.Session()
    item = _item(db)
    item_id = item.id
    db.close()

    assert _rename(api, item_id, new_name="示例-资产-改", reason="correction",
                   note="旧名把机构写错了") == "history_backfill"

    db = api.Session()
    rows = _audits(db, "asset_item", "rename")
    assert len(rows) == 1
    assert rows[0].reason == "correction"
    assert rows[0].reason_note == "旧名把机构写错了"
    db.close()


def test_rename_current_name_maps_to_evolution(api):
    """验收 6：只改当前名（名称演进）→ reason='evolution'，reason_note 透传。"""
    db = api.Session()
    item = _item(db)
    item_id = item.id
    db.close()

    assert _rename(api, item_id, new_name="示例-资产-新", reason="evolution",
                   note="产品改名") == "current"

    db = api.Session()
    rows = _audits(db, "asset_item", "rename")
    assert len(rows) == 1
    assert rows[0].reason == "evolution"
    assert rows[0].reason_note == "产品改名"
    db.close()