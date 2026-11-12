"""三期-3 W2 建议隔离区测试（spec §3.2/§3.3/§8.3；ADR-0013 D2/D3）。

覆盖：AST 导入边界（advisor 包不触账本写服务/模型；反向依赖禁止）、
`PRAGMA foreign_key_list` 零账本 FK、0019 触发器逐表/逐列、settings 种子、
迁移双跑幂等。数据全部合成。
"""
import ast
import sqlite3
from pathlib import Path

import pytest

import models  # noqa: F401  全表注册
from database import Base
from sqlalchemy import create_engine
from services.db_migration_service import _MIGRATIONS_DIR, _load_script_module

M0019 = _load_script_module(f"{_MIGRATIONS_DIR}/0019_advisor.py")
ROOT = Path(__file__).resolve().parents[1]
ADVISOR_DIR = ROOT / "services" / "advisor"

BANNED_MODULES = {
    "asset_service", "principal_service", "transfer_service", "lineage_service",
    "screenshot_service",
}
BANNED_MODELS = {
    "Asset", "AssetItem", "AssetItemName", "AssetItemOwnerChange",
    "PrincipalChange", "TransferEvent", "LineageLink",
}
ADVISOR_TABLES = (
    "advisor_info_sets", "advisor_info_entries", "advisor_suggestions",
    "advisor_suggestion_items", "advisor_decisions", "advisor_jobs",
    "advisor_settings",
)


# ---------------------------------------------------------------- AST 隔离


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
            names.update(alias.name for alias in node.names)
    return names


def test_advisor_package_has_no_ledger_write_imports():
    files = list(ADVISOR_DIR.glob("*.py"))
    assert files, "services/advisor 包缺失"
    for path in files:
        imported = _imports(path)
        flat = {name.split(".")[-1] for name in imported}
        assert not (flat & BANNED_MODULES), f"{path.name} import 账本写服务"
        assert not (flat & BANNED_MODELS), f"{path.name} import 账本模型"
        assert not any(name.startswith("services.ingest") for name in imported)


def test_reverse_dependency_forbidden():
    """账本/估值/规划业务模块不得导入 advisor；调度器为编排根，可挂定时任务。"""
    allowed = {"scheduler_service.py"}
    for path in (ROOT / "services").glob("*.py"):
        if path.name in allowed:
            continue
        imported = _imports(path)
        assert not any("services.advisor" in name or name == "services.advisor"
                       for name in imported), f"{path.name} 反向依赖 advisor"
    routers = ROOT / "routers"
    if routers.is_dir():
        for path in routers.glob("advisor*.py"):
            imported = _imports(path)
            flat = {name.split(".")[-1] for name in imported}
            assert not (flat & BANNED_MODULES)
            assert not (flat & BANNED_MODELS)


# ---------------------------------------------------------------- 迁移 / FK


@pytest.fixture()
def advisor_db(tmp_path):
    path = str(tmp_path / "advisor.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0019.upgrade(conn)
    conn.execute("COMMIT")
    return path, conn


def test_zero_ledger_foreign_keys(advisor_db):
    _, conn = advisor_db
    allowed = {"goals", "goal_versions", "advisor_info_sets",
               "advisor_suggestions"}
    for table in ADVISOR_TABLES:
        refs = {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
        assert refs <= allowed, f"{table} 存在越区 FK: {refs - allowed}"
        assert not (refs & {"assets", "asset_items", "principal_changes",
                            "transfer_events", "lineage_links",
                            "asset_item_owner_changes"})


def test_0019_double_run_idempotent_and_settings_seed(advisor_db):
    path, conn = advisor_db
    conn.isolation_level = None
    conn.execute("BEGIN")
    M0019.upgrade(conn)
    conn.execute("COMMIT")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name LIKE 'advisor_%'")}
    assert set(ADVISOR_TABLES) <= tables
    row = conn.execute(
        "SELECT auto_enabled, cooldown_days, default_horizon"
        " FROM advisor_settings").fetchall()
    assert row == [(0, 7, "mid")]
    triggers = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
        " AND name LIKE 'advisor_%'")}
    assert "advisor_suggestions_content_guard" in triggers
    for table in ("advisor_info_sets", "advisor_info_entries",
                  "advisor_suggestion_items", "advisor_decisions"):
        assert f"{table}_no_update" in triggers
        assert f"{table}_no_delete" in triggers


# ---------------------------------------------------------------- 触发器逐表


def _seed_info_set(conn, sha="a" * 64):
    conn.execute(
        "INSERT INTO advisor_info_sets (created_at, builder_version,"
        " entry_count, payload_sha256) VALUES ('t','advisor.info_set.v1',0,?)",
        (sha,))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _seed_suggestion(conn):
    conn.execute(
        "INSERT INTO advisor_suggestions (trigger_kind, dedupe_key,"
        " status, created_at) VALUES ('manual','manual:x','generating','t')")
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_immutable_tables_reject_update_and_delete(advisor_db):
    _, conn = advisor_db
    info_id = _seed_info_set(conn)
    conn.execute(
        "INSERT INTO advisor_info_entries (info_set_id,key,kind,label,value,"
        "unit,trust,source) VALUES (?,'nw.total','money','净资产','1.00',"
        "'CNY','measured','ledger')", (info_id,))
    suggestion_id = _seed_suggestion(conn)
    conn.execute(
        "INSERT INTO advisor_suggestion_items (suggestion_id,seq,subject_kind,"
        "direction,rationale_template,rationale_rendered,info_refs_json)"
        " VALUES (?,1,'narrative','hold','r','r','[\"nw.total\"]')",
        (suggestion_id,))
    conn.execute(
        "INSERT INTO advisor_decisions (suggestion_id,action,decided_at,actor)"
        " VALUES (?,'accept','t','owner')", (suggestion_id,))
    conn.commit()

    for statement in (
        "UPDATE advisor_info_sets SET entry_count=9 WHERE id=1",
        "DELETE FROM advisor_info_sets WHERE id=1",
        "UPDATE advisor_info_entries SET value='9.00' WHERE id=1",
        "DELETE FROM advisor_info_entries WHERE id=1",
        "UPDATE advisor_suggestion_items SET direction='hold' WHERE id=1",
        "DELETE FROM advisor_suggestion_items WHERE id=1",
        "UPDATE advisor_decisions SET actor='system' WHERE id=1",
        "DELETE FROM advisor_decisions WHERE id=1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)


def test_suggestion_content_write_once_outside_generating(advisor_db):
    _, conn = advisor_db
    suggestion_id = _seed_suggestion(conn)
    conn.commit()

    # 生成期：内容可写
    conn.execute("UPDATE advisor_suggestions SET title='示例标题' WHERE id=?",
                 (suggestion_id,))
    conn.execute("UPDATE advisor_suggestions SET status='awaiting_review'"
                 " WHERE id=?", (suggestion_id,))
    conn.commit()

    content_updates = (
        "title='改'", "summary_template='改'", "summary_rendered='改'",
        "gate_json='{}'", "gate_version='v2'", "provider_label='改'",
        "dedupe_key='manual:y'", "horizon='long'",
        "goal_version_id=9", "info_set_id=9", "prompt_version='改'",
        "model='改'", "request_id='改'", "trigger_ref='改'",
    )
    for assignment in content_updates:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE advisor_suggestions SET {assignment}"
                         f" WHERE id=?", (suggestion_id,))

    # 状态/时间戳列恒可改
    conn.execute(
        "UPDATE advisor_suggestions SET status='accepted',"
        " expires_at='2026-11-30', generated_at='t', closed_at='t'"
        " WHERE id=?", (suggestion_id,))
    conn.commit()
    assert conn.execute("SELECT status FROM advisor_suggestions").fetchone()[0] \
        == "accepted"


def test_terminal_decision_partial_unique(advisor_db):
    _, conn = advisor_db
    suggestion_id = _seed_suggestion(conn)
    conn.execute(
        "INSERT INTO advisor_decisions (suggestion_id,action,decided_at,actor)"
        " VALUES (?,'accept','t','owner')", (suggestion_id,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO advisor_decisions (suggestion_id,action,decided_at,"
            "actor) VALUES (?,'reject','t','owner')", (suggestion_id,))
    # execution_note 不受终态唯一约束
    conn.execute(
        "INSERT INTO advisor_decisions (suggestion_id,action,decided_at,actor,"
        "execution_status) VALUES (?,'execution_note','t','owner','partial')",
        (suggestion_id,))
    conn.commit()