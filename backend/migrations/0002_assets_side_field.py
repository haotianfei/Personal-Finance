"""0002：assets 增加 side 字段并按历史符号回填（搬运自 migrate_side_field）。

幂等守卫：表不存在（全新库 runner 先于 create_all）或列已存在 → no-op。
回填含 UPDATE：仅在首次添加列时执行。
"""


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    if not cols or "side" in cols:
        return
    conn.execute("ALTER TABLE assets ADD COLUMN side VARCHAR(10) NOT NULL DEFAULT 'asset'")
    conn.execute("UPDATE assets SET side = 'liability' WHERE amount < 0")
    conn.execute("UPDATE assets SET amount = ABS(amount)")
