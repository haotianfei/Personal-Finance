"""0029：凭证来源组改外键收口 —— `source_group` 字符串快照 → `source_group_id`（L2）。

规格：`.scratch/source-group-fk/spec.md`（2026-11-05 冻结）
上游：ADR-0019 D3（来源组与机构分层）、ADR-0020 D3（真理源收口范式）

背景：`screenshot_evidence.source_group` 一直是 `VARCHAR(50)` 无外键的**字符串快照**，
而组的身份是 `source_groups.slug`。两者靠 `routers/source_groups.py` 里的改名级联维持
同步——级联是后补的补丁，漏一次就有凭证指向不存在的组（业主当年把种子组从拼音 slug
改成规范 slug 时正是这样留下 3 条悬空凭证）。级联另一种失败模式更隐蔽：它成功或失败
都不影响查询（查询走字符串），所以漂移不会被发现。

本迁移把「这张凭证属于哪个组」收敛成**一列外键**：

- 新增 `source_group_id INTEGER NOT NULL REFERENCES source_groups(id)`；
- **删除** `source_group` 字符串列（同一事实只存一处；组改名由 id 天然免疫，不再需要
  任何同步代码——`routers/source_groups.py` 的改名级联整段删除）；
- 索引 `ix_screenshot_evidence_group_date` 改建在 `(source_group_id, data_date)` 上。

否决过的方案（见 spec §2）：保留字符串 + 新增可空 id 双写（两处真相，漂移不报错）；
用 `ALTER TABLE ADD COLUMN` 加可空列（同上，只是换皮）；冻结 slug 不可改（用永久脏
标识换安全，且不解决「引用不是 id」的根因）；按 label 关联（label 可改且不唯一）。

前置与安全：

- **必须 `PRAGMA foreign_keys=off`**：该表被 4 张表外键引用（`assets.source_evidence_id`、
  `audit_log.evidence_ref`、`principal_changes.evidence_ref`、`transfer_events.evidence_ref`），
  drop 父表时 FK 若开启会级联删行或直接报错。与 0008/0026/0027 同口径。
- **悬空 fail-fast**：`NOT NULL` 装不下「slug 在字典里找不到」的行。迁移先统计悬空数，
  >0 立即抛错中止（整个迁移事务回滚），由人工先处置数据。**不**静默置 NULL（违反 NOT NULL
  语义，等于把本迁移降级成被否决的双写方案），**也**不自动建「未知组」（污染用户可选字典）。
- **12 步重建**：加 NOT NULL 外键只能重建表（SQLite 无法 ALTER 约束）。29 行规模，重建后
  立刻做 `PRAGMA foreign_key_check`（必须为空）与 `integrity_check`、行数对账。
- **幂等**：已是新结构（有 `source_group_id`、无 `source_group`）→ 返回 `noop`；表不存在
  → 返回 `skipped`。runner 不会重跑已登记版本，此处守卫只为人工重放安全。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "screenshot_evidence"
TMP = TABLE + "__new"
INDEX = "ix_screenshot_evidence_group_date"

# (列名, 列定义) —— 新表结构；顺序即 INSERT ... SELECT 的顺序
COLUMNS: list[tuple[str, str]] = [
    ("id", "INTEGER NOT NULL"),
    ("source_group_id", "INTEGER NOT NULL REFERENCES source_groups(id)"),
    ("file_path", "VARCHAR(500) NOT NULL"),
    ("file_sha256", "VARCHAR(64) NOT NULL"),
    ("original_filename", "VARCHAR(255)"),
    ("data_date", "DATE"),
    ("anchor_amount", "NUMERIC(15, 2)"),
    ("supplement_rows", "TEXT"),
    ("engine", "VARCHAR(10)"),
    ("created_at", "DATETIME NOT NULL"),
]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _create_sql() -> str:
    body = ",\n\t".join(f'"{name}" {ddl}' for name, ddl in COLUMNS)
    return (
        f'CREATE TABLE "{TMP}" (\n\t{body},\n\tPRIMARY KEY (id),\n'
        "\tCONSTRAINT ck_evidence_engine CHECK (engine IS NULL OR engine IN ('ocr','ai'))\n)"
    )


def upgrade(conn) -> str:
    if not _table_exists(conn, TABLE):
        return "skipped"

    cols = _columns(conn, TABLE)
    if "source_group_id" in cols and "source_group" not in cols:
        return "noop"          # 已是目标结构
    if "source_group" not in cols:
        # 结构不可识别：既不是 0029 之前（无旧列）也不是之后（无新列）。
        # 这类库多为测试桩表或旁路程序自建的同名表——迁移对"形状不在预期"应幂等跳过，
        # fail-fast 只留给"数据与约束不一致"（悬空凭证），否则整条迁移链会被一张无关桩表炸掉。
        log.warning(
            "0029 跳过：%s 既无 source_group 也无 source_group_id，结构不可识别", TABLE)
        return "skipped"
    if not _table_exists(conn, "source_groups"):
        # 旧列在、父表却不在：本库不完整（source_groups 自 v4 起就存在），无从建立外键。
        # 跳过而不中止——真跑应用时 ORM 会因缺 source_group_id 列立刻暴露问题。
        log.warning(
            "0029 跳过：%s 有 source_group 列但缺少 source_groups，无法建立外键", TABLE)
        return "skipped"
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("0029 要求 PRAGMA foreign_keys=off 下执行表重建")

    total = conn.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    dangling = conn.execute(
        f'SELECT count(*) FROM "{TABLE}" e WHERE NOT EXISTS '
        "(SELECT 1 FROM source_groups g WHERE g.slug = e.source_group)"
    ).fetchone()[0]
    if dangling:
        raise RuntimeError(
            f"0029 中止：{dangling} 条凭证的 source_group 在 source_groups 中不存在"
            f"（共 {total} 行）。NOT NULL 外键无法容纳这些行，请先人工处置"
            "（补建组 / 合并到现有组 / 删除重复凭证），再重跑迁移。"
            "本迁移刻意不静默置 NULL、也不自动建占位组。"
        )

    conn.execute(_create_sql())
    rest = [name for name, _ in COLUMNS if name not in ("id", "source_group_id")]
    rest_cols = ", ".join(f'"{c}"' for c in rest)
    rest_vals = ", ".join(f'e."{c}"' for c in rest)
    conn.execute(
        f'INSERT INTO "{TMP}" (id, source_group_id, {rest_cols}) '
        f'SELECT e.id, (SELECT g.id FROM source_groups g WHERE g.slug = e.source_group), {rest_vals} '
        f'FROM "{TABLE}" e'
    )
    copied = conn.execute(f'SELECT count(*) FROM "{TMP}"').fetchone()[0]
    if copied != total:
        raise RuntimeError(f"0029 中止：复制行数不符（源 {total} / 新 {copied}）")

    conn.execute(f'DROP TABLE "{TABLE}"')
    conn.execute(f'ALTER TABLE "{TMP}" RENAME TO "{TABLE}"')
    conn.execute(f'CREATE INDEX {INDEX} ON "{TABLE}" (source_group_id, data_date)')

    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"0029 中止：重建后 foreign_key_check 非空 {problems[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0029 中止：integrity_check={integrity}")
    nulls = conn.execute(
        f'SELECT count(*) FROM "{TABLE}" WHERE source_group_id IS NULL').fetchone()[0]
    if nulls:
        raise RuntimeError(f"0029 中止：重建后仍有 {nulls} 行 source_group_id 为 NULL")

    log.info("evidence_source_group_fk: rebuilt rows=%d", copied)
    return f"rebuilt rows={copied}"