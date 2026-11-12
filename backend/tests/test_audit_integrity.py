"""库外锚对账（整库回退检测）测试。

背景：`GET /api/audit-log/chain/verify` 的七种判定全在**同一个库内**取证，整库被恢复
成旧版本时 `audit_log`／`audit_chain_state`／`audit_chain_seals` 一起变旧，自证仍然
通过。本模块把备份 `manifest.audit_head`（库外锚）与库内链头比对，把「整库回退」变成
可判定的事实。本文件覆盖：

1. `compare()` 五判定（含无 audit_head → unknown、行数相等 hash 不同 → diverged）；
2. `integrity_report()` 的 seal lag（无封存 / 已封存 / 封存后又增长）；
3. `GET /api/audit-log/integrity` 的结构，且响应里**不含绝对路径**；
4. 启动检查：`db_older` 只记 CRITICAL、绝不抛异常（任何异常都不阻止启动）；
5. 恢复拒绝：目标包链头更旧 → 409 `AUDIT_ROLLBACK_DETECTED`，**库未被替换**；
6. 恢复越过：`confirm_rollback=true` 放行，新库留痕带 `integrity`/`rollback_confirmed`；
7. 目标包无 `audit_head`（缺键 / null）→ `unknown` 放行，恢复照旧成功。

沙箱：`tmp_path` + `DATABASE_PATH` 注入（备份服务按该变量解析 data/backup）；
事件时间线与金额全部合成，**不碰真实 `deploy/backups` 或 `data/`**。
"""
import asyncio
import json
import logging
import os
import sqlite3
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  全表注册
from database import Base
from errors import ApiError, ErrorCode
from services import audit_chain, audit_service, backup_service
from services import audit_integrity_service as integrity

HASH_A = "a" * 64
HASH_B = "b" * 64


# ---------------------------------------------------------------- 助手


def _manifest(last_hash, rows, *, created_at="2026-11-07T10:00:00", with_head=True):
    """合成 manifest（字段名照 `backup_service._build_bundle()` 的实际形状）。"""
    payload = {
        "format_version": 1,
        "created_at": created_at,
        "db_sha256": "0" * 64,
        "evidence_count": 0,
        "reason": "manual",
        "generation": 5,
    }
    if with_head:
        payload["audit_head"] = {"algorithm": "sha256.v1", "last_id": rows,
                                 "last_hash": last_hash, "row_count": rows}
    return payload


def _write_manifest(directory, name, payload):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _seed(db, n, start=1):
    """写 n 条审计行（entity 走已登记值，金额是合成数）。"""
    for i in range(n):
        audit_service.record(db, audit_service.make_change(
            "asset_snapshot", start + i, "create", after={"amount": "100.00"}))
    db.commit()


def _audit_count(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        conn.close()


def _last_restore_payload(db_path):
    """取新库里最后一条 `backup/restore` 审计行的 after（JSON → dict）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT after_json FROM audit_log WHERE entity='backup'"
            " AND action='restore' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None, "新库里没有 backup/restore 留痕"
    return json.loads(row[0])


def _rewrite_manifest(zip_path, mutate):
    """改写 bundle 内嵌 manifest（只动清单，不改库字节 → db_sha256 仍成立）。"""
    with zipfile.ZipFile(zip_path) as zf:
        members = [(info.filename, zf.read(info.filename)) for info in zf.infolist()]
    out = []
    for name, data in members:
        if name == backup_service.MANIFEST_NAME:
            manifest = json.loads(data.decode("utf-8"))
            mutate(manifest)
            data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        out.append((name, data))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in out:
            zf.writestr(name, data)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """tmp_path 下的文件库（全 schema）＋ DATABASE_PATH 注入。"""
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "person_fin.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    try:
        yield {"data": data, "db_path": db_path, "db": db}
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------- 1. compare 五判定


def test_compare_five_statuses():
    current = {"last_hash": HASH_A, "row_count": 5, "max_id": 5}

    # ① 锚里没有 audit_head（老包/库里没有审计表）→ unknown，不臆断
    assert integrity.compare(current, integrity.Anchor("m.json", None, None, None)).status == "unknown"
    # ② 链头一致 → match
    verdict = integrity.compare(current, integrity.Anchor("m.json", "t", HASH_A, 3))
    assert (verdict.status, verdict.detail) == ("match", "当前库链头与锚一致")
    # ③ 当前库行数更多 → db_newer（正常：锚是旧的）
    assert integrity.compare(current, integrity.Anchor("m.json", "t", HASH_B, 3)).status == "db_newer"
    # ④ 当前库行数更少 → db_older（回退嫌疑）
    assert integrity.compare(current, integrity.Anchor("m.json", "t", HASH_B, 9)).status == "db_older"
    # ⑤ 行数相同、链头不同 → diverged（分叉嫌疑）
    assert integrity.compare(current, integrity.Anchor("m.json", "t", HASH_B, 5)).status == "diverged"

    # 锚有 hash 却没有行数 → 只能说不知道，不许猜
    assert integrity.compare(current, integrity.Anchor("m.json", "t", HASH_B, None)).status == "unknown"


def test_anchor_from_manifest_shapes():
    anchor = integrity.anchor_from_manifest(_manifest(HASH_A, 7), "backup_x.zip")
    assert (anchor.source, anchor.audit_head, anchor.audit_rows) == ("backup_x.zip", HASH_A, 7)
    assert anchor.generated_at == "2026-11-07T10:00:00"

    no_head = integrity.anchor_from_manifest(_manifest(HASH_A, 7, with_head=False), "old.zip")
    assert (no_head.audit_head, no_head.audit_rows) == (None, None)


def test_read_anchors_scans_json_and_zip_and_sorts(tmp_path):
    backups = tmp_path / "backup"
    backups.mkdir()
    _write_manifest(backups, "manifest_new.json", _manifest(HASH_A, 9, created_at="2026-11-07T12:00:00"))
    _write_manifest(backups, "manifest_old.json", _manifest(HASH_B, 3, created_at="2026-11-01T08:00:00"))
    bundle = backups / "backup_20261106_120000.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr(backup_service.MANIFEST_NAME,
                    json.dumps(_manifest(HASH_A, 4, created_at="2026-11-06T09:00:00")))
    (backups / "backup_broken.zip").write_bytes(b"not-a-zip")
    (backups / "manifest_broken.json").write_text("{not json", encoding="utf-8")
    (backups / "unrelated.json").write_text("{}", encoding="utf-8")

    anchors = integrity.read_anchors(str(backups))
    assert [a.source for a in anchors] == [
        "manifest_new.json", "backup_20261106_120000.zip", "manifest_old.json"]
    # 只给相对文件名，绝不给绝对路径（公开安全红线）
    assert all(str(tmp_path) not in a.source for a in anchors)


# ---------------------------------------------------------------- 2. seal lag


def test_integrity_report_seal_lag(tmp_path, db):
    empty_backups = str(tmp_path / "no-backups")
    _seed(db, 4)

    report = integrity.integrity_report(db, empty_backups)
    assert report["seals"] == {"last_seal_id": None, "max_id": 4, "lag": 4}
    assert report["anchors"] == []
    assert report["worst_status"] == "unknown"      # 没有锚＝没对过账，不报平安
    assert report["head"]["row_count"] == 4 and report["head"]["max_id"] == 4

    audit_chain.seal(db)
    db.commit()
    report = integrity.integrity_report(db, empty_backups)
    assert report["seals"] == {"last_seal_id": 4, "max_id": 4, "lag": 0}

    _seed(db, 2, start=10)                           # 封存之后继续增长
    report = integrity.integrity_report(db, empty_backups)
    assert report["seals"] == {"last_seal_id": 4, "max_id": 6, "lag": 2}


# ---------------------------------------------------------------- 3. 只读端点


def test_integrity_endpoint_shape_and_no_absolute_path(tmp_path, db, monkeypatch):
    data = tmp_path / "data"
    backups = data / "backup"
    monkeypatch.setenv("DATABASE_PATH", str(data / "person_fin.db"))
    _write_manifest(backups, "manifest_20261107.json", _manifest(HASH_B, 7))
    _seed(db, 3)                                     # 3 行 < 锚 7 行 → db_older

    from routers.audit_log import chain_integrity

    payload = chain_integrity(db=db)
    assert set(payload) == {"head", "seals", "anchors", "worst_status", "checked_at"}
    assert payload["worst_status"] == "db_older"
    assert payload["head"] == {"last_hash": audit_chain.head(db)["last_hash"],
                               "row_count": 3, "max_id": 3}
    assert len(payload["anchors"]) == 1
    anchor = payload["anchors"][0]
    assert anchor["source"] == "manifest_20261107.json"
    assert anchor["status"] == "db_older"
    assert anchor["audit_rows"] == 7

    text = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in text                  # 响应里不得出现绝对路径
    assert "manifest_20261107.json" in text

    # 路由已挂载，且必须排在 `/{log_id}` 之前（否则 "integrity" 会被当行号）
    from main import app
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.index("/api/audit-log/integrity") < paths.index("/api/audit-log/{log_id}")


# ---------------------------------------------------------------- 4. 启动检查


def test_startup_check_logs_critical_and_never_raises(tmp_path, db, caplog):
    backups = tmp_path / "backup"
    _write_manifest(backups, "manifest_old.json", _manifest(HASH_B, 99))
    _seed(db, 1)                                     # 当前 1 行 < 锚 99 行

    with caplog.at_level(logging.CRITICAL, logger="services.audit_integrity_service"):
        report = integrity.startup_check(db, str(backups))

    assert report["worst_status"] == "db_older"
    critical = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert critical, "回退嫌疑必须留下 CRITICAL"
    assert "audit_integrity_suspect" in critical[0].getMessage()
    assert "db_older" in critical[0].getMessage()
    assert str(tmp_path) not in caplog.text           # 日志里不得出现绝对路径

    # 异常一律不外泄（单用户系统没人值班，锁死门外比漏报更糟）
    assert integrity.startup_check(object(), str(backups)) is None


# ---------------------------------------------------------------- 5. 恢复拒绝


def test_restore_rejects_older_target(sandbox):
    _seed(sandbox["db"], 2)
    old_bundle = backup_service.create_backup()
    _seed(sandbox["db"], 3, start=10)                # 当前库 5 行
    assert _audit_count(sandbox["db_path"]) == 5

    backup_dir = backup_service.get_backup_dir()
    files_before = set(os.listdir(backup_dir))

    with pytest.raises(ApiError) as excinfo:
        backup_service.restore_database(old_bundle["path"])

    error = excinfo.value
    assert error.code == ErrorCode.AUDIT_ROLLBACK_DETECTED
    assert error.status_code == 409
    assert error.details["status"] == "db_older"
    assert error.details["current_rows"] == 5
    assert error.details["target_rows"] == 2
    assert len(error.details["current_head_prefix"]) == 8
    assert len(error.details["target_head_prefix"]) == 8
    assert str(sandbox["data"]) not in error.message   # 消息里不得出现绝对路径

    # 库一个字节都没动：行数不变、也没留下 rollback bundle
    assert _audit_count(sandbox["db_path"]) == 5
    leftovers = [name for name in set(os.listdir(backup_dir)) - files_before
                 if name.startswith("rollback_")]
    assert leftovers == []


def test_restore_endpoint_propagates_409(sandbox):
    """路由层透传：ApiError 不被兜底成 500，而是 409 信封。"""
    _seed(sandbox["db"], 2)
    old_bundle = backup_service.create_backup()
    _seed(sandbox["db"], 3, start=10)

    from routers.backup import restore_backup

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(restore_backup(filename=old_bundle["filename"], file=None,
                                   confirm_rollback=False))
    assert excinfo.value.code == ErrorCode.AUDIT_ROLLBACK_DETECTED
    assert excinfo.value.status_code == 409
    assert _audit_count(sandbox["db_path"]) == 5


# ---------------------------------------------------------------- 6. 恢复越过


def test_restore_confirm_rollback_writes_integrity(sandbox):
    _seed(sandbox["db"], 2)
    old_bundle = backup_service.create_backup()
    _seed(sandbox["db"], 3, start=10)

    result = backup_service.restore_database(old_bundle["path"], confirm_rollback=True)
    assert result["kind"] == "bundle"

    # 目标包 2 行 + 本次留痕 1 行 = 3 行（证明真的换库了）
    assert _audit_count(sandbox["db_path"]) == 3
    payload = _last_restore_payload(sandbox["db_path"])
    assert payload["integrity"] == "db_older"
    assert payload["rollback_confirmed"] is True
    assert payload["chain_rows_before"] == 5          # 换库前旧库行数仍在留痕里
    assert payload["chain_head_target"] == _bundle_head(old_bundle["path"])


def _bundle_head(bundle_path):
    """读 bundle manifest 里的链头 hash（对账留痕断言用）。"""
    with zipfile.ZipFile(bundle_path) as zf:
        manifest = json.loads(zf.read(backup_service.MANIFEST_NAME))
    return manifest["audit_head"]["last_hash"]


# ---------------------------------------------------------------- 7. 无锚放行


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda manifest: manifest.pop("audit_head", None), id="missing-key"),
    pytest.param(lambda manifest: manifest.update(audit_head=None), id="null-head"),
])
def test_restore_allows_target_without_audit_head(sandbox, mutate):
    """目标包没有审计链头 → unknown（无法判定）→ 放行，恢复照旧成功。

    注：连 `manifest.json` 都没有的包在更早的坏包校验里就被 `ValueError` 拒了，
    不属于「对账放行」的范围（那是包完整性，不是回退判定）。
    """
    _seed(sandbox["db"], 2)
    old_bundle = backup_service.create_backup()
    _seed(sandbox["db"], 3, start=10)
    _rewrite_manifest(old_bundle["path"], mutate)

    result = backup_service.restore_database(old_bundle["path"])
    assert result["kind"] == "bundle"
    assert _audit_count(sandbox["db_path"]) == 3      # 2 + 1 留痕

    payload = _last_restore_payload(sandbox["db_path"])
    assert payload["integrity"] == "unknown"
    assert payload["rollback_confirmed"] is False
    assert payload["chain_head_target"] is None