"""0019：建议隔离区（7 张 `advisor_*` 表＋写保护触发器＋settings 种子）。

规格：`.scratch/phase3-advisor/spec.md` v1.0 §3.1/§3.2/§8.1；ADR-0013 D2/D9。

- 表：info_sets/entries、suggestions/items、decisions、jobs、settings。
- 隔离：仅对参考区 `goals/goal_versions` 建 FK；账本实体只用文本 `source_ref`，
  **零账本 FK**（测试以 `PRAGMA foreign_key_list` 断言）。
- 写保护：
  * info_sets / info_entries / suggestion_items / decisions → 拒 UPDATE/DELETE；
  * suggestions → 生成期（`status='generating'`）之外改内容列 ABORT；
    `status/expires_at/generated_at/closed_at` 恒可改（状态机在服务层）；
  * jobs 可清理（无触发器）。
- `advisor_settings` 单行种子 `(1, auto_enabled=0, cooldown_days=7, mid)`。
- 全 DDL IF NOT EXISTS；无 CLAIM、无 down；双跑幂等。
"""
from datetime import datetime

_IMMUTABLE_TABLES = (
    "advisor_info_sets", "advisor_info_entries",
    "advisor_suggestion_items", "advisor_decisions",
)

# 建议单内容列（其余 status/expires_at/generated_at/closed_at 恒可改）
_SUGGESTION_CONTENT_COLUMNS = (
    "id", "info_set_id", "goal_id", "goal_version_id", "trigger_kind",
    "trigger_ref", "dedupe_key", "horizon", "title", "summary_template",
    "summary_rendered", "gate_json", "gate_version", "prompt_version",
    "provider_id", "provider_label", "model", "created_at", "request_id",
)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _create_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_info_sets ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  created_at TEXT NOT NULL,"
        "  as_of DATE,"
        "  goal_id INTEGER REFERENCES goals (id),"
        "  goal_version_id INTEGER REFERENCES goal_versions (id),"
        "  builder_version TEXT NOT NULL,"
        "  entry_count INTEGER NOT NULL,"
        "  payload_sha256 CHAR(64) NOT NULL,"
        "  meta_json TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_info_entries ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  info_set_id INTEGER NOT NULL REFERENCES advisor_info_sets (id),"
        "  key TEXT NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  label TEXT NOT NULL,"
        "  value TEXT NOT NULL,"
        "  value_scaled TEXT,"
        "  unit TEXT NOT NULL,"
        "  trust TEXT NOT NULL,"
        "  source TEXT NOT NULL,"
        "  source_ref TEXT,"
        "  as_of DATE,"
        "  UNIQUE (info_set_id, key)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_advisor_entries_set"
        " ON advisor_info_entries (info_set_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_suggestions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  info_set_id INTEGER REFERENCES advisor_info_sets (id),"
        "  goal_id INTEGER REFERENCES goals (id),"
        "  goal_version_id INTEGER REFERENCES goal_versions (id),"
        "  trigger_kind TEXT NOT NULL,"
        "  trigger_ref TEXT,"
        "  dedupe_key TEXT NOT NULL UNIQUE,"
        "  horizon TEXT,"
        "  status TEXT NOT NULL DEFAULT 'generating'"
        "    CHECK (status IN ('generating','awaiting_review','accepted',"
        "                     'rejected','expired','gate_failed',"
        "                     'generation_failed')),"
        "  title TEXT,"
        "  summary_template TEXT,"
        "  summary_rendered TEXT,"
        "  gate_json TEXT,"
        "  gate_version TEXT,"
        "  prompt_version TEXT,"
        "  provider_id INTEGER,"
        "  provider_label TEXT,"
        "  model TEXT,"
        "  expires_at DATE,"
        "  created_at TEXT NOT NULL,"
        "  generated_at TEXT,"
        "  closed_at TEXT,"
        "  request_id TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_advisor_suggestions_status"
        " ON advisor_suggestions (status)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_suggestion_items ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  suggestion_id INTEGER NOT NULL REFERENCES advisor_suggestions (id),"
        "  seq INTEGER NOT NULL,"
        "  subject_kind TEXT NOT NULL"
        "    CHECK (subject_kind IN ('attribute','goal','threshold','narrative')),"
        "  subject_ref TEXT,"
        "  direction TEXT NOT NULL"
        "    CHECK (direction IN ('increase','decrease','hold','rebalance')),"
        "  rationale_template TEXT NOT NULL,"
        "  rationale_rendered TEXT NOT NULL,"
        "  info_refs_json TEXT NOT NULL,"
        "  UNIQUE (suggestion_id, seq)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_decisions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  suggestion_id INTEGER NOT NULL REFERENCES advisor_suggestions (id),"
        "  action TEXT NOT NULL"
        "    CHECK (action IN ('accept','reject','expire','execution_note')),"
        "  reason TEXT,"
        "  execution_status TEXT"
        "    CHECK (execution_status IS NULL OR execution_status IN"
        "           ('done','partial','not_done')),"
        "  decided_at TEXT NOT NULL,"
        "  actor TEXT NOT NULL CHECK (actor IN ('owner','system')),"
        "  request_id TEXT"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_advisor_terminal_decision"
        " ON advisor_decisions (suggestion_id)"
        " WHERE action IN ('accept','reject','expire')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_jobs ("
        "  job_id TEXT PRIMARY KEY,"
        "  suggestion_id INTEGER,"
        "  status TEXT NOT NULL DEFAULT 'queued'"
        "    CHECK (status IN ('queued','running','done','error')),"
        "  stage TEXT NOT NULL DEFAULT 'queued'"
        "    CHECK (stage IN ('queued','info_set','llm','gate','done')),"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  error TEXT,"
        "  created_at TEXT NOT NULL,"
        "  started_at TEXT,"
        "  finished_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_advisor_jobs_status"
        " ON advisor_jobs (status)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS advisor_settings ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  auto_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auto_enabled IN (0,1)),"
        "  cooldown_days INTEGER NOT NULL DEFAULT 7,"
        "  default_horizon TEXT NOT NULL DEFAULT 'mid',"
        "  last_auto_at TEXT,"
        "  updated_at TEXT NOT NULL"
        ")"
    )


def _create_triggers(conn):
    for table in _IMMUTABLE_TABLES:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update"
            f" BEFORE UPDATE ON {table}"
            f" FOR EACH ROW BEGIN"
            f"   SELECT RAISE(ABORT, '{table} is append-only: UPDATE forbidden');"
            f" END"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete"
            f" BEFORE DELETE ON {table}"
            f" FOR EACH ROW BEGIN"
            f"   SELECT RAISE(ABORT, '{table} is append-only: DELETE forbidden');"
            f" END"
        )

    changed = " OR ".join(
        f"NEW.{col} IS NOT OLD.{col}" for col in _SUGGESTION_CONTENT_COLUMNS)
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS advisor_suggestions_content_guard"
        " BEFORE UPDATE ON advisor_suggestions"
        " FOR EACH ROW WHEN OLD.status <> 'generating'"
        " BEGIN"
        "   SELECT CASE WHEN (" + changed + ") THEN"
        "     RAISE(ABORT, 'advisor_suggestions content is write-once"
        " outside generating')"
        "   END;"
        " END"
    )


def _seed_settings(conn):
    conn.execute(
        "INSERT OR IGNORE INTO advisor_settings"
        " (id, auto_enabled, cooldown_days, default_horizon, last_auto_at,"
        "  updated_at) VALUES (1, 0, 7, 'mid', NULL, ?)",
        (datetime.now().isoformat(),)
    )


def upgrade(conn):
    _create_tables(conn)
    _create_triggers(conn)
    _seed_settings(conn)