"""0009：审计日志表（v5 一期·步骤 4，规格 .scratch/v5-audit/spec.md §2）。

audit_log＝不变量 8（全程审计：谁/何时/何操作/前值后值/来源凭证）的承载物。

- 列：id / occurred_at(ISO TEXT) / actor('owner'|'system') / entity / action /
  row_id / before_json / after_json / evidence_ref(可空 FK→screenshot_evidence) /
  request_id / prev_hash（**本步预留列恒 NULL**，三期 hash-chain 启用）
- 索引：(entity, row_id)、(occurred_at)
- **append-only 硬约束**：BEFORE UPDATE/DELETE 触发器 RAISE(ABORT)——
  存储层 write-once，与快照表同哲学；应用层无修改端口。
- 全 DDL IF NOT EXISTS 幂等；create_all 产表（models 只读映射）后重跑本脚本
  仍会补建触发器（CREATE TRIGGER IF NOT EXISTS）。

不回补历史：0008/步骤 3 的既有变更以 schema_version 为准，不造假审计行。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "  id INTEGER PRIMARY KEY,"
        "  occurred_at TEXT NOT NULL,"
        "  actor TEXT NOT NULL,"                    # owner | system
        "  entity TEXT,"                            # asset_snapshot | asset_item | schema | ...
        "  action TEXT,"                            # create|correct|rename|archive|unarchive|delete|apply_row|migration_run|...
        "  row_id INTEGER,"
        "  before_json TEXT,"                       # NULL＝创建；键集见审计形状契约测试
        "  after_json TEXT,"                        # NULL＝删除
        "  evidence_ref INTEGER REFERENCES screenshot_evidence (id),"
        "  request_id TEXT,"
        "  prev_hash CHAR(64)"                      # 三期 hash-chain 预留，本步恒 NULL
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_entity_row ON audit_log (entity, row_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_occurred_at ON audit_log (occurred_at)")

    # append-only：存储层拒绝 UPDATE/DELETE（RAISE(ABORT) 使语句报错并回滚该语句）
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_log_no_update"
        " BEFORE UPDATE ON audit_log"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete"
        " BEFORE DELETE ON audit_log"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'audit_log is append-only: DELETE forbidden');"
        " END"
    )
