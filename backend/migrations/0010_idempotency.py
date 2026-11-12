"""0010：幂等键表＋审计列（v5 步骤 5，spec §3）。

- `idempotency_keys`：key PK / scope（method+path）/ body_hash / status
  pending|done|failed / response_status / response_json / created_at；
  TTL 由应用层每日清理 >7 天（复用 scheduler）。
- `audit_log.idempotency_key`：可空列（幂等键与 request_id 并列落审计，
  audit 本身 append-only 不动）。

无 CLAIM 标记（v5 新增面，首跑也实际执行）；全守卫幂等
（IF NOT EXISTS / PRAGMA 列检查）。audit_log 尚不存在的库跳过 ALTER
（create_all 或 0009 就位后由重跑/新库补齐——本步真库顺序下必然存在）。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idempotency_keys ("
        "  key TEXT PRIMARY KEY,"
        "  scope TEXT NOT NULL,"                      # 例 "POST /api/assets"
        "  body_hash CHAR(64) NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'pending',"   # pending | done | failed
        "  response_status INTEGER,"
        "  response_json TEXT,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_idempotency_created ON idempotency_keys (created_at)"
    )

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "audit_log" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_log)")]
        if "idempotency_key" not in cols:
            conn.execute("ALTER TABLE audit_log ADD COLUMN idempotency_key TEXT")
