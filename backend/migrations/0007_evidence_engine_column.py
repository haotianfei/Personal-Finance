"""0007：screenshot_evidence 增加 engine 列（搬运自 migrate_evidence_engine_column）。

幂等守卫：表不存在或列已存在 → no-op。可空 VARCHAR 直接 ALTER 添加，
存量凭证无需回填（历史行 NULL，语义 = "ocr"，双引擎对等审计）。
"""


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(screenshot_evidence)")}
    if not cols or "engine" in cols:
        return
    conn.execute("ALTER TABLE screenshot_evidence ADD COLUMN engine VARCHAR(10)")
