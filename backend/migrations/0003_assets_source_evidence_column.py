"""0003：assets 增加 source_evidence_id 列（搬运自 migrate_source_evidence_column）。

幂等守卫：表不存在或列已存在 → no-op。凭证表 screenshot_evidence 由 create_all 建，
此处仅补资产 → 凭证外键列；SQLite 支持 ALTER ADD COLUMN 带 REFERENCES。
"""


CLAIM = True  # v4 编目：旧 inspect 机制每启动必跑，行为已收敛，可被基线认领


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    if not cols or "source_evidence_id" in cols:
        return
    conn.execute(
        "ALTER TABLE assets ADD COLUMN source_evidence_id INTEGER "
        "REFERENCES screenshot_evidence(id)"
    )
