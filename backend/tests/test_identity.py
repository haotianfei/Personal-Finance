"""v5 一期·步骤 3「身份层落地」测试（spec §6，真实库副本演练由验收方执行）。

① 0008 回填+校验断言（含负债行）；② PUT 改金额→409；③ batch overwrite 拒绝/
skip 显式清单；④ correct 追加+supersede+净值取新+同日双 active 拒绝；
⑤ rename 留史/旧名可查/历史行不回写；⑥ archive 状态与过滤；
⑦ _upsert/apply 无 action 冲突显式返回。
沙箱：tmp_path 文件库 + monkeypatch DATABASE_PATH；真实 migrations/ 只读。
"""
import json
import os
import sqlite3
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import Asset, AssetItem, AssetItemName
from services import asset_service, backup_service
from services import db_migration_service as dbs
from services.asset_service import WriteOnceConflict


def confirms(p) -> dict:
    """D4：create 行必须显式表态。测试里统一走「确认新增」（不是本特性的被测对象）。"""
    return {str(r.index if r.index is not None else pos): {"action": "create"}
            for pos, r in enumerate(p.rows) if r.action == "create"}


# ---------------------------------------------------------------- fixtures

V4_DDL = [
    "CREATE TABLE screenshot_evidence (id INTEGER PRIMARY KEY, source_group VARCHAR(50))",
    "CREATE TABLE attributes (id INTEGER PRIMARY KEY, name VARCHAR(50), type VARCHAR(50))",
    "CREATE TABLE asset_attributes (asset_id INTEGER NOT NULL, attribute_id INTEGER NOT NULL,"
    " PRIMARY KEY (asset_id, attribute_id))",
    # v4 收敛形态：有 side/source_evidence_id，无 asset_item_id/superseded_by，旧 UQ 在
    "CREATE TABLE assets (id INTEGER PRIMARY KEY, asset_name VARCHAR(100) NOT NULL,"
    " amount NUMERIC(15,2) NOT NULL, side VARCHAR(10) NOT NULL DEFAULT 'asset',"
    " asset_date DATE NOT NULL, source_evidence_id INTEGER REFERENCES screenshot_evidence(id),"
    " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,"
    " CONSTRAINT uq_asset_date_name UNIQUE (asset_date, asset_name))",
]


def _make_v4_db(db_path):
    conn = sqlite3.connect(db_path)
    for ddl in V4_DDL:
        conn.execute(ddl)
    rows = [
        # (id, name, amount, side, date)——含正数负债行（金额恒非负，方向由 side 表达）
        (1, "示例-稳健基金", "4000.00", "asset", "2026-01-15"),
        (2, "示例-稳健基金", "4200.00", "asset", "2026-02-15"),
        (3, "示例-活期", "500.00", "asset", "2026-02-15"),
        (4, "示例-借款", "5.00", "liability", "2026-02-15"),
        (5, "其他渠道-同标的", "900.00", "asset", "2026-02-15"),
    ]
    for r in rows:
        conn.execute("INSERT INTO assets (id, asset_name, amount, side, asset_date,"
                     " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (*r, "2026-02-15 00:00:00", "2026-02-15 00:00:00"))
    # 挂接面：行 1/2 挂账户属性（asset_attributes 计数不变断言用）
    conn.execute("INSERT INTO attributes (id, name, type) VALUES (10, '示例银行', 'account')")
    conn.execute("INSERT INTO asset_attributes (asset_id, attribute_id) VALUES (1, 10)")
    conn.execute("INSERT INTO asset_attributes (asset_id, attribute_id) VALUES (2, 10)")
    conn.commit()
    conn.close()


def _nets(db_path):
    conn = sqlite3.connect(db_path)
    out = {str(d): Decimal(str(v)) for d, v in conn.execute(
        "SELECT asset_date, SUM(CASE WHEN side='liability' THEN -amount ELSE amount END)"
        " FROM assets GROUP BY asset_date")}
    conn.close()
    return out


@pytest.fixture()
def v4_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "person_fin.db")
    _make_v4_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    return db_path


# ---------------------------------------------------------------- ① 0008

def test_0008_backfill_and_validation(v4_db, tmp_path):
    pre_nets = _nets(v4_db)
    pre_ids = {r[0] for r in sqlite3.connect(v4_db).execute("SELECT id FROM assets")}

    # v4 库首跑：认领 0001~0007（CLAIM）＋其余编号脚本（0008 起，无 CLAIM）全部实跑
    expected = [v for v, _, _ in dbs._discover_scripts(dbs._MIGRATIONS_DIR) if v >= 8]
    report = dbs.run_migrations(db_path=v4_db)
    assert report.baseline == [1, 2, 3, 4, 5, 6, 7]
    assert report.applied == expected
    assert report.backup

    conn = sqlite3.connect(v4_db)
    # item 数＝distinct name 数；迁移不代做归档。
    # ADR-0019 起 status 全 draft：回填出来的项没有任何分类，而 active 要求六维齐全
    # （ck_asset_items_dims_required），0026 重建 asset_items 时会把 active 降级为 draft。
    names = {r[0] for r in conn.execute("SELECT DISTINCT asset_name FROM assets")}
    n_items = conn.execute("SELECT COUNT(*) FROM asset_items").fetchone()[0]
    assert n_items == len(names)
    assert conn.execute("SELECT COUNT(*) FROM asset_items WHERE status!='draft'").fetchone()[0] == 0
    # asset_item_id 零 NULL；id 原值保留不重编号
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE asset_item_id IS NULL").fetchone()[0] == 0
    assert {r[0] for r in conn.execute("SELECT id FROM assets")} == pre_ids
    # 净资产逐日合计一致（含负债符号）
    assert _nets(v4_db) == pre_nets
    # 旧 UQ 已删、部分唯一索引已建
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "uq_asset_date_name" not in idx
    assert "uq_assets_item_date_active" in idx
    # 显示名史：每 item 首条 origin='migration'，effective_from＝最早快照日
    for item_id, legacy in conn.execute("SELECT id, legacy_name FROM asset_items"):
        row = conn.execute(
            "SELECT name, effective_from, origin FROM asset_item_names WHERE item_id=?",
            (item_id,)).fetchone()
        assert row is not None and row[0] == legacy and row[2] == "migration"
    # 挂接面：0027 已把旧 EAV 两表物理删除（0008 重建时 asset_attributes 行数不变
    # 的断言随之失去载体），此处断言「表已不存在」+ 全库 FK 零违规
    assert {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('attributes','asset_attributes')")} == set()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()

    # 前置备份 bundle 已生成（reason=pre_migration）
    bundles = [f for f in os.listdir(os.path.join(os.path.dirname(v4_db), "backup"))
               if f.startswith("backup_") and f.endswith(".zip")]
    with zipfile.ZipFile(os.path.join(os.path.dirname(v4_db), "backup", bundles[-1])) as zf:
        assert json.loads(zf.read("manifest.json"))["reason"] == "pre_migration"


def test_0008_channel_inference(v4_db):
    dbs.run_migrations(db_path=v4_db)
    conn = sqlite3.connect(v4_db)
    # 最近快照行挂接 account 属性 → channel＝该属性名
    ch = conn.execute("SELECT channel FROM asset_items WHERE legacy_name='示例-稳健基金'").fetchone()[0]
    assert ch == "示例银行"
    # 无挂接且「-」首段不命中账户 → NULL（unspecified）
    ch2 = conn.execute("SELECT channel FROM asset_items WHERE legacy_name='示例-活期'").fetchone()[0]
    assert ch2 is None
    conn.close()


def test_runner_rerun_idempotent(v4_db):
    dbs.run_migrations(db_path=v4_db)
    conn = sqlite3.connect(v4_db)
    state = (conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM asset_items").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0])
    # 0008 本体二次直跑：守卫幂等（重建跳过、回填 DO NOTHING、校验通过）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "m0008", os.path.join(dbs._MIGRATIONS_DIR, "0008_asset_items.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    conn.isolation_level = None
    conn.execute("BEGIN")
    m.upgrade(conn)
    conn.execute("COMMIT")
    conn.close()
    after = _state(v4_db)
    assert after == state


def _state(db_path):
    conn = sqlite3.connect(db_path)
    s = (conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
         conn.execute("SELECT COUNT(*) FROM asset_items").fetchone()[0],
         conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0])
    conn.close()
    return s


# ---------------------------------------------------------------- ② PUT 409

def test_put_amount_rejected_409(db):
    from routers.assets import update_asset as route_update, AssetUpdateRequest
    a = asset_service.create_asset(db, "示例-股票", 1000, date(2026, 3, 1))
    with pytest.raises(HTTPException) as ei:
        route_update(a.id, AssetUpdateRequest(amount="2000"), db)
    assert ei.value.status_code == 409


def test_put_attribute_ids_rejected_422(db):
    """ADR-0019：分类已上移到资产项 → 记录 PUT 再传 attribute_ids 显式 422。"""
    from routers.assets import AssetUpdateRequest
    from errors import ApiError

    with pytest.raises(ApiError) as ei:
        AssetUpdateRequest(attribute_ids=[1])
    assert ei.value.status_code == 422
    assert ei.value.details["fields"][0]["field"] == "attribute_ids"
    assert "资产分类已上移到资产项" in ei.value.message


def test_put_name_rejected(db):
    a = asset_service.create_asset(db, "示例-改名前", 100, date(2026, 3, 1))
    with pytest.raises(WriteOnceConflict):
        asset_service.update_asset(db, a.id, asset_name="示例-改名后")


# ---------------------------------------------------------------- ③ batch

def test_batch_overwrite_removed_skip_explicit(db):
    rec = {"asset_name": "示例-标的X", "amount": "100",
           "asset_date": date(2026, 3, 1), "attribute_ids": []}
    created, skipped = asset_service.batch_create_assets(db, [rec], "skip")
    assert len(created) == 1 and skipped == []
    # 再导同值：skip 显式清单（不静默丢弃、不覆盖）
    created2, skipped2 = asset_service.batch_create_assets(db, [rec], "skip")
    assert created2 == []
    assert skipped2[0]["asset_name"] == "示例-标的X"
    assert skipped2[0]["reason"] == "duplicate_active"
    # overwrite 已冻结移除
    with pytest.raises(ValueError):
        asset_service.batch_create_assets(db, [rec], "overwrite")


def test_batch_router_surfaces_skipped(db):
    from routers.assets import batch_create as route_batch
    data = {"records": [{"asset_name": "示例-标的Y", "amount": "50",
                         "asset_date": "2026-03-01", "attribute_ids": []}],
            "conflict_strategy": "skip"}
    r1 = route_batch(data, db)
    assert r1["created_count"] == 1
    r2 = route_batch(data, db)
    assert r2["created_count"] == 0 and len(r2["skipped"]) == 1
    data2 = {**data, "conflict_strategy": "overwrite"}
    with pytest.raises(HTTPException) as ei:
        route_batch(data2, db)
    assert ei.value.status_code == 400


# ---------------------------------------------------------------- ④ correct

def test_correct_appends_and_supersedes(db):
    a = asset_service.create_asset(db, "示例-标的Z", 100, date(2026, 3, 1))
    res = asset_service.correct_asset(db, a.id, "180",
        reason="correction")
    assert res is not None
    new, change = res
    assert change["action"] == "correct" and change["before"]["amount"] == "100.00"
    # 旧行仍在且指向新行；新行 active、同日唯一有效
    old = db.get(Asset, a.id)
    assert old.superseded_by == new.id and old.amount == Decimal("100.00")
    assert new.superseded_by is None and new.amount == Decimal("180.00")
    # 净值口径取新行
    snap = asset_service.get_snapshot_assets(db, date(2026, 3, 1))
    assert [r.id for r in snap] == [new.id]
    # 同日双 active 被部分唯一索引拒绝
    dup = Asset(asset_name="示例-标的Z", asset_item_id=a.asset_item_id,
                amount=Decimal("9"), asset_date=date(2026, 3, 1), side="asset")
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    # 已 supersede 的旧行不可再更正
    with pytest.raises(ValueError):
        asset_service.correct_asset(db, old.id, "1",
        reason="correction")


# ---------------------------------------------------------------- ⑤ rename

def test_rename_keeps_history_and_old_name_resolves(db):
    a = asset_service.create_asset(db, "示例-旧名", 100, date(2026, 3, 1))
    item_id = a.asset_item_id
    _, change = asset_service.rename_item(db, item_id, "示例-新名")
    assert change["action"] == "rename"
    item = db.get(AssetItem, item_id)
    assert item.display_name == "示例-新名"
    # 留史：新旧名均入 history（旧名确保可解析），origin='rename'
    hist = db.execute(select(AssetItemName).where(AssetItemName.item_id == item_id,
                                                  AssetItemName.origin == "rename")).scalars().all()
    assert {h.name for h in hist} == {"示例-旧名", "示例-新名"}
    # 历史快照行 name 不回写（时点语义）
    a2 = db.get(Asset, a.id)
    assert a2.asset_name == "示例-旧名"
    # 旧名仍可解析回同一 item → history-by-name 命中
    assert asset_service.resolve_item_id(db, "示例-旧名") == item_id
    h = asset_service.get_asset_history_by_name(db, "示例-旧名")
    assert h["total_count"] == 1


# ---------------------------------------------------------------- ⑥ archive

def test_archive_status_and_filter(db):
    asset_service.create_asset(db, "示例-归档对象", 10, date(2026, 3, 1))
    items = asset_service.list_items(db)
    target = [i for i in items if i["display_name"] == "示例-归档对象"][0]
    _, ch = asset_service.set_item_status(db, target["id"], "archived",
                                  reason="normalization")
    # 0025：归档携带生效日，审计 after 记录 {status, archived_effective_from}
    assert ch["action"] == "archive" and ch["after"]["status"] == "archived"
    assert ch["after"]["archived_effective_from"] == date.today()
    active_only = asset_service.list_items(db, include_archived=False)
    assert target["id"] not in {i["id"] for i in active_only}
    # 非法状态拒绝
    with pytest.raises(ValueError):
        asset_service.set_item_status(db, target["id"], "deleted")
    # 状态回退
    asset_service.set_item_status(db, target["id"], "active")
    assert target["id"] in {i["id"] for i in asset_service.list_items(db, include_archived=False)}


# ---------------------------------------------------------------- ⑦ ingest conflict

def test_upsert_create_conflict_explicit_no_overwrite(db):
    from services.screenshot_service import _upsert_asset
    a = asset_service.create_asset(db, "示例-截图项", 100, date(2026, 3, 1))
    status, row, before = _upsert_asset(db, "示例-截图项", Decimal("999"), date(2026, 3, 1),
                                        source_evidence_id=None, allow_update=False)
    assert status == "conflict" and row.id == a.id
    assert row.amount == Decimal("100.00")  # 绝不静默覆盖
    # 人裁决 update 通道保持冻结契约：允许覆盖；before 回传覆盖前旧值（审计用）
    status2, row2, before2 = _upsert_asset(db, "示例-截图项", Decimal("999"), date(2026, 3, 1),
                                           source_evidence_id=None, allow_update=True)
    assert status2 == "updated" and row2.amount == Decimal("999.00")
    assert before2 == {"amount": "100.00", "side": "asset"}


def test_apply_preview_reports_conflicts(db, tmp_path):
    from services.screenshot_service import (
        apply_preview, compute_preview, ParsedItem, SOURCE_GROUPS,
        seed_default_source_groups)
    from decimal import Decimal as D
    seed_default_source_groups(db)   # 0029：apply 自建凭证行需要组行存在
    prefix = SOURCE_GROUPS["bantuou"]["asset_prefix"]
    # 同日已有 active 行（经 ledger 单点创建，含 item）；截图行未映射（known 为空）
    # → compute_preview 判 create → apply 必须显式冲突而不是覆盖
    asset_service.create_asset(db, prefix + "冲突项", D("100"), date(2026, 3, 1))
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    p = compute_preview([ParsedItem(name="冲突项", amount=D("777"))], [], prefix)
    assert p.rows[0].action == "create"
    res = apply_preview(db, p, source_group="bantuou", data_date=date(2026, 3, 1),
                        image_path=str(img),
                        evidence_root=str(tmp_path / "ev"),
                        identity_decisions=confirms(p))
    assert res.rows_written == 0 and len(res.conflicts) == 1
    assert res.conflicts[0]["reason"] == "create_on_active_same_day"
    row = db.execute(select(Asset).where(Asset.asset_name == prefix + "冲突项")).scalar_one()
    assert row.amount == D("100")  # 未被覆盖


# ---------------------------------------------------------------- 写路径挂 item

def test_all_write_paths_link_item(db):
    asset_service.create_asset(db, "示例-写A", 1, date(2026, 3, 1))
    asset_service.batch_create_assets(
        db, [{"asset_name": "示例-写B", "amount": "2",
              "asset_date": date(2026, 3, 1), "attribute_ids": []}], "skip")
    from services.screenshot_service import _upsert_asset
    _upsert_asset(db, "示例-写C", Decimal("3"), date(2026, 3, 1), None)
    rows = db.execute(select(Asset)).scalars().all()
    assert all(r.asset_item_id is not None for r in rows)
    # name 快照＝写入时 display_name
    assert {r.asset_name for r in rows} == {"示例-写A", "示例-写B", "示例-写C"}


def test_response_shape_new_fields(db):
    from routers.assets import build_asset_response
    a = asset_service.create_asset(db, "示例-响应", 5, date(2026, 3, 1))
    resp = build_asset_response(db, a)
    assert resp["asset_item_id"] == a.asset_item_id
    assert resp["display_name"] == "示例-响应"
    assert resp["asset_name"] == "示例-响应"  # name 快照保留
    # ADR-0019 契约：分类只读回显自资产项，旧 EAV 扁平字段已移除
    for old in ("fund_type_id", "account_id", "liquidity_rating_id", "attributes"):
        assert old not in resp
    for new in ("asset_class_id", "asset_class_path", "liquidity_days",
                "institution_id", "channel_id", "owner_id", "risk_level",
                "tags", "classification_missing"):
        assert new in resp
    # 新项为 draft（四项必填维度未填）→ 四维全部缺失
    assert resp["classification_missing"] == [
        "asset_class_id", "liquidity_days", "institution_id", "owner_id"]


# ---------------------------------------------------------------- 列表 active 口径

def test_list_assets_default_hides_superseded(db):
    a = asset_service.create_asset(db, "示例-列表项", 100, date(2026, 3, 1))
    new, _ = asset_service.correct_asset(db, a.id, "180",
        reason="correction")

    items, total = asset_service.list_assets(db)  # 默认仅 active
    assert {r.id for r in items} == {new.id}
    assert total == 1

    items_all, total_all = asset_service.list_assets(db, include_superseded=True)
    assert {r.id for r in items_all} == {a.id, new.id}
    assert total_all == 2


def test_list_route_include_superseded_chain_fields(db):
    from routers.assets import list_assets as route_list
    a = asset_service.create_asset(db, "示例-列表路由", 100, date(2026, 3, 1))
    new, _ = asset_service.correct_asset(db, a.id, "180",
        reason="correction")

    out = route_list(page=1, page_size=100, include_superseded=False, db=db)
    assert {i["id"] for i in out["items"]} == {new.id}
    # active 行也带链字段（向后兼容新增可空字段）
    assert out["items"][0]["superseded_by"] is None
    assert out["items"][0]["is_superseded"] is False

    out2 = route_list(page=1, page_size=100, include_superseded=True, db=db)
    by_id = {i["id"]: i for i in out2["items"]}
    assert by_id[a.id]["superseded_by"] == new.id
    assert by_id[a.id]["is_superseded"] is True
    assert by_id[new.id]["superseded_by"] is None
