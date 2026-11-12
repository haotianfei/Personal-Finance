"""留痕覆盖率：离场桶·备份文件（ADR-0027 D5）——三个端点的**行为**锁。

契约表里的 entity/action 是冻结值（`rule_for()`），本文件锁的是「真的留了痕」：

1. `POST /api/backup/create` → 1 条 `backup/create`，`after` **只有壳**
   （filename / size_bytes / sha256 前 16 位 / source / db_schema_version），
   文件内容、库字节、口令一律不进审计；
2. `DELETE /api/backup/delete/{filename}` 与 `DELETE /api/imports/backups/{filename}`
   → 各 1 条 `backup/delete`，`before` 只有壳（filename / size_bytes / mtime /
   existed）、`after=NULL`；
3. 文件不存在 → 幂等返回、**零审计行**（没有东西离开系统，不造假的删痕）；
4. 顺序语义（文件系统与 DB 不同事务，只能选一边先）：
   - create：**先造文件、成功后写审计行** → 审计失败则端点抛错、零审计行、文件已落盘
     （缺口可见，可再用删除端点补留痕）；
   - delete：**先写审计行并提交、再删文件** → 审计失败则端点抛错、文件仍在、零审计行
     （宁可没删成，也不允许「文件没了但无痕」）。
5. 这些写入之后 `audit_chain.verify()` 仍 ok（链能续接）。

沙箱：`tmp_path` 文件库（`Base.metadata.create_all` 建全 schema）＋ `DATABASE_PATH`
注入（备份服务按该变量解析 data/backup 目录）。**不触真实 `data/` 或 `deploy/data/`**。
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

import models
from models import AuditLog
from services import audit_chain, backup_service
from services.audit_contract import rule_for

# 审计行允许出现的壳字段（白名单）——离场文件的内容一律不许进审计。
_CREATE_SHELL = {"filename", "size_bytes", "sha256", "source", "db_schema_version"}
_DELETE_SHELL = {"filename", "size_bytes", "mtime", "existed"}
_CONTENT_KEYS = {"content", "db_bytes", "sqlite", "body", "text", "data", "preview",
                 "rows", "sample", "bytes", "payload", "password", "secret", "token"}


# ---------------------------------------------------------------- 助手

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """沙箱：tmp_path 下的文件库（含审计表 + schema_version 记账表）。"""
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "person_fin.db"
    engine = create_engine(f"sqlite:///{db_path}")
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # 迁移 runner 的记账表（裸 SQL 自建，不入 create_all）：留痕壳字段要读它
    db.execute(text(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT,"
        " baseline INTEGER, applied_at TEXT)"))
    db.execute(text("INSERT INTO schema_version VALUES (34, '示例迁移', 0, '2026-11-01')"))
    db.execute(text("INSERT INTO schema_version VALUES (36, '示例迁移2', 0, '2026-11-07')"))
    db.commit()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    try:
        yield {"data": data, "db_path": db_path, "db": db}
    finally:
        db.close()
        engine.dispose()


def _audits(db, entity, action=None):
    q = select(AuditLog).where(AuditLog.entity == entity)
    if action is not None:
        q = q.where(AuditLog.action == action)
    return list(db.execute(q.order_by(AuditLog.id)).scalars().all())


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def _after(row):
    return json.loads(row.after_json) if row.after_json else None


def _before(row):
    return json.loads(row.before_json) if row.before_json else None


def _fake_backup_file(name="backup_20260101_000000.zip", content=b"fake-bundle-bytes"):
    """在沙箱备份目录放一份假备份（删除类用例只关心文件壳，不需要真 zip）。"""
    backup_dir = Path(backup_service.get_backup_dir())
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    path.write_bytes(content)
    return path


def _backup_zip_count() -> int:
    return len(list(Path(backup_service.get_backup_dir()).glob("*.zip")))


# ---------------------------------------------------------------- 契约冻结值

FROZEN_ROWS = [
    ("POST", "/api/backup/create", "backup", "create"),
    ("DELETE", "/api/backup/delete/{filename}", "backup", "delete"),
    ("DELETE", "/api/imports/backups/{filename}", "backup", "delete"),
]


@pytest.mark.parametrize("method,path,entity,action", FROZEN_ROWS)
def test_contract_rows_are_frozen(method, path, entity, action):
    """三行留痕的 entity/action 必须与契约表逐字一致（冻结值，不自创）。"""
    rule = rule_for(method, path)
    assert rule is not None, f"{method} {path} 未登记进契约表"
    assert (rule.entity, rule.action) == (entity, action)


# ---------------------------------------------------------------- 1. create：只记壳

def test_create_leaves_shell_only_audit(sandbox):
    db = sandbox["db"]
    info = backup_service.create_full_backup(db, source="manual")

    rows = _audits(db, "backup", "create")
    assert len(rows) == 1
    row = rows[0]
    assert row.before_json is None                 # create 无 before（D5）
    assert row.row_id is None                      # 文件不是账本行，没有行号可指

    payload = _after(row)
    assert set(payload) == _CREATE_SHELL           # 只有壳，键集就是白名单
    assert not set(payload) & _CONTENT_KEYS        # 不记文件内容/库字节/口令
    assert payload["filename"] == info["filename"]
    assert payload["size_bytes"] == info["size"] == os.path.getsize(info["path"])
    assert isinstance(payload["size_bytes"], int)
    assert payload["sha256"] == info["sha256"][:16]     # 只记前 16 位
    assert len(payload["sha256"]) == 16
    assert info["sha256"].startswith(payload["sha256"])
    assert payload["source"] == "manual"
    assert payload["db_schema_version"] == 36           # 读 schema_version 记账表
    assert os.path.exists(info["path"])                 # 文件确实造出来了

    # 只对账一次：审计行数就是 1（没有批次头/明细之类多余行）
    assert _count(db) == 1


def test_create_audit_failure_keeps_file_but_no_audit_row(sandbox, monkeypatch):
    """顺序＝先造文件、后留痕：审计写失败 → 抛错、零审计行、文件已落盘。"""
    db = sandbox["db"]

    def boom(*args, **kwargs):
        raise RuntimeError("审计链路不可用")

    monkeypatch.setattr(backup_service.audit_service, "record", boom)
    with pytest.raises(RuntimeError):
        backup_service.create_full_backup(db, source="manual")
    db.rollback()

    assert _count(db) == 0                    # 没有假痕迹
    assert _backup_zip_count() == 1           # 副本已落盘（缺口在响应里已报错）


def test_create_rejects_unknown_source(sandbox):
    """source 只认 manual/auto/import；非法值在写审计前就被拒。"""
    db = sandbox["db"]
    info = backup_service.create_backup(reason="manual", prune=False)
    with pytest.raises(ValueError):
        backup_service.audit_backup_created(db, info, source="示例非法来源")
    db.rollback()
    assert _count(db) == 0


# ---------------------------------------------------------------- 2. delete：只记壳

def test_delete_leaves_before_shell_only(sandbox):
    db = sandbox["db"]
    path = _fake_backup_file()
    stat = os.stat(path)                       # 删之前取壳（删后 stat 就没了）
    expect = {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "existed": True,
    }

    assert backup_service.delete_backup(path.name, db=db) is True

    rows = _audits(db, "backup", "delete")
    assert len(rows) == 1
    row = rows[0]
    # 业主 2026-11-08 裁决：删备份不索取理由 → 服务端 normalization + 触发源
    assert row.reason == "normalization" and row.actor == "owner"
    assert _after(row) == {"source": "manual_cleanup"}
    assert _before(row) == expect
    assert set(_before(row)) == _DELETE_SHELL
    assert not set(_before(row)) & _CONTENT_KEYS
    assert not path.exists()                   # 审计落库后才删文件
    assert _count(db) == 1


def test_delete_missing_file_leaves_no_audit(sandbox):
    """没有东西离开系统 → 幂等返回、零审计行（服务层与路由层同一语义）。"""
    db = sandbox["db"]
    assert backup_service.delete_backup("backup_不存在.zip", db=db) is False
    assert _count(db) == 0

    from routers.imports import delete_backup as route_imports_delete
    with pytest.raises(HTTPException) as ei:
        route_imports_delete("backup_不存在.zip", db)
    assert ei.value.status_code == 404
    assert _count(db) == 0


def test_delete_audit_failure_keeps_file(sandbox, monkeypatch):
    """顺序＝先审计、后删文件：审计写失败 → 抛错、文件仍在、零审计行。"""
    db = sandbox["db"]
    path = _fake_backup_file("backup_20260103_000000.zip")

    def boom(*args, **kwargs):
        raise RuntimeError("审计链路不可用")

    monkeypatch.setattr(backup_service.audit_service, "record", boom)
    with pytest.raises(RuntimeError):
        backup_service.delete_backup(path.name, db=db)
    db.rollback()

    assert path.exists()                       # 宁可不删，也不许「文件没了但无痕」
    assert _count(db) == 0


# ---------------------------------------------------------------- 3. 三个端点接线

def test_all_three_endpoints_leave_audit(sandbox):
    """走路由函数（不绕过端点）：create 一条 + 两个 delete 各一条。"""
    db = sandbox["db"]
    from routers.backup import create_backup as route_backup_create
    from routers.backup import delete_backup as route_backup_delete
    from routers.imports import delete_backup as route_imports_delete

    out = route_backup_create(db)
    assert out["success"] is True
    creates = _audits(db, "backup", "create")
    assert len(creates) == 1
    created_name = _after(creates[0])["filename"]

    route_backup_delete(created_name, db)                 # DELETE /api/backup/delete/{filename}
    assert not (Path(backup_service.get_backup_dir()) / created_name).exists()

    import_backup = _fake_backup_file("backup_20260104_000000.zip")
    route_imports_delete(import_backup.name, db)          # DELETE /api/imports/backups/{filename}
    assert not import_backup.exists()

    deletes = _audits(db, "backup", "delete")
    assert len(deletes) == 2
    assert {_before(r)["filename"] for r in deletes} == {
        created_name, import_backup.name}
    assert all(_before(r)["existed"] is True for r in deletes)
    assert _count(db) == 3


# ---------------------------------------------------------------- 4. 链仍可续

def test_audit_chain_still_verifies(sandbox):
    db = sandbox["db"]
    info = backup_service.create_full_backup(db, source="manual")
    backup_service.delete_backup(info["filename"], db=db)

    out = audit_chain.verify(db)
    assert out["ok"] is True, out
    assert out["first_broken"] is None
    assert out["unchained_rows"] == 0
    assert out["head"]["row_count"] == _count(db) == 2