"""数据库迁移服务

v5 一期·步骤 2：编号脚本 runner（schema_version 记账 + 前置备份 + fail-fast）。

- 迁移脚本落 `backend/migrations/NNNN_描述.py`，各暴露 `upgrade(conn)`（conn 为 stdlib
  sqlite3 连接，体内保留存在性守卫，重复运行无害）。runner 按编号升序扫目录，
  文件名即真相（importlib 动态加载，无注册表）。
- `schema_version` 表由 runner 以裸 SQL 自建，不入 models/create_all。
- 启动顺序（main.py lifespan）：create_all → run_migrations() → seed。
- 失败语义：每脚本一事务，成功即登记；任一脚本抛错 → 回滚该脚本 → RuntimeError
  中止启动（日志含失败编号＋前置备份路径）；修复后重启从首个未登记编号自动续跑。
- 备份钩子：仅「增量且有待执行脚本」时调 create_backup(reason="pre_migration",
  prune=False)；备份失败＝中止迁移（宁可不起，不裸迁）。全新库与 v4 认领均不前置备份。
- 两类首跑路径：
  * 全新库（无 assets 表）→ 全部编号脚本实际执行（列守卫使 DDL 成 no-op）→ baseline=0
  * 既有 v4 库（有 assets 且 schema_version 空）→ 基线认领，全部登记 baseline=1，不跑本体
- seed 两项不编号：保持每次启动幂等运行、吞异常仅 warning 的现状语义。

旧 inspect 迁移辅助函数（migrate_* / check_*）保留：runner 之外无生产调用，但被
若干单测直接引用，且幂等无害，故不删（避免破坏 383 现有用例）。
"""

import importlib.util
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import inspect, select, text
from database import engine, SessionLocal, DB_PATH
from services import backup_service, audit_service

logger = logging.getLogger(__name__)

# V1 遗留表（整套删除，含相互外键依赖；asset_records 排首位先 drop）
V1_TABLES = ["asset_records", "fund_types", "accounts", "liquidity_ratings", "asset_owners"]

# backend/migrations 目录（与本文件相隔一层 services/）
_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)

_SCHEMA_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "  version INTEGER PRIMARY KEY,"
    "  name TEXT NOT NULL,"
    "  baseline INTEGER NOT NULL DEFAULT 0,"
    "  applied_at TEXT NOT NULL"
    ")"
)

_SCRIPT_NAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.py$")

# 第三条 LLM 供应商预置：百炼 Token Plan（个人版）。
# kind 故意复用 dashscope 的 quirks 映射（enable_thinking=false +
# response_format=json_object + vl_high_resolution_images），这是
# qwen3.8-flash 的必需请求形态；不新建 kind、不改 llm_verification_service。
# api_key 留空，由用户自填 sk-sp- 前缀订阅 Key。
TOKEN_PLAN_LLM_PROVIDER_SEED = {
    "label": "百炼TokenPlan(个人版)",
    "kind": "dashscope",
    "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3.8-flash",
}


@dataclass
class MigrationReport:
    applied: list[int] = field(default_factory=list)   # 本次实际执行并登记的编号
    baseline: list[int] = field(default_factory=list)  # 本次基线认领登记的编号（未跑本体）
    failed: int | None = None                          # 失败脚本编号（成功为 None）
    backup: str | None = None                          # 前置备份路径（增量执行时有值）

    @property
    def noop(self) -> bool:
        return not self.applied and not self.baseline and self.failed is None


def _resolve_db_path() -> str:
    """调用时解析 DB 路径（与 database.py / backup_service._db_path 同口径）。"""
    return os.environ.get("DATABASE_PATH") or DB_PATH


def _discover_scripts(scripts_dir: str) -> list[tuple[int, str, str]]:
    """扫描 migrations 目录，按 4 位编号升序返回 (version, name, path)。文件名即真相。"""
    out: list[tuple[int, str, str]] = []
    if not os.path.isdir(scripts_dir):
        return out
    for fn in sorted(os.listdir(scripts_dir)):
        if not _SCRIPT_NAME_RE.match(fn):
            continue
        version = int(fn[:4])
        name = fn[4:-3]  # 去 NNNN_ 前缀与 .py 后缀
        out.append((version, name, os.path.join(scripts_dir, fn)))
    out.sort(key=lambda x: x[0])
    return out


def _load_script_module(path: str):
    """importlib 按文件路径动态加载脚本（文件名即真相，无注册表）。"""
    spec = importlib.util.spec_from_file_location(f"migration_{os.path.basename(path)}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载迁移脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "upgrade", None)):
        raise RuntimeError(f"迁移脚本缺少 upgrade(conn): {path}")
    return module


def _run_script(conn: sqlite3.Connection, version: int, name: str, upgrade,
                baseline: int, backup_path: str | None):
    """单脚本一事务：BEGIN → upgrade → 登记 schema_version → COMMIT；出错 ROLLBACK。"""
    conn.execute("BEGIN")
    try:
        upgrade(conn)
        conn.execute(
            "INSERT INTO schema_version (version, name, baseline, applied_at) VALUES (?,?,?,?)",
            (version, name, baseline, datetime.now().isoformat()),
        )
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error("migration_failed: script %04d_%s err=%s backup=%s",
                     version, name, e, backup_path)
        raise RuntimeError(
            f"迁移失败：脚本 {version:04d}_{name}（{e}）；"
            f"前置备份：{backup_path or '无'}；修复后重启将从该编号续跑"
        ) from e


def _emit_migration_run(conn: sqlite3.Connection, report: MigrationReport):
    """步骤 4 审计：runner 完成时发一行 migration_run（actor=system，独立事务）。

    - 仅在本次确实有记账动作（applied 或 baseline 非空）时发；纯 no-op 启动不刷行。
    - audit_log 表尚不存在（0009 未进目录/未执行）→ 跳过，不回补历史。
    - after＝报告摘要（applied/baseline/failed/backup 路径）。
    """
    if not (report.applied or report.baseline):
        return
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
    if not has_table:
        logger.info("migration_run_audit_skipped: audit_log 表不存在")
        return
    change = {
        "entity": "schema", "row_id": None, "action": "migration_run",
        "before": None,
        "after": {"applied": report.applied, "baseline": report.baseline,
                  "failed": report.failed, "backup": report.backup},
        "evidence_ref": None,
    }
    conn.execute("BEGIN")
    try:
        audit_service.record_sqlite(conn, change, actor="system")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise RuntimeError(f"migration_run 审计记账失败（可信优先，中止启动）: {e}") from e


def run_migrations(db_path: str | None = None,
                   scripts_dir: str | None = None) -> MigrationReport:
    """编号脚本 runner：建表→首跑判定（全新/v4 认领）→执行（前置备份钩子）。

    v4 基线认领只吞 `CLAIM = True` 的脚本（0001~0007＝旧 inspect 机制每启动必跑、
    行为已收敛的存量编目）；未认领脚本（如 0008 身份层）即便是首跑也**实际执行**
    ——否则新增迁移会被认领机制静默跳过。

    fail-fast：任一脚本失败即抛 RuntimeError 中止启动。调用方（migrate_database/main
    lifespan）不再吞该异常。
    """
    path = db_path or _resolve_db_path()
    scripts_dir = scripts_dir or _MIGRATIONS_DIR
    if not os.path.exists(path):
        raise FileNotFoundError(f"数据库文件不存在（应由 create_all 先行创建）: {path}")

    conn = sqlite3.connect(path)
    conn.isolation_level = None  # 手动事务控制：BEGIN/COMMIT/ROLLBACK；FK 本连接默认 off
    try:
        conn.execute(_SCHEMA_VERSION_DDL)
        conn.commit()

        registered = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        has_assets = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone() is not None

        # 文件名即真相：逐个动态加载，读 CLAIM 标记（True＝v4 已收敛编目）
        catalog = []  # (version, name, upgrade, claimable)
        for version, name, spath in _discover_scripts(scripts_dir):
            module = _load_script_module(spath)
            catalog.append((version, name, module.upgrade,
                            bool(getattr(module, "CLAIM", False))))

        report = MigrationReport()
        first_run = not registered

        # —— 首跑＋既有 v4 库：只认领 CLAIM=True 编目（不跑本体、不前置备份）——
        if first_run and has_assets:
            claimable = [c for c in catalog if c[3]]
            if claimable:
                conn.execute("BEGIN")
                for version, name, _, _ in claimable:
                    conn.execute(
                        "INSERT INTO schema_version (version, name, baseline, applied_at) VALUES (?,?,?,?)",
                        (version, name, 1, datetime.now().isoformat()),
                    )
                conn.execute("COMMIT")
                report.baseline = [v for v, _, _, _ in claimable]
                logger.info("migrations_v4_baseline_claimed versions=%s", report.baseline)
                registered |= set(report.baseline)

        pending = [c for c in catalog if c[0] not in registered]
        if not pending:
            logger.info("migrations_noop registered=%s", sorted(registered))
            _emit_migration_run(conn, report)
            return report

        # 前置备份：有业务数据可失（assets 存在）且有待执行脚本才备；
        # 全新空库（无 assets）无可失，不备（spec §4-①）。备份失败＝中止（不裸迁）。
        backup_path = None
        if has_assets:
            try:
                info = backup_service.create_backup(reason="pre_migration", prune=False)
            except Exception as e:
                logger.error("migration_aborted_backup_failed: %s", e)
                raise RuntimeError(f"迁移中止：前置备份失败（宁可不起，不裸迁）：{e}") from e
            backup_path = info["path"]
            logger.info("migrations_pre_backup %s pending=%s",
                        backup_path, [p[0] for p in pending])

        for version, name, upgrade, _ in pending:
            _run_script(conn, version, name, upgrade, baseline=0, backup_path=backup_path)
            report.applied.append(version)
        report.backup = backup_path
        logger.info("migrations_applied versions=%s (first_run=%s)",
                    report.applied, first_run)
        _emit_migration_run(conn, report)
        return report
    finally:
        conn.close()


def migrate_database() -> MigrationReport:
    """启动链的执行体：run_migrations（fail-fast）→ seed（现状语义）。

    seed 两项不编号：每次启动幂等运行、吞异常仅 warning。迁移/备份失败会抛出
    RuntimeError（fail-fast），由 lifespan 决定中止启动——不再被这里吞掉。
    """
    report = run_migrations()

    # 三期-3 W1：启动期链自愈（0018 已回填；回滚期 NULL 尾部续接；不符则拒接）
    try:
        from services import audit_chain
        if not audit_chain.ensure_chain():
            logger.critical("audit_chain_ensure_refused: 已链行与状态不符，需人工")
    except Exception:
        logger.critical("audit_chain_ensure_failed", exc_info=True)

    # 三期-3 W1-补：库外锚对账（整库回退检测）。`verify()` 只能在同库内自证，整库
    # 被恢复成旧版本时它仍然通过；这里拿备份 manifest 里的 `audit_head` 与当前链头
    # 比对，发现 `db_older`/`diverged` 只记 CRITICAL——**绝不阻止启动**（单用户系统
    # 没人值班，锁死门外比漏报更糟），也不改变上面 `ensure_chain` 的任何行为。
    try:
        from services import audit_integrity_service
        with SessionLocal() as _integrity_db:
            audit_integrity_service.startup_check(_integrity_db)
    except Exception:
        logger.critical("audit_integrity_startup_check_failed", exc_info=True)

    # 来源组落库：把旧硬编码 SOURCE_GROUPS 的 bantuou 组迁入 source_groups 表（幂等）
    seed_source_groups()

    # LLM 供应商预置：seed 两条默认禁用记录（幂等）+ 第三条百炼TokenPlan（幂等）
    seed_llm_providers()

    logger.info("database_migration_completed applied=%s baseline=%s failed=%s",
                report.applied, report.baseline, report.failed)
    return report


def seed_llm_providers():
    """预置 LLM 供应商（幂等，逻辑在 service 层）。

    与 seed_source_groups 同构：本函数只负责开 SessionLocal，
    实际「表空才插」逻辑在 llm_verification_service.seed_default_llm_providers
    （可被测试用内存 db fixture 直接调用）。
    第三条「百炼TokenPlan(个人版)」按 label 查存否单独补种，
    保证已预置前两条的老库下次启动仍能自动补上这一条。
    """
    try:
        from services.llm_verification_service import seed_default_llm_providers
        with SessionLocal() as conn:
            seed_default_llm_providers(conn)
            seed_token_plan_llm_provider(conn)
        logger.info("llm_providers seed checked")
    except Exception as e:
        logger.warning(f"seed_llm_providers failed: {e}")


def seed_token_plan_llm_provider(conn):
    """按 label 幂等补种「百炼TokenPlan(个人版)」（不存在才插入）。"""
    from models import LLMProvider
    label = TOKEN_PLAN_LLM_PROVIDER_SEED["label"]
    exists = conn.execute(
        select(LLMProvider.id).where(LLMProvider.label == label)
    ).first()
    if exists:
        return
    # sort_order 排在现有两条（均为 0）之后；api_key 由用户自填
    conn.add(LLMProvider(api_key="", is_default=False, enabled=False, sort_order=1,
                         **TOKEN_PLAN_LLM_PROVIDER_SEED))
    conn.commit()


def seed_source_groups():
    """把 legacy SOURCE_GROUPS（bantuou 组）落入 source_groups 表（幂等）。"""
    try:
        from services.screenshot_service import seed_default_source_groups
        with SessionLocal() as conn:
            seed_default_source_groups(conn)
        logger.info("source_groups seeded with legacy bantuou group")
    except Exception as e:
        logger.warning(f"seed_source_groups failed: {e}")


def migrate_allocation_targets_table():
    """迁移 allocation_targets 表，添加缺失的列"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('allocation_targets')]
    
    with engine.connect() as conn:
        # 添加 alert_threshold 列（用于自定义偏离预警阈值）
        if 'alert_threshold' not in columns:
            logger.info("Adding alert_threshold column to allocation_targets table")
            conn.execute(text("ALTER TABLE allocation_targets ADD COLUMN alert_threshold DECIMAL(5, 2) NOT NULL DEFAULT 5.00"))
            conn.commit()
            logger.info("alert_threshold column added successfully")


def migrate_account_remind_enabled():
    """为 attributes 表增加 remind_enabled 列（幂等，账户录入提醒开关）

    SQLite 支持带 DEFAULT 的 ADD COLUMN，无需重建表；存量行由
    DEFAULT 1 回填（默认全部参与提醒）。
    """
    if check_column_exists("attributes", "remind_enabled"):
        return
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE attributes ADD COLUMN remind_enabled "
            "BOOLEAN NOT NULL DEFAULT 1"
        ))
        conn.commit()
        logger.info("migrate_account_remind_enabled: attributes.remind_enabled column added")


def migrate_supplement_rows_column():
    """为 screenshot_evidence 表增加 supplement_rows 列（幂等，补录审计）。

    与 migrate_account_remind_enabled 同型：可空 TEXT 直接 ALTER 添加，
    存量凭证无需回填（历史无补录 = NULL）。
    """
    if check_column_exists("screenshot_evidence", "supplement_rows"):
        return
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE screenshot_evidence ADD COLUMN supplement_rows TEXT"
        ))
        conn.commit()
        logger.info("migrate_supplement_rows_column: screenshot_evidence.supplement_rows column added")


def migrate_evidence_engine_column():
    """为 screenshot_evidence 表增加 engine 列（幂等，识别引擎审计）。

    与 migrate_supplement_rows_column 同型：可空 VARCHAR 直接 ALTER 添加，
    存量凭证无需回填（历史行 NULL，语义 = "ocr"）。
    """
    if check_column_exists("screenshot_evidence", "engine"):
        return
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE screenshot_evidence ADD COLUMN engine VARCHAR(10)"
        ))
        conn.commit()
        logger.info("migrate_evidence_engine_column: screenshot_evidence.engine column added")


def check_table_exists(table_name: str) -> bool:
    """检查表是否存在"""
    inspector = inspect(engine)
    return table_name in inspector.get_table_names()


def check_column_exists(table_name: str, column_name: str) -> bool:
    """检查列是否存在"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def drop_v1_tables():
    """删除 V1 遗留表（asset_records 等），仅当存在时"""
    with engine.connect() as conn:
        for t in V1_TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        conn.commit()


def migrate_side_field():
    """为 assets 表增加 side 字段（幂等）

    - 无 side 列则 ALTER 添加，默认 'asset'
    - 按历史符号回填：amount < 0 记 liability，金额取绝对值
    """
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns('assets')]
    if 'side' in columns:
        return

    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE assets ADD COLUMN side VARCHAR(10) NOT NULL DEFAULT 'asset'"))
        conn.execute(text("UPDATE assets SET side = 'liability' WHERE amount < 0"))
        conn.execute(text("UPDATE assets SET amount = ABS(amount)"))
        conn.commit()
        logger.info("migrate_side_field: assets.side column added and backfilled")


def migrate_source_evidence_column():
    """为 assets 表增加 source_evidence_id 列（幂等）

    截图凭证（screenshot_evidence 表）由 create_all 自动建；此处仅补
    资产 → 凭证的外键列。SQLite 支持 ALTER ADD COLUMN 带 REFERENCES。
    """
    if check_column_exists("assets", "source_evidence_id"):
        return
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE assets ADD COLUMN source_evidence_id INTEGER "
            "REFERENCES screenshot_evidence(id)"
        ))
        conn.commit()
        logger.info("migrate_source_evidence_column: assets.source_evidence_id column added")
