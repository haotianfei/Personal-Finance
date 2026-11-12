"""0005：attributes 增加 remind_enabled 列（搬运自 migrate_account_remind_enabled）。

幂等守卫：表不存在或列已存在 → no-op。SQLite 支持带 DEFAULT 的 ADD COLUMN，
存量行由 DEFAULT 1 回填（默认全部参与录入提醒）。
"""


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attributes)")}
    if not cols or "remind_enabled" in cols:
        return
    conn.execute(
        "ALTER TABLE attributes ADD COLUMN remind_enabled "
        "BOOLEAN NOT NULL DEFAULT 1"
    )
