"""v5 一期·步骤 1「备份修复」测试（spec §5 六条用例）。

沙箱方式：tmp_path + monkeypatch.setenv("DATABASE_PATH", ...)，
backup_service 调用时动态解析该环境变量，不触碰真实 data/。
"""
import hashlib
import json
import os
import sqlite3
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import models
from database import get_db
from models import AuditLog
from services import backup_service


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """沙箱：tmp_path 下的 DB（WAL、含一张表一行数据）+ evidence 目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "person_fin.db"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO assets (name) VALUES ('初始标的A')")
    conn.commit()
    conn.close()

    evidence = data_dir / "evidence" / "2026" / "09"
    evidence.mkdir(parents=True)
    (evidence / "shot.png").write_bytes(b"fake-png-bytes")

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    return {"data": data_dir, "db": db_path}


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM assets ORDER BY id")]
    finally:
        conn.close()


def _backup_files():
    d = backup_service.get_backup_dir()
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ① bundle 结构与 sha256 相符
def test_bundle_structure_and_sha(sandbox):
    info = backup_service.create_backup()
    assert info["filename"].endswith(".zip")
    assert info["size"] == os.path.getsize(info["path"])

    expect_zip_sha = hashlib.sha256(open(info["path"], "rb").read()).hexdigest()
    assert info["sha256"] == expect_zip_sha

    with zipfile.ZipFile(info["path"]) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "person_fin.db" in names
        assert "evidence/2026/09/shot.png" in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["evidence_count"] == 1
        assert manifest["db_sha256"] == hashlib.sha256(zf.read("person_fin.db")).hexdigest()


# ② WAL 写入中备份，解包可查得该写入（一致性）
def test_backup_is_wal_consistent(sandbox):
    # 写入但不 checkpoint：数据留在 -wal，裸拷会丢，backup() 不可丢
    conn = sqlite3.connect(sandbox["db"])
    conn.execute("INSERT INTO assets (name) VALUES ('WAL中的行B')")
    conn.commit()  # 不落 main db 文件，留在 WAL
    assert os.path.exists(sandbox["db"].with_name(sandbox["db"].name + "-wal"))

    info = backup_service.create_backup()
    conn.close()

    with zipfile.ZipFile(info["path"]) as zf:
        zf.extract("person_fin.db", sandbox["data"] / "unzip")
    extracted = sandbox["data"] / "unzip" / "person_fin.db"
    assert "WAL中的行B" in _rows(extracted)


# ③ 改库→恢复→数据回退且 rollback bundle 生成（含删 -wal/-shm）
def test_restore_rolls_back_and_creates_rollback(sandbox):
    info = backup_service.create_backup()

    conn = sqlite3.connect(sandbox["db"])
    conn.execute("INSERT INTO assets (name) VALUES ('恢复前应消失的行')")
    conn.commit()
    conn.close()

    result = backup_service.restore_database(info["path"])
    assert result["kind"] == "bundle"
    assert _rows(sandbox["db"]) == ["初始标的A"]

    # rollback bundle 生成且含 evidence
    rollbacks = [f for f in _backup_files() if f.startswith("rollback_")]
    assert rollbacks
    with zipfile.ZipFile(os.path.join(backup_service.get_backup_dir(), rollbacks[0])) as zf:
        assert "evidence/2026/09/shot.png" in zf.namelist()

    # 替换后不得残留旧 WAL/SHM（漏删会污染新库）
    assert not os.path.exists(str(sandbox["db"]) + "-wal")
    assert not os.path.exists(str(sandbox["db"]) + "-shm")


def test_evidence_merge_only_adds(sandbox):
    info = backup_service.create_backup()
    target = sandbox["data"] / "evidence" / "2026" / "09" / "shot.png"
    target.write_bytes(b"current-marker")  # 模拟恢复后新写入的凭证
    marker = sandbox["data"] / "evidence" / "2026" / "09" / "newer.png"
    marker.write_bytes(b"marker-not-in-bundle")

    backup_service.restore_database(info["path"])
    # 只增不删不覆盖
    assert target.read_bytes() == b"current-marker"
    assert marker.exists()


# ④ 裸 .db 兼容恢复
def test_legacy_bare_db_restore(sandbox):
    legacy = sandbox["data"] / "legacy.db"
    src = sqlite3.connect(sandbox["db"])
    dst = sqlite3.connect(legacy)
    src.backup(dst)
    src.close()
    dst.close()

    conn = sqlite3.connect(sandbox["db"])
    conn.execute("INSERT INTO assets (name) VALUES ('将被回退的行')")
    conn.commit()
    conn.close()

    result = backup_service.restore_database(str(legacy))
    assert result["kind"] == "legacy_db"
    assert any("不含凭证" in w for w in result["warnings"])
    assert _rows(sandbox["db"]) == ["初始标的A"]


# ⑤ prune 保 N 且不删 rollback_*
def test_prune_keeps_n_and_never_rollback(sandbox):
    rollback_name = "rollback_before_restore_99999999_999999.zip"
    (sandbox["data"] / "backup" / rollback_name).parent.mkdir(parents=True, exist_ok=True)
    (sandbox["data"] / "backup" / rollback_name).write_bytes(b"x")

    for _ in range(7):
        backup_service.create_backup(keep=3)

    zips = [f for f in _backup_files() if f.startswith("backup_") and f.endswith(".zip")]
    assert len(zips) == 3
    assert rollback_name in _backup_files()


# ⑥ 坏包/坏 db 拒绝、现库不动
def test_bad_bundle_rejected_current_db_untouched(sandbox):
    # 坏 zip
    bogus = sandbox["data"] / "bogus.zip"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        backup_service.restore_database(str(bogus))

    # manifest 缺失
    no_manifest = sandbox["data"] / "no_manifest.zip"
    with zipfile.ZipFile(no_manifest, "w") as zf:
        zf.write(sandbox["db"], "person_fin.db")
    with pytest.raises(ValueError):
        backup_service.restore_database(str(no_manifest))

    # db_sha256 不符（篡改包内 db）
    tampered = sandbox["data"] / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format_version": 1, "created_at": "x",
            "db_sha256": "0" * 64, "evidence_count": 0,
        }))
        zf.write(sandbox["db"], "person_fin.db")
    with pytest.raises(ValueError):
        backup_service.restore_database(str(tampered))

    # 坏 db（sha 相符但内容不是 SQLite）
    bad_db = sandbox["data"] / "bad_db.zip"
    payload = b"definitely-not-sqlite"
    with zipfile.ZipFile(bad_db, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format_version": 1, "created_at": "x",
            "db_sha256": hashlib.sha256(payload).hexdigest(), "evidence_count": 0,
        }))
        zf.writestr("person_fin.db", payload)
    with pytest.raises(ValueError):
        backup_service.restore_database(str(bad_db))

    # 现库未被动过
    assert _rows(sandbox["db"]) == ["初始标的A"]
    assert not [f for f in _backup_files() if f.startswith("rollback_")]


# 步骤 2 接口预留：create_backup 签名（迁移 runner 用法 pre_migration + prune=False）
def test_create_backup_signature_for_migration_runner(sandbox):
    before = set(_backup_files())
    info = backup_service.create_backup(reason="pre_migration", prune=False)
    assert {"path", "sha256", "size"} <= set(info)
    added = set(_backup_files()) - before
    assert len(added) == 1  # prune=False：不删任何旧包


# 薄封装保持旧签名
def test_thin_wrappers(sandbox):
    legacy_info = backup_service.create_full_backup()
    assert legacy_info["filename"].endswith(".zip")

    p = backup_service.create_backup_before_import("my data.csv")
    assert os.path.exists(p) and p.endswith(".zip")


# =====================================================================
# 删除端点的 HTTP 状态语义（spec `.scratch/backup-delete-404/spec.md` §3）
#
# 不变量：已定语义的异常不得被 `except Exception` 兜底改写。
#   文件不存在        → 404 + 零审计行（没东西离场，不造假痕迹）
#   os.remove 失败     → 500 + 审计行保留（「尝试删过」是事实）+ 文案含「删除失败」
# =====================================================================

@pytest.fixture()
def delete_api(tmp_path, monkeypatch):
    """删备份端点的 HTTP 沙箱：文件库（全 schema）+ DATABASE_PATH 注入 + 最小应用。

    用真实 HTTP 层（TestClient）而非直接调路由函数，锁的是客户端实际看到的状态码。
    """
    from main import register_exception_handlers
    from routers import backup as backup_router
    from routers import imports as imports_router

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "person_fin.db"
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(backup_router.router, prefix="/api/backup")
    app.include_router(imports_router.router, prefix="/api/imports")
    try:
        yield {"client": TestClient(app), "db": db, "data": data_dir}
    finally:
        db.close()
        engine.dispose()


def _put_fake_backup(data_dir, name="backup_20260101_000000.zip"):
    """在沙箱备份目录放一份假备份（删备份只关心文件壳，不需要真 zip）。"""
    backup_dir = data_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    path.write_bytes(b"fake-bundle-bytes")
    return path


def _delete_audits(db):
    return list(db.execute(
        select(AuditLog)
        .where(AuditLog.entity == "backup", AuditLog.action == "delete")
        .order_by(AuditLog.id)).scalars().all())


def test_delete_missing_returns_404_and_leaves_no_audit(delete_api):
    """① 删不存在的文件 → 404，且零审计行（404 不得被兜底吞成 500）。"""
    resp = delete_api["client"].delete("/api/backup/delete/backup_不存在.zip")

    assert resp.status_code == 404, resp.text
    assert "不存在" in resp.json()["error"]["message"]
    assert _delete_audits(delete_api["db"]) == []


def test_delete_existing_returns_success_and_one_audit(delete_api):
    """② 删存在的文件 → 成功（200），新增恰好 1 条 backup/delete。"""
    path = _put_fake_backup(delete_api["data"])

    # 业主 2026-11-08 裁决：删备份**不索取理由**，服务端归一化为
    # normalization + after.source=manual_cleanup（不传任何 query 也 200）
    resp = delete_api["client"].delete(f"/api/backup/delete/{path.name}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert not path.exists()
    rows = _delete_audits(delete_api["db"])
    assert len(rows) == 1
    assert json.loads(rows[0].before_json)["filename"] == path.name
    assert rows[0].reason == "normalization"
    assert json.loads(rows[0].after_json) == {"source": "manual_cleanup"}


def test_delete_remove_failure_returns_500_and_keeps_audit(delete_api, monkeypatch):
    """③ os.remove 失败（审计已提交）→ 500，审计行保留，文案含「删除失败」。"""
    path = _put_fake_backup(delete_api["data"], "backup_20260102_000000.zip")
    real_remove = os.remove

    def boom(target, *args, **kwargs):
        if os.path.basename(str(target)) == path.name:
            raise OSError("模拟磁盘只读")
        return real_remove(target, *args, **kwargs)

    monkeypatch.setattr(backup_service.os, "remove", boom)

    resp = delete_api["client"].delete(f"/api/backup/delete/{path.name}")

    assert resp.status_code == 500, resp.text
    assert "删除失败" in resp.json()["error"]["message"]
    assert path.exists()                       # 文件没删成
    rows = _delete_audits(delete_api["db"])
    assert len(rows) == 1                      # 但「尝试删过」已留痕
    assert json.loads(rows[0].before_json)["existed"] is True


def test_imports_delete_missing_returns_404(delete_api):
    """④ `DELETE /api/imports/backups/{filename}` 不存在 → 404（同 S2）。"""
    resp = delete_api["client"].delete("/api/imports/backups/backup_不存在.zip")

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["error"]["message"].lower()
    assert _delete_audits(delete_api["db"]) == []
