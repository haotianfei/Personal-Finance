"""0001：删除 V1 遗留表（搬运自 db_migration_service.drop_v1_tables）。

幂等：DROP TABLE IF EXISTS。conn 为 stdlib sqlite3 连接，事务由 runner 管理。
V1_TABLES 5 表含相互外键依赖，asset_records 排首位先 drop。
"""

V1_TABLES = ["asset_records", "fund_types", "accounts", "liquidity_ratings", "asset_owners"]


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    for t in V1_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
