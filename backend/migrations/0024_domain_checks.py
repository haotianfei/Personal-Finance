"""0024：值域约束落地（12 张表补 19 条 CHECK）。

来源：`.scratch/db-audit/audit-2026-10-29.md` D5/D7（2026-10-29 审计）
  - D7：枚举值域普遍缺 CHECK，与 v5.9 世代（goals/advisor_* 密集 CHECK）
    形成两套纪律；`assets.amount` 的「非负」只有文档（ADR-0015）。
  - 只补约束，**不删列、不改列、不动任何行**。

做法：对每张目标表做 SQLite 12 步重建的最小版——按 `sqlite_master.sql`
原样复制 DDL（逐字节保留既有列定义/默认值/既有约束），仅在末尾追加 CHECK；
显式列名 `INSERT ... SELECT` 搬行；重建原索引（含部分唯一索引）与触发器；
逐表断言行数、索引名集合、触发器名集合不变。

容错：每条 CHECK 声明其依赖列；目标表缺列（历史最小化 fixture / 早期库）
时跳过该条而不炸库；整表无可用 CHECK 时跳过重建。

前置守卫：`assets.amount < 0` 的遗留行会让 CHECK 失败——本脚本先扫描并以
可读错误列出受影响行 id，提示人工归正后再升级（不静默改写账本金额）。

约束清单（值域均有代码常量背书）：
  assets                amount >= 0（ADR-0015）｜side ∈ asset|liability
  asset_items           status ∈ active|archived（R-37）
  asset_item_names      origin ∈ migration|rename
  principal_changes     origin ∈ external|transfer|lineage
  audit_log             actor ∈ owner|system（ADR-0007）
  attributes            type ∈ fund_type|liquidity_rating|account|owner|tag
  screenshot_evidence   engine IS NULL OR ∈ ocr|ai
  allocation_targets    dimension ∈ 已支持 5 类｜percent/threshold ∈ [0,100]
  expenses              expense_type ∈ fixed|large_oneoff｜amount > 0
  fixed_expense_templates cycle ∈ monthly|quarterly|yearly｜amount > 0
  transfer_events       from_item_id <> to_item_id
  lineage_links         from_item_id <> to_item_id

幂等：目标表内已存在同名 CHECK 即跳过该表。无 CLAIM、无 down；
schema_version 由 runner 登记。
"""
import re

# 表 → [(CHECK 片段, 依赖列)]；依赖列缺失时跳过该条
_CHECKS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "assets": [
        ("CONSTRAINT ck_assets_amount_non_negative CHECK (amount >= 0)", ("amount",)),
        ("CONSTRAINT ck_assets_side CHECK (side IN ('asset','liability'))", ("side",)),
    ],
    "asset_items": [
        ("CONSTRAINT ck_asset_items_status CHECK (status IN ('active','archived'))",
         ("status",)),
    ],
    "asset_item_names": [
        ("CONSTRAINT ck_asset_item_names_origin"
         " CHECK (origin IN ('migration','rename'))", ("origin",)),
    ],
    "principal_changes": [
        ("CONSTRAINT ck_principal_origin"
         " CHECK (origin IN ('external','transfer','lineage'))", ("origin",)),
    ],
    "audit_log": [
        ("CONSTRAINT ck_audit_actor CHECK (actor IN ('owner','system'))", ("actor",)),
    ],
    "attributes": [
        ("CONSTRAINT ck_attributes_type CHECK"
         " (type IN ('fund_type','liquidity_rating','account','owner','tag'))", ("type",)),
    ],
    "screenshot_evidence": [
        ("CONSTRAINT ck_evidence_engine"
         " CHECK (engine IS NULL OR engine IN ('ocr','ai'))", ("engine",)),
    ],
    "allocation_targets": [
        ("CONSTRAINT ck_targets_dimension CHECK"
         " (dimension IN ('asset_name','fund_type','liquidity_rating','account','owner','tag'))",
         ("dimension",)),
        ("CONSTRAINT ck_targets_percent CHECK (target_percent BETWEEN 0 AND 100)",
         ("target_percent",)),
        ("CONSTRAINT ck_targets_alert CHECK (alert_threshold BETWEEN 0 AND 100)",
         ("alert_threshold",)),
        ("CONSTRAINT ck_targets_alert_red CHECK"
         " (alert_threshold_red IS NULL OR alert_threshold_red BETWEEN 0 AND 100)",
         ("alert_threshold_red",)),
        ("CONSTRAINT ck_targets_relative CHECK"
         " (relative_threshold IS NULL OR relative_threshold BETWEEN 0 AND 100)",
         ("relative_threshold",)),
    ],
    "expenses": [
        ("CONSTRAINT ck_expenses_type"
         " CHECK (expense_type IN ('fixed','large_oneoff'))", ("expense_type",)),
        ("CONSTRAINT ck_expenses_amount CHECK (amount > 0)", ("amount",)),
    ],
    "fixed_expense_templates": [
        ("CONSTRAINT ck_fixed_cycle"
         " CHECK (cycle IN ('monthly','quarterly','yearly'))", ("cycle",)),
        ("CONSTRAINT ck_fixed_amount CHECK (amount > 0)", ("amount",)),
    ],
    "transfer_events": [
        ("CONSTRAINT ck_transfer_distinct CHECK (from_item_id <> to_item_id)",
         ("from_item_id", "to_item_id")),
    ],
    "lineage_links": [
        ("CONSTRAINT ck_lineage_distinct CHECK (from_item_id <> to_item_id)",
         ("from_item_id", "to_item_id")),
    ],
}

_CREATE_RE = re.compile(
    r"(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(\"?)([A-Za-z_][A-Za-z0-9_]*)(\"?)(.*)$"
)


def _table_sql(conn, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _applicable(checks, cols: set[str]) -> list[str]:
    return [sql for sql, needed in checks if set(needed) <= cols]


def _append_checks(sql: str, checks: list[str]) -> str:
    """在 CREATE TABLE 的最后一个 ')' 前追加表级约束（其余字节原样保留）。"""
    idx = sql.rstrip().rfind(")")
    if idx < 0:
        raise RuntimeError("无法定位 CREATE TABLE 结尾")
    head = sql[:idx].rstrip()
    if head.endswith("("):
        body = head + "\n  " + ",\n  ".join(checks)
    else:
        body = head + ",\n  " + ",\n  ".join(checks)
    return body + "\n)" + sql[idx + 1:]


def _as_new_table(sql: str, table: str) -> str:
    m = _CREATE_RE.match(sql)
    if not m:
        raise RuntimeError(f"无法解析 CREATE TABLE：{table}")
    return f'{m.group(1)}"{table}__new"{m.group(5)}'


def _capture(conn, table: str) -> tuple[list[str], list[str]]:
    idx = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL ORDER BY name", (table,))]
    trg = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?"
        " ORDER BY name", (table,))]
    return idx, trg


def _guard_negative_amounts(conn) -> None:
    """CHECK(amount >= 0) 前的可读守卫：列出遗留负额行，绝不静默改写账本。"""
    if "assets" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    if "amount" not in _columns(conn, "assets"):
        return
    bad = conn.execute(
        "SELECT id, asset_name, amount, asset_date FROM assets"
        " WHERE amount < 0 ORDER BY id LIMIT 20").fetchall()
    if not bad:
        return
    total = conn.execute("SELECT COUNT(*) FROM assets WHERE amount < 0").fetchone()[0]
    sample = "; ".join(f"id={r[0]} name={r[1]} amount={r[2]} date={r[3]}" for r in bad)
    raise RuntimeError(
        f"0024 中止：assets 有 {total} 条负额行（ADR-0002/0015 要求金额非负、"
        f"方向由 side 表达）。请先人工归正（取绝对值并校正 side）再升级。"
        f"样例：{sample}")


# 已被更新的值域取代的 CHECK：旧体文本 → 新版本「标记字符串」。
# 若表 DDL 里已经出现该标记，说明 models.py 或更晚的迁移已经装了新值域，
# 本条旧约束不再追加（否则会在同一张表上出现同名重复约束，且旧白名单会
# 把新维度值全部拒掉）。ADR-0019 分类重构后 allocation_targets 即属此类。
_SUPERSEDED: dict[str, str] = {
    "(dimension IN ('asset_name','fund_type','liquidity_rating','account','owner','tag'))":
        "'asset_class'",
}


def _is_superseded(check: str, table_sql: str) -> bool:
    marker = _SUPERSEDED.get(check.split("CHECK", 1)[1].strip())
    return bool(marker) and marker in table_sql


def _rebuild(conn, table: str, checks) -> str:
    sql = _table_sql(conn, table)
    if sql is None:
        return "missing"
    cols = _columns(conn, table)
    applicable = [c for c in _applicable(checks, cols)
                  if not _is_superseded(c, sql)]
    if not applicable:
        return "no-applicable-check"
    if all(c.split("CHECK", 1)[1].strip() in sql for c in applicable):
        return "skipped"

    collist = ", ".join(f'"{c}"' for c in cols)
    before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    idx_sqls, trg_sqls = _capture(conn, table)
    idx_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}
    trg_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}

    conn.execute(_as_new_table(_append_checks(sql, applicable), table))
    conn.execute(
        f'INSERT INTO "{table}__new" ({collist}) SELECT {collist} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{table}__new" RENAME TO "{table}"')
    for ddl in idx_sqls + trg_sqls:
        conn.execute(ddl)

    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if before != after:
        raise RuntimeError(f"{table} 行数不一致：{before} → {after}")
    now_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?"
        " AND sql IS NOT NULL", (table,))}
    now_trg = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
    if now_idx != idx_names or now_trg != trg_names:
        raise RuntimeError(
            f"{table} 索引/触发器未复原：{idx_names ^ now_idx} {trg_names ^ now_trg}")
    new_sql = _table_sql(conn, table) or ""
    missing = [c for c in applicable if c.split("CHECK", 1)[1].strip() not in new_sql]
    if missing:
        raise RuntimeError(f"{table} CHECK 未生效：{missing[0][:60]}")
    return "rebuilt"


def upgrade(conn):
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("0024 要求 PRAGMA foreign_keys=off 下执行表重建")

    _guard_negative_amounts(conn)

    results: dict[str, str] = {}
    for table, checks in _CHECKS.items():
        results[table] = _rebuild(conn, table, checks)

    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"重建后外键不一致：{problems[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"重建后完整性检查失败：{integrity}")
    return results