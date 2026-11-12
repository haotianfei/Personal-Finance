"""0023：读写路径索引补齐 + 显示名史 append-only 兜底。

来源：`.scratch/db-audit/audit-2026-10-29.md`（2026-10-29 审计）
  - D6：20 个 FK 列无前导索引，其中 `audit_log.evidence_ref/request_id`
    （`routers/audit_log.py` 按此过滤）、`assets.superseded_by`（20+ 处
    `IS NULL` 过滤）、`expenses.category_id` 有明确读路径支撑。
  - D3：`asset_item_names` 标称 append-only（ADR-0011「改名不改身份、留史」）
    却无存储层触发器兜底。

**只做加法**：纯 `CREATE INDEX` / `CREATE TRIGGER`，不重建任何表、不改任何行、
不碰账本数据。以下两类结构性修正**故意留待 0024**（需 12 步重建，单独验收）：
  - `CHECK(amount >= 0)` 等枚举/值域约束（ADR-0015 决定值域非负，现有 982 条
    零值行合法，加约束不会失败，但重建 assets 风险高于收益，单独立项）；
  - 死列删除（`assets.updated_at`、`asset_attributes.value`、
    `screenshot_evidence.supplement_rows`）。

全部语句 `IF NOT EXISTS` + 存在性守卫，双跑幂等；无 CLAIM、无 down；
`schema_version` 由 runner 在同一事务内登记，脚本内不重复写。
"""

# (索引名, 表名, 列表达式) —— 命名沿用 ix_<表>_<列> 惯例（存量 idx_* 不在此统一）
_INDEXES = [
    # 账本热路径：全库 20+ 处以 superseded_by IS NULL 过滤，且自引用 FK 无索引
    ("ix_assets_superseded", "assets", "superseded_by"),
    ("ix_assets_evidence", "assets", "source_evidence_id"),
    # 审计查询面（routers/audit_log.py 按 evidence_ref / request_id 过滤）
    ("ix_audit_evidence", "audit_log", "evidence_ref"),
    ("ix_audit_request", "audit_log", "request_id"),
    # 凭证反向追溯（"这张图影响了哪些行"）
    ("ix_principal_evidence", "principal_changes", "evidence_ref"),
    ("ix_transfer_evidence", "transfer_events", "evidence_ref"),
    # 支出域
    ("ix_expenses_category", "expenses", "category_id"),
    ("ix_expenses_account", "expenses", "account_id"),
    ("ix_fixed_expense_category", "fixed_expense_templates", "category_id"),
    ("ix_fixed_expense_account", "fixed_expense_templates", "account_id"),
    # 参考/规划域
    ("ix_goals_scope_owner", "goals", "scope_owner_id"),
    ("ix_allocation_targets_parent", "allocation_targets", "parent_id"),
    ("ix_attributes_parent", "attributes", "parent_id"),
    # 建议隔离区（按 goal / info_set / 复盘聚合读取）
    ("ix_advisor_suggestions_infoset", "advisor_suggestions", "info_set_id"),
    ("ix_advisor_suggestions_goal", "advisor_suggestions", "goal_id, goal_version_id"),
    ("ix_advisor_info_sets_goal", "advisor_info_sets", "goal_id, goal_version_id"),
    ("ix_advisor_reviews_versions", "advisor_reviews",
     "goal_version_id, start_goal_version_id"),
    ("ix_advisor_review_suggestions_suggestion", "advisor_review_suggestions",
     "suggestion_id"),
    # 终态裁决是部分唯一索引（WHERE action IN ('accept','reject','expire')），
    # 按建议查全部裁决时用不上，补常规索引
    ("ix_advisor_decisions_suggestion", "advisor_decisions", "suggestion_id"),
]

_TRIGGERS = [
    (
        "asset_item_names_no_update",
        "CREATE TRIGGER IF NOT EXISTS asset_item_names_no_update"
        " BEFORE UPDATE ON asset_item_names FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT,"
        " 'asset_item_names is append-only: UPDATE forbidden'); END",
    ),
    (
        "asset_item_names_no_delete",
        "CREATE TRIGGER IF NOT EXISTS asset_item_names_no_delete"
        " BEFORE DELETE ON asset_item_names FOR EACH ROW"
        " BEGIN SELECT RAISE(ABORT,"
        " 'asset_item_names is append-only: DELETE forbidden'); END",
    ),
]


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def upgrade(conn):
    tables = _table_names(conn)
    touched: list[str] = []

    for name, table, cols in _INDEXES:
        if table not in tables:
            continue
        if not all(c.strip() in _columns(conn, table) for c in cols.split(",")):
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")
        touched.append(name)

    # 名下史写一次：更新/删除一律 ABORT（与 0009/0012/0013/0014 同模式）
    if "asset_item_names" in tables:
        for name, ddl in _TRIGGERS:
            conn.execute(ddl)
            touched.append(name)

    return touched