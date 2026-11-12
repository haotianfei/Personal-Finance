"""0008：身份层落地（v5 一期·步骤 3，规格 .scratch/v5-identity/spec.md §3）。

单事务（runner 保证）＋前置 create_backup(reason="pre_migration", prune=False)
（本脚本无 CLAIM 标记，即便首跑（v4 库）也实际执行）。

步骤：
 1. 建 asset_items / asset_item_names（IF NOT EXISTS）
 2. 回填 item：SELECT DISTINCT asset_name；status 一律 active（归档是人裁决，
    迁移不代做）；channel 初判仅信息列、不参与唯一性（唯一锚＝legacy_name）
 3. 首条显示名史（origin='migration'，effective_from＝该 name 最早快照日）
 4. assets 表重建（12 步法）：asset_item_id NOT NULL（legacy_name 映射回填）、
    superseded_by；删旧 uq(asset_date, asset_name)，建部分唯一索引
    uq_assets_item_date_active + ix_assets_item；**assets.id 原值保留不重编号**
 5. 校验断言（任一失败 raise → runner 回滚＋中止启动＋备份兜底）：
    行数相等、逐快照日净资产合计一致、asset_item_id 零 NULL、
    item 数＝distinct name 数、PRAGMA foreign_key_check 零违规、
    asset_attributes 挂接行数不变。

幂等：DDL IF NOT EXISTS；回填 ON CONFLICT DO NOTHING；重建以 asset_item_id 列
存在性守卫（重建后重跑＝校验直接通过）。

硬化①：重建期间显式要求 PRAGMA foreign_keys=off（runner 连接本默认 off；
事务内无法改此 pragma，故为显式断言而非设置）。
"""
from datetime import datetime

# v4 时代已收敛编目的标记（见 db_migration_service.run_migrations docstring）。
# 本脚本是 v5 新增迁移：**不认领、必实跑**。


def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _net_by_date(conn):
    """逐快照日净资产合计（负债按负号）。→ {date_str: Decimal}"""
    from decimal import Decimal
    rows = conn.execute(
        "SELECT asset_date, SUM(CASE WHEN side='liability' THEN -amount ELSE amount END) "
        "FROM assets GROUP BY asset_date"
    ).fetchall()
    return {str(d): Decimal(str(v or 0)) for d, v in rows}


def _infer_channel(conn, name):
    """channel 初判（信息列，不参与唯一性）：
    1) 该 name 最近快照行挂接的 account 属性名；
    2) 无则按「-」拆首段且命中 account 属性名；
    3) 否则 NULL（unspecified）。

    0027 起 `attributes` / `asset_attributes` 已物理删除（空库重建路径从不写它们），
    两表任一缺失即无旧口径可推 → 直接 NULL（新库的渠道由项级 `channel_id` 承担）。
    """
    tables = _table_names(conn)
    if not {"attributes", "asset_attributes"} <= tables:
        return None
    hit = conn.execute(
        "SELECT at.name FROM assets a"
        " JOIN asset_attributes aa ON aa.asset_id = a.id"
        " JOIN attributes at ON at.id = aa.attribute_id AND at.type='account'"
        " WHERE a.asset_name = ? ORDER BY a.asset_date DESC LIMIT 1", (name,)
    ).fetchone()
    if hit:
        return hit[0]
    if "-" in name:
        head = name.split("-", 1)[0]
        hit = conn.execute(
            "SELECT name FROM attributes WHERE type='account' AND name = ?", (head,)
        ).fetchone()
        if hit:
            return hit[0]
    return None


def upgrade(conn):
    now = datetime.now().isoformat()
    tables = _table_names(conn)

    # 硬化①：重建需 FK off（runner 连接默认 off；事务内无法改，显式断言）
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk_on:
        raise RuntimeError("0008 要求 PRAGMA foreign_keys=off 下执行表重建")

    # ---- 1. 新表（IF NOT EXISTS；create_all 已建则跳过） ----
    conn.execute(
        "CREATE TABLE IF NOT EXISTS asset_items ("
        "  id INTEGER PRIMARY KEY,"
        "  display_name VARCHAR(100) NOT NULL,"
        "  channel VARCHAR(100),"
        "  status VARCHAR(10) NOT NULL DEFAULT 'active',"
        "  legacy_name VARCHAR(100),"
        "  created_at DATETIME NOT NULL,"
        "  updated_at DATETIME NOT NULL,"
        "  CONSTRAINT uq_asset_items_legacy_name UNIQUE (legacy_name)"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_asset_items_status ON asset_items (status)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS asset_item_names ("
        "  id INTEGER PRIMARY KEY,"
        "  item_id INTEGER NOT NULL REFERENCES asset_items (id),"
        "  name VARCHAR(100) NOT NULL,"
        "  effective_from DATE NOT NULL,"
        "  origin VARCHAR(10) NOT NULL,"
        "  created_at DATETIME NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_asset_item_names_item ON asset_item_names (item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_asset_item_names_name ON asset_item_names (name)")

    if "assets" not in tables:
        # 空库（runner 先于 create_all 的全新路径）：无可回填，建表即全部
        return

    # ---- 2. 回填 item（ON CONFLICT 幂等） ----
    # ADR-0019 之后 asset_items 带 ck_asset_items_dims_required：active 要求六维分类齐全，
    # 而此刻回填出来的项没有任何分类。新结构下回填为 draft，等分类补齐再转 active；
    # 旧结构（无 asset_class_id 列）仍写 active，保持历史行为。
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_items)")}
    _status = "draft" if "asset_class_id" in _cols else "active"
    names = [r[0] for r in conn.execute("SELECT DISTINCT asset_name FROM assets ORDER BY asset_name")]
    for name in names:
        channel = _infer_channel(conn, name)
        conn.execute(
            "INSERT INTO asset_items (display_name, channel, status, legacy_name, created_at, updated_at)"
            f" VALUES (?, ?, '{_status}', ?, ?, ?)"
            " ON CONFLICT(legacy_name) DO NOTHING",
            (name, channel, name, now, now),
        )

    # ---- 3. 首条显示名史（effective_from＝该 name 最早快照日） ----
    conn.execute(
        "INSERT INTO asset_item_names (item_id, name, effective_from, origin, created_at)"
        " SELECT i.id, i.legacy_name, a.min_date, 'migration', :now"
        " FROM asset_items i"
        " JOIN (SELECT asset_name, MIN(asset_date) AS min_date FROM assets GROUP BY asset_name) a"
        "   ON a.asset_name = i.legacy_name"
        " WHERE i.legacy_name IS NOT NULL"
        "   AND NOT EXISTS (SELECT 1 FROM asset_item_names n WHERE n.item_id = i.id)",
        {"now": now},
    )

    cols = _columns(conn, "assets")
    rebuilt_already = "asset_item_id" in cols

    # ---- 前置基线量（重建前采集；重建跳过时取当前值，校验自然相等） ----
    pre_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    pre_net = _net_by_date(conn)
    pre_attr_links = conn.execute("SELECT COUNT(*) FROM asset_attributes").fetchone()[0] \
        if "asset_attributes" in _table_names(conn) else 0
    item_count = conn.execute("SELECT COUNT(*) FROM asset_items WHERE legacy_name IS NOT NULL").fetchone()[0]

    # ---- 4. assets 表重建（列存在性守卫幂等） ----
    if not rebuilt_already:
        # 兼容列：source_evidence_id/side/created_at/updated_at 理论上已由
        # 0003/0002/建表存在；缺列时以默认值投影，防御未收敛库（全部限定 a. 前缀，
        # 避免与 asset_items 同名列 ambiguous）。
        sel_side = "a.side" if "side" in cols else "'asset'"
        sel_ev = "a.source_evidence_id" if "source_evidence_id" in cols else "NULL"
        sel_created = "a.created_at" if "created_at" in cols else ":now"
        sel_updated = "a.updated_at" if "updated_at" in cols else ":now"

        conn.execute(
            "CREATE TABLE assets_new ("
            "  id INTEGER PRIMARY KEY,"
            "  asset_name VARCHAR(100) NOT NULL,"
            "  amount NUMERIC(15, 2) NOT NULL,"
            "  side VARCHAR(10) NOT NULL DEFAULT 'asset',"
            "  asset_date DATE NOT NULL,"
            "  source_evidence_id INTEGER REFERENCES screenshot_evidence (id),"
            "  asset_item_id INTEGER NOT NULL REFERENCES asset_items (id),"
            "  superseded_by INTEGER REFERENCES assets (id),"
            "  created_at DATETIME NOT NULL,"
            "  updated_at DATETIME NOT NULL"
            ")"
        )
        # assets.id 原值保留（asset_attributes 挂接面依赖，不得重编号）
        conn.execute(
            f"INSERT INTO assets_new (id, asset_name, amount, side, asset_date,"
            f"                      source_evidence_id, asset_item_id, superseded_by,"
            f"                      created_at, updated_at)"
            f" SELECT a.id, a.asset_name, a.amount, {sel_side}, a.asset_date,"
            f"        {sel_ev}, i.id, NULL, {sel_created}, {sel_updated}"
            f"   FROM assets a JOIN asset_items i ON i.legacy_name = a.asset_name",
            {"now": now},
        )
        conn.execute("DROP TABLE assets")
        conn.execute("ALTER TABLE assets_new RENAME TO assets")

    # ---- 索引（幂等；重建分支补建，认领/重跑分支同名跳过） ----
    conn.execute("CREATE INDEX IF NOT EXISTS ix_assets_date ON assets (asset_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_assets_item ON assets (asset_item_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_assets_item_date_active"
        " ON assets (asset_item_id, asset_date) WHERE superseded_by IS NULL"
    )

    # ---- 5. 校验断言（失败 raise → runner 回滚＋fail-fast＋备份兜底） ----
    # 用显式 raise（不用 assert：-O 运行下 assert 会被剥离）
    post_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    if post_count != pre_count:
        raise RuntimeError(f"0008 校验失败：assets 行数不等 {pre_count} → {post_count}")

    nulls = conn.execute("SELECT COUNT(*) FROM assets WHERE asset_item_id IS NULL").fetchone()[0]
    if nulls:
        raise RuntimeError(f"0008 校验失败：asset_item_id 存在 {nulls} 个 NULL")

    post_net = _net_by_date(conn)
    if post_net != pre_net:
        raise RuntimeError(f"0008 校验失败：净资产合计不一致（差异日：{sorted(set(pre_net) ^ set(post_net))}）")

    if item_count != len(names) and not rebuilt_already:
        raise RuntimeError(f"0008 校验失败：item 数 {item_count} ≠ distinct name 数 {len(names)}")

    post_attr_links = conn.execute("SELECT COUNT(*) FROM asset_attributes").fetchone()[0] \
        if "asset_attributes" in _table_names(conn) else 0
    if post_attr_links != pre_attr_links:
        raise RuntimeError(f"0008 校验失败：asset_attributes 挂接行数变化 {pre_attr_links} → {post_attr_links}")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"0008 校验失败：外键完整性违规 {violations[:3]}")
