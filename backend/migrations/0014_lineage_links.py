"""0014：血缘链接（v5 二期·子步 2.4，规格 .scratch/v5-appreciation/spec.md §2.4）。

- `lineage_links`：前身/后继链（from_item_id＝旧项 → to_item_id＝新项，**创建即固定**）：
  link_type CHECK ∈ merge|split|revision；split_ratio NULL（仅 split 携带，(0,1] 由
  服务层校验）；note 可空；created_at。
- append-only：BEFORE UPDATE/DELETE 触发器 RAISE(ABORT)，同 0009/0012/0013 模式。
  血缘不可改＝拍板 8；**改名不走血缘**（显示名史已承载，拍板 10）——本表无 rename 类型。
- 服务层同事务编排（不在 DB 层表达）：旧项归档＋新项创建＋链＋口径承接
  （principal_changes origin='lineage'，0012 预留值本步启用；transfer_event_id 恒 NULL）。
- 全守卫幂等（IF NOT EXISTS）；无 CLAIM 标记＝首跑也实跑；回滚＝revert（残留无害）。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lineage_links ("
        "  id INTEGER PRIMARY KEY,"
        "  from_item_id INTEGER NOT NULL REFERENCES asset_items (id),"   # 旧（前身）
        "  to_item_id INTEGER NOT NULL REFERENCES asset_items (id),"     # 新（后继）
        "  link_type TEXT NOT NULL"
        "    CHECK (link_type IN ('merge', 'split', 'revision')),"       # 拍板 8/10：无 rename
        "  split_ratio NUMERIC(6, 4),"                                   # 仅 split；(0,1] 服务层校验
        "  note TEXT,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_lineage_from"
        " ON lineage_links (from_item_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_lineage_to"
        " ON lineage_links (to_item_id, id)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS lineage_links_no_update"
        " BEFORE UPDATE ON lineage_links"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'lineage_links is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS lineage_links_no_delete"
        " BEFORE DELETE ON lineage_links"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'lineage_links is append-only: DELETE forbidden');"
        " END"
    )
