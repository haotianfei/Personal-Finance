"""0025：归档生效日——`asset_items.archived_effective_from DATE NULL`（ADR-0017 D2 修订）。

背景：ADR-0017 D2 初版把 `status='archived'` 的资产项**从一切 as-of 日期**剔除，
等价于「历史测量值被追溯抹除」。实测后果：把某项标为已归档后，其归档日之前一整年
的净值一律塌陷（伪造出一段 −1.2M 的坑），比 D2 要修的事故更严重。正确语义是
**归档＝自该日起终止计量，历史测量值保留**，故归档必须携带生效日。

本迁移只加一列，不改任何行、不重建表（SQLite `ALTER TABLE ADD COLUMN` 足够）：

- `archived_effective_from`：归档生效日。语义：
  * `status='archived'` 且日期非 NULL → 该日起不再参与 as-of 聚合（此前照常计量）；
  * `status='archived'` 且日期为 NULL → **归档日未知**（D2 落地前的存量形态，当前
    库 archived=0，无需回填）→ 对所有 as-of 日期保守剔除（保持改前行为）；
  * `status='active'` → 恒为 NULL（服务层状态机 `set_item_status` 保证：归档写入
    日期，解除归档清回 NULL）。
- 索引：**不新增**。归档是极少数行（当前 0），查询形态 `status='archived' AND
  archived_effective_from <= :as_of` 已由 0008 的 `ix_asset_items_status` 先收窄到
  该少数子集；为全表绝大多数为 NULL 的列再加索引没有读路径支撑（同 0023 的
  「有明确读路径才补」纪律）。

幂等：列已存在即跳过（双跑成功）；无 CLAIM、无 down；`schema_version` 由 runner
在同一事务内登记，脚本内不重复写。
"""

TABLE = "asset_items"
COLUMN = "archived_effective_from"


def _table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None


def _columns(conn) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")')}


def upgrade(conn) -> str:
    """加列（幂等）。返回 'added' | 'skipped' | 'missing' 便于测试断言。"""
    if not _table_exists(conn):
        return "missing"
    if COLUMN in _columns(conn):
        return "skipped"
    conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN {COLUMN} DATE')
    return "added"