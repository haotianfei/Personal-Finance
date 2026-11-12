import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import text

import database
from database import DB_PATH
from errors import ApiError, ErrorCode
from services import audit_integrity_service, audit_service

logger = structlog.get_logger(__name__)

# 兼容历史引用：模块级常量（调用时路径请以 _db_path()/get_backup_dir() 等函数为准，
# 函数按当前 DATABASE_PATH 环境变量解析，便于测试沙箱注入）。
DATA_DIR = os.path.dirname(DB_PATH)
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
AUTO_EXPORT_DIR = os.path.join(DATA_DIR, "auto-export")

# bundle 归档格式常量
MANIFEST_NAME = "manifest.json"
BUNDLE_DB_NAME = "person_fin.db"
BUNDLE_EVIDENCE_PREFIX = "evidence/"
SUPPORTED_FORMAT_VERSION = 1
DEFAULT_KEEP = 5  # 服务端保留份数（rollback_* 永不删）
# 数据世代（V5）；与 meta_service.SCHEMA_GENERATION 同源，步骤 5 起写入 manifest
SCHEMA_GENERATION = 5
# 可再生缓存目录，不入包（即便出现在 evidence 下也排除）
_EXCLUDE_TOP_DIRS = {"ocr_cache", "verification_cache"}


def _db_path() -> str:
    """调用时解析数据库路径（DATABASE_PATH 环境变量优先，与 database.py 一致）。"""
    return os.environ.get("DATABASE_PATH") or DB_PATH


def _data_dir() -> str:
    return os.path.dirname(_db_path())


def get_backup_dir() -> str:
    return os.path.join(_data_dir(), "backup")


def get_auto_export_dir() -> str:
    return os.path.join(_data_dir(), "auto-export")


def _evidence_dir() -> str:
    return os.path.join(_data_dir(), "evidence")


def ensure_backup_dir():
    """确保备份目录存在"""
    backup_dir = get_backup_dir()
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    return backup_dir


def ensure_auto_export_dir():
    """确保自动导出目录存在"""
    export_dir = get_auto_export_dir()
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    return export_dir


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_filename(prefix: str, suffix: str) -> str:
    """backup_/rollback_ 命名：backup_YYYYmmdd_HHMMSS[_n].zip（同秒冲突递增序号）。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{prefix}{timestamp}{suffix}"
    i = 1
    while os.path.exists(os.path.join(get_backup_dir(), name)):
        name = f"{prefix}{timestamp}_{i}{suffix}"
        i += 1
    return name


def _snapshot_db(dst_path: str):
    """SQLite 在线一致快照（WAL 安全、不停服务）：stdlib Connection.backup()。"""
    db_path = _db_path()
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _iter_evidence_files(evidence_root: str):
    """遍历凭证文件，产出 (绝对路径, 相对路径)。排除可再生缓存目录。"""
    if not os.path.isdir(evidence_root):
        return
    for dirpath, dirnames, filenames in os.walk(evidence_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_TOP_DIRS]
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, evidence_root).replace(os.sep, "/")
            if rel.split("/")[0] in _EXCLUDE_TOP_DIRS:
                continue
            yield full, rel


def _audit_head(snap_db_path: str) -> dict | None:
    """备份外锚：从 DB 快照读链头（缺表/未初始化 → null；加性字段）。"""
    try:
        conn = sqlite3.connect(snap_db_path)
        try:
            row = conn.execute(
                "SELECT algorithm, last_id, last_hash, row_count"
                " FROM audit_chain_state WHERE id=1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"algorithm": row[0], "last_id": row[1], "last_hash": row[2],
            "row_count": row[3]}


def _build_bundle(prefix: str, reason: str, include_evidence: bool) -> dict:
    """构造单一 zip bundle：manifest.json + 在线备份 DB + evidence/（可选）。
    先写 .tmp 再 os.replace 原子改名。"""
    ensure_backup_dir()
    tmp_zip_path = None
    try:
        with tempfile.TemporaryDirectory(prefix="bundle_") as tmp_dir:
            # 1) DB 一致快照先落临时文件
            snap_db = os.path.join(tmp_dir, BUNDLE_DB_NAME)
            _snapshot_db(snap_db)
            db_sha = _sha256_file(snap_db)

            # 2) evidence 清单
            evidence_files = list(_iter_evidence_files(_evidence_dir())) if include_evidence else []

            # 3) manifest（世代位：V5 收官自步骤 5 起填 5，见 meta_service.SCHEMA_GENERATION 同源）
            manifest = {
                "format_version": SUPPORTED_FORMAT_VERSION,
                "created_at": datetime.now().isoformat(),
                "db_sha256": db_sha,
                "evidence_count": len(evidence_files),
                "reason": reason,
                "generation": SCHEMA_GENERATION,
                "audit_head": _audit_head(snap_db),
            }

            # 4) zip 写 .tmp 后原子改名
            final_name = _unique_filename(prefix, ".zip")
            final_path = os.path.join(get_backup_dir(), final_name)
            tmp_zip_path = final_path + ".tmp"
            with zipfile.ZipFile(tmp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
                zf.write(snap_db, BUNDLE_DB_NAME)
                for full, rel in evidence_files:
                    zf.write(full, BUNDLE_EVIDENCE_PREFIX + rel)
            os.replace(tmp_zip_path, final_path)
            tmp_zip_path = None
    except FileNotFoundError:
        raise
    except Exception as e:
        raise RuntimeError(f"创建备份归档失败: {e}")
    finally:
        if tmp_zip_path and os.path.exists(tmp_zip_path):
            os.remove(tmp_zip_path)

    stat = os.stat(final_path)
    info = {
        "filename": final_name,
        "path": final_path,
        "size": stat.st_size,
        "sha256": _sha256_file(final_path),
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "reason": reason,
        "evidence_count": len(evidence_files),
    }
    logger.info("bundle_created", file=final_path, reason=reason,
                evidence_count=info["evidence_count"])
    return info


def _prune_bundles(keep: int):
    """服务端保留策略：backup_*.zip 只留最近 keep 个；rollback_* 永不删；旧裸 .db 不动。"""
    backup_dir = get_backup_dir()
    bundles = []
    for filename in os.listdir(backup_dir):
        if not (filename.startswith("backup_") and filename.endswith(".zip")):
            continue
        if "rollback" in filename:
            continue
        filepath = os.path.join(backup_dir, filename)
        bundles.append((os.stat(filepath).st_mtime, filepath))
    bundles.sort(reverse=True)
    for _, filepath in bundles[keep:]:
        try:
            os.remove(filepath)
            logger.info("bundle_pruned", file=filepath)
        except OSError:
            logger.warning("bundle_prune_failed", file=filepath)


# ---------------------------------------------------------------- ⑥ 离场桶留痕（ADR-0027 D5）
#
# 备份文件＝**可带走的整库副本**，删备份＝留存链断一条（ADR-0027 D1「离场」桶）。
# 留痕只记「哪个文件走了/没了」这个**壳**：文件名、字节数、哈希前缀、来源、库世代。
# 绝不记库内容、文件字节、口令——审计是永久保留、可被整库导出的表面，把离场数据
# 再抄一份进库里等于自己制造第二个离场面。
#
# 与「整库桶」的区别（别混用）：
# - 整库桶（上面的 `_append_audit_to_current_db`）是**换库**场景，此刻 ORM 引擎已被
#   dispose，只能走裸 sqlite3 + `record_sqlite`，且必须写在**替换后的新库**上；
# - 本段是**普通请求**场景：db 会话健在，用 `audit_service.record(db, change)` 同库
#   写入 + commit，不碰裸连接。

#: 备份来源（壳字段 `source` 的合法值）：手工端点 / 定时任务 / 导入前自动备份。
BACKUP_SOURCES = ("manual", "auto", "import")

#: create 审计行 `after` 的白名单（快照分型 D5：create 只给 after 关键字段全集）。
BACKUP_CREATE_SHELL_KEYS = ("filename", "size_bytes", "sha256", "source",
                           "db_schema_version")

#: delete 审计行 `before` 的白名单（D5：delete 只给 before 关键字段全集）。
BACKUP_DELETE_SHELL_KEYS = ("filename", "size_bytes", "mtime", "existed")

#: 哈希只记前 16 位：够比对「是不是同一份」，又不把整份文件指纹抄进审计
#: （全量 sha256 已在 bundle 的 manifest.json 里，审计不重复存一份）。
_SHA_PREFIX_LEN = 16


def _db_schema_version(db) -> int:
    """当前库已应用的最大迁移号（留痕壳字段 `db_schema_version`）。

    `schema_version` 表由迁移 runner 以裸 SQL 自建（不入 models/create_all），因此
    老库/测试库可能没有这张表 → 退化到数据世代 `SCHEMA_GENERATION`（不阻断留痕）。
    """
    try:
        value = db.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
    except Exception:
        logger.warning("backup_audit_schema_version_unavailable",
                       fallback=SCHEMA_GENERATION)
        try:
            db.rollback()      # 失败语句可能把会话留在半开事务里，先收干净
        except Exception:      # pragma: no cover - 会话已坏时无从收拾
            pass
        return SCHEMA_GENERATION
    return int(value) if value is not None else SCHEMA_GENERATION


def backup_created_payload(info: dict, source: str, schema_version: int) -> dict:
    """备份创建留痕载荷（**只有壳**；键集＝`BACKUP_CREATE_SHELL_KEYS`）。"""
    return {
        "filename": info["filename"],
        "size_bytes": int(info["size"]),
        "sha256": (info.get("sha256") or "")[:_SHA_PREFIX_LEN],
        "source": source,
        "db_schema_version": schema_version,
    }


def backup_deleted_payload(filename: str, stat) -> dict:
    """备份删除留痕载荷（**只有壳**；键集＝`BACKUP_DELETE_SHELL_KEYS`）。

    `existed: true` 是显式的：能走到这一步说明文件当时确实在盘上（删除行只可能由
    「真的删掉一份留存副本」产生，见 `delete_backup` 的顺序）。
    """
    return {
        "filename": filename,
        "size_bytes": int(stat.st_size),
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "existed": True,
    }


def audit_backup_created(db, info: dict, *, source: str = "manual") -> None:
    """留痕：`entity='backup', action='create'`（离开系统的整库副本）。

    `row_id=None`：备份是文件不是账本行，没有可指的行号——靠 `filename` 与
    `sha256` 前缀定位这份副本。

    Raises:
        ValueError: source 不在 `BACKUP_SOURCES` 内。
    """
    if source not in BACKUP_SOURCES:
        raise ValueError(f"非法备份来源: {source!r}（合法值 {sorted(BACKUP_SOURCES)}）")
    payload = backup_created_payload(info, source, _db_schema_version(db))
    audit_service.record(db, audit_service.make_change(
        "backup", None, "create", before=None, after=payload))
    db.commit()


def create_backup(reason: str = "manual", include_evidence: bool = True,
                  prune: bool = True, keep: int = DEFAULT_KEEP) -> dict:
    """创建完整备份 bundle（zip：manifest + DB 在线快照 + evidence）。

    步骤 2 迁移 runner 将以 create_backup(reason="pre_migration", prune=False) 调用。

    Returns:
        dict: path/filename/sha256/size/created_at/reason/evidence_count
    """
    info = _build_bundle("backup_", reason, include_evidence)
    if prune:
        _prune_bundles(keep)
    return info


def create_full_backup(db=None, *, source: str = "manual") -> dict:
    """创建完整数据库备份（薄封装，保持旧签名；现返回 zip bundle）。

    留痕（ADR-0027 离场桶，`POST /api/backup/create`）：传 `db` 才写审计行
    （`entity='backup', action='create'`，见 `audit_backup_created`）。迁移 runner 的
    前置备份（`create_backup(reason="pre_migration")`）与导入前备份
    （`create_backup_before_import`）走的是模块级函数，不经端点、也不传 db → 不产
    审计行（它们不是「用户把数据带走」，本次不在契约范围内）。

    **顺序：先造文件、成功后写审计行并提交。** 文件系统与 DB 不在同一事务，只能选
    一边先：备份文件是「既成事实」，审计不能先声明一份还没造出来的副本；反过来，
    审计写失败时（异常上抛、端点 500）副本已经落盘但**没有留痕**——这是可见的缺口
    （响应即报错、盘上文件可用删除端点补一笔删除留痕），比「审计先说走了、文件却
    没造出来」的假痕迹更容易追。
    """
    info = create_backup(reason="manual", include_evidence=True, prune=True)
    if db is not None:
        audit_backup_created(db, info, source=source)
    return info


def create_backup_before_import(import_filename: str) -> str:
    """在导入前创建数据库备份（薄封装，保持旧签名：返回备份文件完整路径）。"""
    clean_name = Path(import_filename).stem
    clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in clean_name)
    return create_backup(reason=f"before_import:{clean_name}", prune=False)["path"]


def list_backups() -> list[dict]:
    """列出所有备份文件（放宽后缀：.zip bundle 与旧裸 .db/.sqlite 并列，原样列出）

    Returns:
        备份文件列表，按创建时间倒序排列（最近的在最上面）
    """
    ensure_backup_dir()

    backups = []
    for filename in os.listdir(get_backup_dir()):
        if filename.endswith((".zip", ".db", ".sqlite")):
            filepath = os.path.join(get_backup_dir(), filename)
            if not os.path.isfile(filepath):
                continue
            stat = os.stat(filepath)
            backups.append({
                "filename": filename,
                "path": filepath,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "mtime": stat.st_mtime,  # 用于排序的时间戳
            })

    # 按修改时间倒序排列，最近的在最上面
    backups.sort(key=lambda x: x["mtime"], reverse=True)

    # 移除排序用的临时字段
    for backup in backups:
        del backup["mtime"]

    return backups


BACKUP_DELETE_REASON = "normalization"   # 服务端归一化，不向用户索取
BACKUP_SOURCE_MANUAL = "manual_cleanup"  # 人从端点删（含 imports 那个同义端点）
BACKUP_SOURCE_RETENTION = "retention"    # 调度器保留策略 job


def delete_backup(filename: str, *, db, actor: str = "owner",
                  audit_reason: str | None = None,
                  audit_reason_note: str | None = None,
                  source: str | None = None) -> bool:
    """删除指定的备份文件（留痕：`entity='backup', action='delete'`）。

    **不向用户索取理由（业主 2026-11-08 裁决）**：删一个旧备份文件是家务事（离场物
    的物理清理），问「为什么」只会产生噪声。故 `audit_reason` 缺省时由**服务端**记
    `reason='normalization'`，并在 `after` 里标出**触发源**：手动删除
    `source='manual_cleanup'`、调度器保留策略 `source='retention'`（spec E2 ①）。

    这不是「伪造一条人声明的裁决」：审计行仍照记（离场桶不可缺），但 reason 表达的是
    「这是归一化清理」，`after.source` 表达「谁触发的」——两件事分开写清，读端不必猜。
    `audit_reason` 仍可显式传入（未来若要区分更细的清理原因），但**没有必填口**：
    两个端点都不接 reason 参数，也就不会 422。

    留痕形状：`before` 只记文件壳（文件名/字节数/mtime/existed）；`after` 恒为
    `{"source": <manual_cleanup|retention>}`——备份删除**不再**是 D5 的 `after=NULL`
    那一型（入参 `after` 承载触发源，不用 NULL 冒充「无话可说」）。

    **顺序：先写审计行并提交，再删文件。** 文件系统与 DB 不在同一事务，两边只能选
    一边先；删除是不可逆的离场动作，先把「要断的留存链」落进不可改的审计链，再动
    文件。删文件失败 → 抛错（审计行留着＝「尝试删过」的痕迹，文件仍在，可复查）；
    若反过来「先删后记」，记失败时副本已经没了、痕迹也补不上，那是审计链不可接受
    的静默缺口。

    文件不存在（或路径越出备份目录）→ 幂等返回 False 且**零审计行**：没有东西离开
    系统，不制造假痕迹。

    Args:
        filename: 备份文件名（相对备份目录）
        db: 调用方的 ORM 会话（审计行与请求同库写入；必须传入）
        actor: `'owner'`（默认，人从端点删）| `'system'`（保留策略 job）
        audit_reason: 审计原因；缺省即 `normalization`（服务端归一化，非必填口）
        audit_reason_note: 原因说明（≤200 字）
        source: 触发源标记；缺省 `manual_cleanup`，保留策略传 `retention`

    Returns:
        是否真的删掉了文件

    Raises:
        RuntimeError: 审计已落库但删文件失败（文件仍在，事件已留痕）。
    """
    backup_dir = get_backup_dir()
    filepath = os.path.join(backup_dir, filename)
    if not (os.path.exists(filepath) and filepath.startswith(backup_dir)):
        return False
    payload = backup_deleted_payload(filename, os.stat(filepath))
    audit_service.record(db, audit_service.make_change(
        "backup", None, "delete", before=payload,
        after={"source": source or BACKUP_SOURCE_MANUAL},
        reason=audit_reason or BACKUP_DELETE_REASON,
        reason_note=audit_reason_note), actor=actor)
    db.commit()          # 审计先落地：此后任何失败都留得下痕迹
    try:
        os.remove(filepath)
    except OSError as e:
        logger.error("backup_delete_failed_after_audit", file=filepath,
                     error=str(e))
        raise RuntimeError(
            f"删除备份文件失败（本次删除已留痕，文件仍在）: {e}")
    return True


# ---------------------------------------------------------------- 保留策略（spec E2 ①）
#
# 语义从 `frontend/src/app/settings/backup-restore/page.tsx` 的 `applyRetentionPolicy`
# **原样搬到后端**（spec §4：不重构规则语义，只换执行位置）：
#   - `type='count'`：列表（mtime 倒序）第 count 条**之后**的全部删除；
#   - `type='days'`：`created_at` 早于 `now - days` 的全部删除；
#   - 两种模式都**排除**文件名含 `rollback` 的回滚备份（唯一硬保护）；
#   - `enabled=False` → 什么都不做。
# 差别只有一处：执行者从浏览器循环调 `DELETE /api/backup/delete/{name}`（每条一个
# `actor='owner'` 审计行）变成**系统 job**（`actor='system'`、`reason='normalization'`、
# `after.source='retention'`）——策略是系统规则，不是人的裁决（spec E2 理由）。

RETENTION_DEFAULTS = {"enabled": False, "type": "count", "count": 10, "days": 30}


def retention_policy_from_env(env=None) -> dict:
    """从环境变量读保留策略（前端那份存在 localStorage，后端读不到）。

    `BACKUP_RETENTION_ENABLED`（默认 false，与前端初始 state 一致：不配置＝不自动删）
    `BACKUP_RETENTION_TYPE`（count|days）、`BACKUP_RETENTION_COUNT`（默认 10）、
    `BACKUP_RETENTION_DAYS`（默认 30）。非法值一律退回默认，不因配置错删文件。
    """
    src = os.environ if env is None else env
    enabled = str(src.get("BACKUP_RETENTION_ENABLED", "")).strip().lower()
    ptype = str(src.get("BACKUP_RETENTION_TYPE", "")).strip().lower()
    if ptype not in ("count", "days"):
        ptype = RETENTION_DEFAULTS["type"]
    try:
        count = int(src.get("BACKUP_RETENTION_COUNT") or RETENTION_DEFAULTS["count"])
    except (TypeError, ValueError):
        count = RETENTION_DEFAULTS["count"]
    try:
        days = int(src.get("BACKUP_RETENTION_DAYS") or RETENTION_DEFAULTS["days"])
    except (TypeError, ValueError):
        days = RETENTION_DEFAULTS["days"]
    return {"enabled": enabled in ("1", "true", "yes", "on"),
            "type": ptype, "count": max(1, count), "days": max(1, days)}


def retention_candidates(backups: list[dict], policy: dict) -> list[dict]:
    """按策略选出**该删的文件**（纯函数，便于逐条回归规则语义）。

    `backups` 必须是 `list_backups()` 的形状（已按 mtime 倒序）。
    """
    if not policy.get("enabled") or not backups:
        return []
    if policy.get("type") == "days":
        cutoff = datetime.now() - timedelta(days=int(policy.get("days") or 0))
        picked = [b for b in backups
                  if datetime.fromisoformat(b["created_at"]) < cutoff]
    else:
        picked = list(backups[int(policy.get("count") or 0):])
    return [b for b in picked if "rollback" not in b["filename"]]


def apply_retention_policy(policy: dict | None = None, *, db,
                           dry_run: bool = False) -> dict:
    """执行保留策略（scheduler job `backup_retention_policy` 的唯一入口）。

    每条删除都走 `delete_backup(actor='system', source='retention')`：`reason` 由
    服务端归一化为 `normalization`（与手动删除同一取值，靠 `after.source` 区分触发源），
    审计行**先落库再删文件**（顺序见该函数 docstring），单条失败只记 `failed[]`、
    不拖累其余（与前端循环的吞错语义同）。

    Returns:
        `{enabled, type, deleted[filename], failed[{filename,reason}], candidates}`
    """
    policy = policy or retention_policy_from_env()
    if not policy.get("enabled"):
        return {"enabled": False, "type": policy.get("type"),
                "deleted": [], "failed": [], "candidates": 0}
    picked = retention_candidates(list_backups(), policy)
    result = {"enabled": True, "type": policy.get("type"),
              "deleted": [], "failed": [], "candidates": len(picked)}
    if dry_run:
        return result
    for item in picked:
        filename = item["filename"]
        try:
            removed = delete_backup(
                filename, db=db, actor="system",
                source=BACKUP_SOURCE_RETENTION)
        except Exception as exc:        # 单条故障不拖累其余（对齐前端 try/catch）
            logger.warning("backup_retention_delete_failed", file=filename,
                           error=str(exc))
            result["failed"].append({"filename": filename,
                                     "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if removed:
            result["deleted"].append(filename)
    if result["deleted"] or result["failed"]:
        logger.info("backup_retention_applied", policy=policy,
                    deleted=len(result["deleted"]), failed=len(result["failed"]))
    return result


def retention_daily() -> dict:
    """调度器入口：每日一次执行保留策略（失败仅日志，绝不冒泡到调度线程）。"""
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            return apply_retention_policy(db=db)
    except Exception as exc:            # pragma: no cover - 调度线程保护
        logger.warning("backup_retention_job_failed: %s", exc)
        return {"enabled": False, "deleted": [], "failed": [], "candidates": 0,
                "error": str(exc)}


def _validate_sqlite(db_path: str):
    """基础有效性：含表 + PRAGMA integrity_check 通过。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchall()
            if not result or result[0][0] != "ok":
                raise ValueError(f"数据库完整性检查未通过: {result}")
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            if not tables:
                raise ValueError("备份文件不包含任何表")
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ValueError(f"无效的 SQLite 备份文件: {e}")


def _safe_extract(zf: zipfile.ZipFile, dest_dir: str):
    """解包并拒绝路径穿越/绝对路径成员。"""
    for member in zf.infolist():
        name = os.path.normpath(member.filename)
        if not name or name.startswith("..") or os.path.isabs(name) or name.startswith("/"):
            raise ValueError(f"归档包含非法成员路径: {member.filename}")
    zf.extractall(dest_dir)


def _replace_current_db(new_db_path: str):
    """替换当前 DB 并删除 -wal/-shm（漏删会导致旧 WAL 污染新库）。"""
    db_path = _db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    database.engine.dispose()
    logger.info("restore_db_connections_closed")
    staged = db_path + ".restoring"
    shutil.copy2(new_db_path, staged)
    os.replace(staged, db_path)
    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.exists(side):
            os.remove(side)


# ---------------------------------------------------------------- ⑤整库留痕（ADR-0027）

#: 整库替换类动作（`entity='backup'`）：restore＝从备份包/备份库恢复，upload_db＝直接上传库文件。
RESTORE_AUDIT_ACTIONS = ("restore", "upload_db")

#: 目标库具备这两张表才谈得上「在新库续链」；外来旧库两者皆无 → 跳过留痕、不阻断恢复。
_AUDIT_TABLES = ("audit_log", "audit_chain_state")


def _restore_audit_payload(source_label: str, before_head: dict | None,
                           target_head: dict | None, db_sha256: str,
                           integrity_status: str | None = None,
                           rollback_confirmed: bool = False) -> dict:
    """整库替换的留痕载荷（ADR-0027 D4 ⑤桶）。

    `chain_head_before` / `chain_rows_before` 是**换库前**旧库的链头与行数：换库会
    连带换掉 audit_log，不先记下来就再也没有任何痕迹说明「旧链停在哪」。
    `chain_head_target` 是目标库自己的链头（manifest.audit_head 或直读目标文件），
    读不到写 None——取证少一个锚点，绝不因此阻塞恢复。

    2026-11 追加两键（**不新增列、不改链口径**，只扩 `after` 的键集）：
    - `integrity`：本次恢复前的库外锚对账结论（`match`/`db_newer`/`db_older`/
      `diverged`/`unknown`，见 `audit_integrity_service`）；
    - `rollback_confirmed`：是否**真的**越过了阻断判定（`db_older`/`diverged`）。
      仅仅是「传了 confirm_rollback=true 但目标并不旧」时为 False——留痕要记的是
      「发生过一次被确认的回退」，不是「调用方填了哪个参数」。
    """
    return {
        "chain_head_before": (before_head or {}).get("last_hash"),
        "chain_rows_before": (before_head or {}).get("row_count"),
        "chain_head_target": (target_head or {}).get("last_hash"),
        "db_sha256": db_sha256,
        "source": source_label,
        "integrity": integrity_status,
        "rollback_confirmed": bool(rollback_confirmed),
    }


#: 恢复前对账的阻断判定（回退/分叉）；match/db_newer/unknown 一律放行。
_RESTORE_BLOCKING_STATUSES = audit_integrity_service.BLOCKING_STATUSES


def _head_view(head: dict | None) -> dict:
    """`_audit_head()` 结果 → 对账视图（字段名对齐 `audit_integrity_service.current_head()`）。

    「当前库没有审计表」由调用方先判成 `unknown`（不在此处臆断成 0 行）。
    """
    head = head or {}
    return {
        "last_hash": head.get("last_hash"),
        "row_count": head.get("row_count") or 0,
        "max_id": head.get("last_id") or 0,
    }


def _manifest_head_view(manifest: dict) -> dict:
    """目标包 manifest 的审计链头 → 对账视图（缺键/非 dict → 全 None＝无法判定）。"""
    head = manifest.get("audit_head") if isinstance(manifest, dict) else None
    if not isinstance(head, dict):
        head = {}
    return {
        "last_hash": head.get("last_hash"),
        "row_count": head.get("row_count") if isinstance(head.get("row_count"), int) else None,
        "max_id": head.get("last_id"),
    }


def _check_restore_rollback(before_head: dict | None, manifest: dict,
                            source_label: str, *, confirm_rollback: bool):
    """恢复前对账（阻断，但可显式越过）：返回 `(verdict, rollback_confirmed)`。

    **比对方向**：被判断的一方是**目标包**（它是不是比当前库旧），参照锚是**当前库**
    的链头。所以 `db_older` ＝「目标比当前旧」＝整库回退，`db_newer` ＝「目标更新」
    ＝正常前滚（与 `compare()` 的字段语义一致：第一个参数永远是「被判断的那一方」）。

    阻断条件（`db_older` / `diverged`）在**替换库之前**抛出，现库一个字节都不动，
    rollback bundle 也还没造。放行条件：目标包没有 `audit_head`（`unknown`，无法
    判定）、当前库没有审计链头、`match`、`db_newer`，或显式 `confirm_rollback=True`。
    """
    current = _head_view(before_head)
    target = _manifest_head_view(manifest)

    if before_head is None:
        verdict = audit_integrity_service.Verdict(
            "unknown", "当前库没有审计链头（无审计表），无法判定")
    else:
        verdict = audit_integrity_service.compare(
            target,
            audit_integrity_service.Anchor(
                source=source_label, generated_at=None,
                audit_head=current["last_hash"], audit_rows=current["row_count"]))

    overridden = verdict.status in _RESTORE_BLOCKING_STATUSES
    if overridden and not confirm_rollback:
        prefix = audit_integrity_service.hash_prefix
        raise ApiError(
            ErrorCode.AUDIT_ROLLBACK_DETECTED, 409,
            f"目标备份的审计链比当前库更旧（{verdict.status}）："
            f"current_rows={current['row_count']} target_rows={target['row_count']} "
            f"current_head={prefix(current['last_hash'])} "
            f"target_head={prefix(target['last_hash'])}；"
            "如确认要整库回退，请显式传 confirm_rollback=true",
            details={
                "status": verdict.status,
                "current_rows": current["row_count"],
                "target_rows": target["row_count"],
                "current_head_prefix": prefix(current["last_hash"]),
                "target_head_prefix": prefix(target["last_hash"]),
            })
    return verdict, (overridden and bool(confirm_rollback))


def _append_audit_to_current_db(action: str, after: dict) -> bool:
    """把留痕写进**替换后的新库**，作为新库的第一条业务留痕。

    陷阱（本函数存在的理由）：换库会把 audit_log 整表换掉，替换前写下的审计行随旧库
    一起消失。所以顺序必须是「先换库、再在新库写」——本函数只允许在
    `_replace_current_db` 成功之后调用；替换失败时调用方不会走到这里，旧库原样可用。

    目标库没有审计表（外来旧库 / 非本系统库）→ 不写、不阻断恢复，返回 False，
    由调用方降级成 `warnings` 里的显式提示（缺口可见，不假装留痕了）。

    写入用裸 sqlite3 + `audit_service.record_sqlite`（actor=system）：此刻 ORM 引擎
    已被 dispose，且整库操作没有「业务事务」可依附。
    """
    if action not in RESTORE_AUDIT_ACTIONS:
        raise ValueError(f"非法整库留痕动作: {action}")
    db_path = _db_path()
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(_AUDIT_TABLES) <= tables:
            logger.warning("restore_audit_skipped", reason="target_db_has_no_audit_tables",
                           db=db_path)
            return False
        conn.execute("BEGIN")
        try:
            audit_service.record_sqlite(conn, audit_service.make_change(
                "backup", None, action, before=None, after=after), actor="system")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def _no_audit_warning() -> str:
    return "目标库不含审计表（audit_log/audit_chain_state），本次整库操作未留痕"


def _merge_evidence(src_evidence_dir: str):
    """凭证合并：只增不删不覆盖（凭证 write-once，合并安全）。"""
    if not os.path.isdir(src_evidence_dir):
        return 0
    evidence_root = _evidence_dir()
    merged = 0
    for full, rel in _iter_evidence_files(src_evidence_dir):
        target = os.path.join(evidence_root, rel)
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)
            merged += 1
    return merged


def restore_database(source_path: str, *, action: str = "restore",
                     source_label: str | None = None,
                     confirm_rollback: bool = False) -> dict:
    """从备份恢复数据库

    **警告：此操作将替换当前数据库！**

    bundle（.zip）：验 manifest + sha256 + integrity_check → **恢复前库外锚对账**
    （目标包链头比当前库旧/分叉＝整库回退，默认 409 阻断，`confirm_rollback=True`
    可显式越过）→ 恢复前 rollback bundle（含 evidence）→ 替换 DB 并删 -wal/-shm
    → evidence 只增合并。
    旧裸 .db/.sqlite：按现路径仅恢复 DB，响应附「不含凭证」警示。旧备份不迁移；
    裸库没有 manifest，对账结论恒为 `unknown`（放行）。

    留痕（ADR-0027 D4 ⑤整库桶）：`entity='backup'`、`action='restore'|'upload_db'`，
    **写在替换后的新库里**（替换前写的行会随旧库一起消失），载荷含换库前链头/行数、
    目标库链头（读不到为 None）、目标库 sha256、来源标识，以及本次对账结论
    `integrity` 与 `rollback_confirmed`。替换失败 → 不写审计、旧库保持可用。

    Args:
        source_path: 备份文件路径
        action: 留痕动作，`restore`（从备份恢复）或 `upload_db`（直接上传库文件）
        source_label: 留痕里的来源标识（上传场景传原始文件名；缺省取路径 basename）
        confirm_rollback: 显式确认「我知道这是整库回退」——仅在该判定本会阻断时生效

    Returns:
        dict: kind（bundle/legacy_db）、warnings、rollback（回滚备份文件名）

    Raises:
        FileNotFoundError: 备份文件不存在
        ValueError: 备份文件无效（坏包/坏 db 一律拒绝，现库不动）
        ApiError: AUDIT_ROLLBACK_DETECTED（409）——目标包链头比当前库旧/分叉
        RuntimeError: 恢复过程中出错
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"备份文件不存在: {source_path}")
    label = source_label or os.path.basename(source_path)

    extract_dir = None
    try:
        if source_path.endswith(".zip"):
            # ---- bundle 恢复：先全部验证，通过后才触碰现库 ----
            if not zipfile.is_zipfile(source_path):
                raise ValueError(f"无效的备份归档: {source_path}")
            extract_dir = tempfile.mkdtemp(prefix="restore_", dir=ensure_backup_dir())
            with zipfile.ZipFile(source_path) as zf:
                _safe_extract(zf, extract_dir)

            manifest_path = os.path.join(extract_dir, MANIFEST_NAME)
            if not os.path.exists(manifest_path):
                raise ValueError("归档缺少 manifest.json")
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ValueError(f"manifest.json 无法解析: {e}")

            if manifest.get("format_version") != SUPPORTED_FORMAT_VERSION:
                raise ValueError(f"不支持的归档格式版本: {manifest.get('format_version')}")

            db_file = os.path.join(extract_dir, BUNDLE_DB_NAME)
            if not os.path.exists(db_file):
                raise ValueError("归档缺少数据库文件")
            expected_sha = manifest.get("db_sha256")
            actual_sha = _sha256_file(db_file)
            if expected_sha != actual_sha:
                raise ValueError("db_sha256 校验不符，归档可能已损坏或被篡改")
            _validate_sqlite(db_file)

            # ⑤-补 恢复前对账：目标包链头比当前库旧/分叉＝整库回退，默认 409 阻断。
            # 必须在替换之前（替换后旧库就没了，比对无从谈起）；也放在造 rollback
            # bundle 之前——被拒绝的恢复不该在盘上留下任何新文件。
            before_head = _audit_head(_db_path())
            verdict, rollback_confirmed = _check_restore_rollback(
                before_head, manifest, label, confirm_rollback=confirm_rollback)

            rollback = _build_bundle("rollback_before_restore_", "pre_restore", True)
            _replace_current_db(db_file)
            merged = _merge_evidence(os.path.join(extract_dir, "evidence"))
            audited = _append_audit_to_current_db(action, _restore_audit_payload(
                label, before_head, manifest.get("audit_head"), actual_sha,
                integrity_status=verdict.status,
                rollback_confirmed=rollback_confirmed))
            logger.info("database_restored", source=source_path, kind="bundle",
                        evidence_merged=merged, audited=audited)
            return {"kind": "bundle", "warnings": [] if audited else [_no_audit_warning()],
                    "merged_evidence": merged, "rollback": rollback["filename"]}
        else:
            # ---- 旧裸 .db 兼容路径（仅 DB）----
            _validate_sqlite(source_path)
            target_sha = _sha256_file(source_path)
            target_head = _audit_head(source_path)   # 读不到 → None，不阻塞恢复
            rollback = _build_bundle("rollback_before_restore_", "pre_restore", True)
            before_head = _audit_head(_db_path())
            _replace_current_db(source_path)
            audited = _append_audit_to_current_db(action, _restore_audit_payload(
                label, before_head, target_head, target_sha))
            warnings = ["旧裸 .db 备份仅恢复数据库，不含凭证（evidence 未变动）"]
            if not audited:
                warnings.append(_no_audit_warning())
            logger.info("database_restored", source=source_path, kind="legacy_db",
                        audited=audited)
            return {"kind": "legacy_db", "warnings": warnings, "merged_evidence": 0,
                    "rollback": rollback["filename"]}
    except (FileNotFoundError, ValueError, ApiError):
        raise
    except Exception as e:
        raise RuntimeError(f"恢复数据库失败: {e}")
    finally:
        if extract_dir and os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
