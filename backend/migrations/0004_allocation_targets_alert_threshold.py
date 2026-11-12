"""0004：allocation_targets 增加 alert_threshold 列（搬运自 migrate_allocation_targets_table）。

幂等守卫：表不存在或列已存在 → no-op。存量行由 DEFAULT 5.00 回填（自定义偏离预警阈值）。
"""


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(allocation_targets)")}
    if not cols or "alert_threshold" in cols:
        return
    conn.execute(
        "ALTER TABLE allocation_targets ADD COLUMN alert_threshold "
        "DECIMAL(5, 2) NOT NULL DEFAULT 5.00"
    )
