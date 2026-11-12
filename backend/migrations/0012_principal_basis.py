"""0012：本金口径变更史＋assets.cost_basis 冗余列（v5 二期·子步 2.1，规格 .scratch/v5-appreciation/spec.md §2.1）。

- `principal_changes`：口径变更史＝权威（append-only，BEFORE UPDATE/DELETE 触发器
  RAISE(ABORT)，同 0009 模式）。new_amount **带符号**（注入正/撤出负，可为负＝净撤出）；
  effective_date（R-36 生效日期）；reason 必填（R-9 人裁决标注）；
  **origin DEFAULT 'external'（预留 external|transfer|lineage）＋transfer_event_id NULL**
  ——2.3/2.4 零返工的关键预留（§3），本步只写 external。
- `assets.cost_basis`：ledger 写单点建行时冗余「当日生效口径」；**存量行不回填**
  （恒 NULL）——读端统一走 `principal_service.resolve_cost_basis(item_id, d)`：
  行值优先、NULL 回退变更史。
- 更正＝追加：录错再 POST 一条（解析按 effective_date DESC, id DESC 决胜）；无 update/delete。
- 全守卫幂等（IF NOT EXISTS / PRAGMA 列检查）；无 CLAIM 标记＝首跑也实跑。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS principal_changes ("
        "  id INTEGER PRIMARY KEY,"
        "  asset_item_id INTEGER NOT NULL REFERENCES asset_items (id),"
        "  new_amount NUMERIC(15, 2) NOT NULL,"              # 带符号：注入正/撤出负
        "  effective_date DATE NOT NULL,"                    # R-36 生效日期
        "  reason TEXT NOT NULL,"                            # R-9 人裁决必填
        "  evidence_ref INTEGER REFERENCES screenshot_evidence (id),"
        "  origin TEXT NOT NULL DEFAULT 'external',"         # 预留 external|transfer|lineage
        "  transfer_event_id INTEGER,"                       # 预留：2.3 转移事件回填
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_principal_item_eff"
        " ON principal_changes (asset_item_id, effective_date, id)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS principal_changes_no_update"
        " BEFORE UPDATE ON principal_changes"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'principal_changes is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS principal_changes_no_delete"
        " BEFORE DELETE ON principal_changes"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'principal_changes is append-only: DELETE forbidden');"
        " END"
    )

    # assets.cost_basis：列存在性守卫（全新库 create_all 已含；v4 迁移库此处补列）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
    if "cost_basis" not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN cost_basis NUMERIC(15, 2)")
