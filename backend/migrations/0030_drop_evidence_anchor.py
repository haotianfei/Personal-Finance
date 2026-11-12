"""0030：删除死字段 `screenshot_evidence.anchor_amount` —— 12 步重建（L2）。

规格：`.scratch/evidence-manager/spec.md` D7（2026-11-05 冻结）
上游：ADR-0021（业主裁决：该字段无法承担 R-12 赋予的对账职责，删除优于留考据）

背景：`anchor_amount` 是「截图上的汇总金额」快照，设计意图（`business-architecture.md`
R-12）是当对账锚点与系统合计比对。实际从未通用化——提取逻辑硬编码单一 App 文案，
通用标签集只用于**排除**汇总行、从未用于提取；全仓也没有任何比对/报警/导出决策读它。
运行库 29 条凭证里只有 1 条有值。更根本的理由：截图总额常含「预计可借」「昨日收益」
「已赎回仍展示」等有意排除项，「合计 − 锚点」的差值无法区分「解析漏读」与「主动剔除」，
一个分不清对错的报警条件只会制造假阳/假阴（ADR-0021 §3）。

本迁移只做一件事：**物理删列**（SQLite 无法 `DROP COLUMN` 带 CHECK/外键约束的表列，
只能重建）。没有数据搬迁、没有口径变更——该列从未参与任何计算。

守卫与安全（照 0029 同型）：

- **必须 `PRAGMA foreign_keys=off`**：该表被 4 张表外键引用（`assets.source_evidence_id`、
  `audit_log.evidence_ref`、`principal_changes.evidence_ref`、`transfer_events.evidence_ref`），
  重建（DROP 父表 + RENAME）在 FK 开启下会级联删行或直接报错。runner 的连接默认 off，
  此守卫只为人工重放安全。
- **幂等**：已无 `anchor_amount` → `noop`；表不存在 → `skipped`。
- **结构不可识别 → `skipped`**（不重建）：没有 `source_group_id` 的表既不是 0030 之前
  （0029 之后必有该列）也不是之后，多为测试桩表或旁路程序自建同名表。这类形状若被
  照模板重建，等于把 0029 的活偷偷干一遍；而 0029 回归重跑会立刻重复建表——所以按
  0029「形状不在预期则幂等跳过」的先例处置（fail-fast 只留给数据与约束不一致）。
- **`source_groups` 缺失 → `skipped`**：重建后仍保留 `REFERENCES source_groups(id)`，
  父表不在时 `foreign_key_check` 无从校验（桩库形状）。真跑应用时 ORM 会立刻暴露缺表。
- **12 步**：① 表存在守卫 ② 列集合读取 / 无该列 → noop ③ 结构可识别守卫
  ④ FK 父表守卫 ⑤ `PRAGMA foreign_keys` 必须 OFF ⑥ 记迁移前行数 ⑦ 建影子表
  ⑧ `INSERT ... SELECT` 复制 ⑨ 行数对账 ⑩ 删旧表 + 影子表改名 ⑪ 重建索引
  ⑫ 迁移后校验（列集合与索引逐字、`ck_evidence_engine` 仍在、`foreign_key_check` 空、
  `integrity_check=ok`）。
- 事务由 runner（`services/db_migration_service._run_script`）负责，本脚本不 commit；
  任一断言失败即抛错 → 整事务回滚，迁移前备份（runner 自动前置备份）是唯一回退路径
  （删列不可回退，见 ADR-0021 §5）。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "screenshot_evidence"
TMP = TABLE + "__new"
INDEX = "ix_screenshot_evidence_group_date"
CHECK_NAME = "ck_evidence_engine"

# (列名, 列定义) —— 新表结构＝0029 的重建目标减去 anchor_amount；顺序即 INSERT ... SELECT 的顺序
COLUMNS: list[tuple[str, str]] = [
    ("id", "INTEGER NOT NULL"),
    ("source_group_id", "INTEGER NOT NULL REFERENCES source_groups(id)"),
    ("file_path", "VARCHAR(500) NOT NULL"),
    ("file_sha256", "VARCHAR(64) NOT NULL"),
    ("original_filename", "VARCHAR(255)"),
    ("data_date", "DATE"),
    ("supplement_rows", "TEXT"),
    ("engine", "VARCHAR(10)"),
    ("created_at", "DATETIME NOT NULL"),
]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _create_sql() -> str:
    body = ",\n\t".join(f'"{name}" {ddl}' for name, ddl in COLUMNS)
    return (
        f'CREATE TABLE "{TMP}" (\n\t{body},\n\tPRIMARY KEY (id),\n'
        f"\tCONSTRAINT {CHECK_NAME} CHECK (engine IS NULL OR engine IN ('ocr','ai'))\n)"
    )


def _index_info(conn, name: str) -> list[tuple]:
    return [(r[0], r[2]) for r in conn.execute(f'PRAGMA index_info("{name}")')]


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        return "skipped"

    # ② 已无该列 → 幂等 noop
    cols = _columns(conn, TABLE)
    if "anchor_amount" not in cols:
        return "noop"

    # ③ 结构可识别守卫：0030 之前的目标形状必有 source_group_id（0029 建）；没有则不是本迁移的活
    if "source_group_id" not in cols:
        log.warning(
            "0030 跳过：%s 有 anchor_amount 但无 source_group_id，结构不可识别"
            "（多为桩表或 0029 尚未应用），不按模板重建", TABLE)
        return "skipped"

    # ④ FK 父表守卫：重建后仍带 REFERENCES source_groups(id)
    if not _table_exists(conn, "source_groups"):
        log.warning(
            "0030 跳过：%s 缺少外键父表 source_groups，无法复核外键", TABLE)
        return "skipped"

    # ⑤ 重建（DROP 父表）必须在 FK 关闭下进行
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("0030 要求 PRAGMA foreign_keys=off 下执行表重建")

    # ⑥ 迁移前行数（唯一的数据对账基线）
    total = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]

    # ⑦ 建影子表
    conn.execute(_create_sql())

    # ⑧ 复制行（列清单显式列出：anchor_amount 被排除在外＝删列本身）
    kept = [name for name, _ in COLUMNS]
    col_list = ", ".join(f'"{c}"' for c in kept)
    conn.execute(
        f'INSERT INTO "{TMP}" ({col_list}) SELECT {col_list} FROM "{TABLE}"'
    )

    # ⑨ 行数对账
    copied = conn.execute(f'SELECT count(*) FROM "{TMP}"').fetchone()[0]
    if copied != total:
        raise RuntimeError(f"0030 中止：复制行数不符（源 {total} / 新 {copied}）")

    # ⑩ 删旧表 + 影子表改名（名称与引用不变，4 处外键指向自动跟随）
    conn.execute(f'DROP TABLE "{TABLE}"')
    conn.execute(f'ALTER TABLE "{TMP}" RENAME TO "{TABLE}"')

    # ⑪ 重建索引（DROP TABLE 会带走旧索引）
    conn.execute(f'CREATE INDEX {INDEX} ON "{TABLE}" (source_group_id, data_date)')

    # ⑫ 迁移后校验
    new_cols = _columns(conn, TABLE)
    if new_cols != kept:
        raise RuntimeError(f"0030 中止：重建后列集合不符（期望 {kept} / 实际 {new_cols}）")
    idx = _index_info(conn, INDEX)
    if idx != [(0, "source_group_id"), (1, "data_date")]:
        raise RuntimeError(f"0030 中止：重建后索引 {INDEX} 列不符 {idx}")
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()[0]
    if CHECK_NAME not in (ddl or ""):
        raise RuntimeError(f"0030 中止：重建后丢失约束 {CHECK_NAME}")
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"0030 中止：重建后 foreign_key_check 非空 {problems[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0030 中止：integrity_check={integrity}")

    log.info("drop_evidence_anchor: rebuilt rows=%d", copied)
    return f"rebuilt rows={copied}"