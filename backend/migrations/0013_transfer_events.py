"""0013：内部买卖转移事件（v5 二期·子步 2.3，规格 .scratch/v5-appreciation/spec.md §2.3）。

- `transfer_events`：登记凭证行（from/to 两 item、amount **恒正**（CHECK，方向由
  from/to 表达）、event_date 登记日＝快照日、reason 必填、evidence_ref NULL、
  created_at）；append-only：BEFORE UPDATE/DELETE 触发器 RAISE(ABORT)，同 0009/0012
  模式。**更正＝登记反向转移**（追加不改史），无应用层 update/delete 端口。
- 登记的服务层不变量（不在 DB 层表达）：同事务生成两条 principal_changes
  （from −amount / to +amount，origin='transfer'、transfer_event_id 回填——0012
  预留列本步启用），两侧本金等额、净值不变、不产生流水（R-34/拍板 9）。
- 全守卫幂等（IF NOT EXISTS）；无 CLAIM 标记＝首跑也实跑；回滚＝revert＋本表残留
  无害（0012 预留列已就位，无耦合断裂）。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS transfer_events ("
        "  id INTEGER PRIMARY KEY,"
        "  from_item_id INTEGER NOT NULL REFERENCES asset_items (id),"
        "  to_item_id INTEGER NOT NULL REFERENCES asset_items (id),"
        "  amount NUMERIC(15, 2) NOT NULL CHECK (amount > 0),"   # 恒正：方向由 from/to 表达
        "  event_date DATE NOT NULL,"                             # 登记日＝快照日（R-34 注）
        "  reason TEXT NOT NULL,"
        "  evidence_ref INTEGER REFERENCES screenshot_evidence (id),"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_transfer_from"
        " ON transfer_events (from_item_id, event_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_transfer_to"
        " ON transfer_events (to_item_id, event_date)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS transfer_events_no_update"
        " BEFORE UPDATE ON transfer_events"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'transfer_events is append-only: UPDATE forbidden');"
        " END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS transfer_events_no_delete"
        " BEFORE DELETE ON transfer_events"
        " FOR EACH ROW BEGIN"
        "   SELECT RAISE(ABORT, 'transfer_events is append-only: DELETE forbidden');"
        " END"
    )
