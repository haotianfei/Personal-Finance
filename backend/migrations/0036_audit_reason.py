"""0036：`audit_log` 加 `reason`/`reason_note` 两列（ADR-0027 审计留痕契约，L2）。

规格：ADR-0027 D2/D3（变更原因分类）＋ `.scratch/audit-contract` 的 lane 说明。

改什么：**只加两列**，其余一律不动。

- `reason` VARCHAR(20) —— correction|evolution|normalization|import|migration|other；
- `reason_note` VARCHAR(200) —— 一句话说明（长度由应用层校验）。

**两列必须可空且无默认**：`NULL` 的语义是「未记录原因」。存量审计行是契约生效前的
历史，不能倒填一个假的 reason（倒填＝伪造人当时的表态，比空缺更有害）；新写入由
`audit_service.make_change()` 在**调用方提供时**校验取值（ADR-0027 D7：纠正类应先有
reason，但运行时零强制——存量调用点与前端请求体尚无该字段，强制即 500；缺口由
`tests/test_audit_contract.py` 的 `KNOWN_DEBT` 显式登记并逐票清零）。

**不加 CHECK 约束**：SQLite 的 `ALTER TABLE ADD COLUMN` 无法带表级 CHECK，而 SQLite
也不能在已存在表上改约束（需整表重建）。审计表是 append-only（BEFORE UPDATE/DELETE
ABORT），整表重建＝摘掉触发器重灌全表——风险远大于收益，且 hash-chain 的
`before_json/after_json` 必须「原样取库内文本」，重建过程一旦被序列化改动就是链断。
故枚举由应用层 `services/audit_contract.REASON` 单点校验（与 `entity`/`action`
同口径：契约在代码里，不在 DDL 里）。

**哈希口径不变**：`audit_chain.FIELDS` 不含这两个列，`row_hash` 一字不改；
加列不动既有行的哈希，`audit_chain.verify()` 照旧 ok。

**绝不动 append-only 触发器**：本脚本只做 `ALTER TABLE ... ADD COLUMN`，
迁移后显式断言 `audit_log_no_update` / `audit_log_no_delete` 仍存在——
少了它们审计表就可被改写，那比没有 reason 列严重得多。

幂等：
- 表不存在 → `skipped`（新库/桩库形状，`create_all` 会按新模型建表）；
- 两列已齐 → `noop`（不重复 ALTER，SQLite 重复加列会报 duplicate column）；
- 只缺一列（手工补过/上次中断）→ 只补缺的那一列，整体不跳。

不动任何行数据、不写任何审计行；事务由 runner
（`services/db_migration_service._run_script`）负责，成功即登记 `schema_version=36`。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "audit_log"

# 本迁移新增的两列（顺序即 ALTER 顺序，也是 create_all 后的物理顺序）
NEW_COLUMNS = [
    ("reason", "VARCHAR(20)"),
    ("reason_note", "VARCHAR(200)"),
]

# 必须存在的 append-only 触发器（本迁移不得摘除）
REQUIRED_TRIGGERS = ("audit_log_no_update", "audit_log_no_delete")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _column_def(conn, table: str, column: str):
    """(type, notnull, dflt_value) —— 确认「加的是可空、无默认的列」。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == column:
            return (r[2] or "").upper(), int(r[3]), r[4]
    return None


def _trigger_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone() is not None


def _verify(conn) -> None:
    for name, ddl in NEW_COLUMNS:
        got = _column_def(conn, TABLE, name)
        if got != (ddl, 0, None):
            raise RuntimeError(
                f"0036 中止：{TABLE}.{name} 的类型/可空性/默认值不符"
                f"（期望 {ddl}、可空、无默认 / 实际 {got}）")

    for trigger in REQUIRED_TRIGGERS:
        if not _trigger_exists(conn, trigger):
            raise RuntimeError(f"0036 中止：append-only 触发器 {trigger} 缺失")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0036 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        log.warning("0036 跳过：缺少 %s 表（新库由 create_all 建新形状）", TABLE)
        return "skipped"

    # ② 逐列判定加列：全在 → 不加；只缺一部分 → 只补缺的
    cols = _columns(conn, TABLE)
    added: list[str] = []
    for name, ddl in NEW_COLUMNS:
        if name in cols:
            continue
        conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" {ddl}')
        added.append(name)

    # ③ 迁移后校验（类型/可空/无默认、触发器仍在、integrity）
    _verify(conn)

    log.info("0036 audit_log_reason: added=%s", added or "-")
    if not added:
        return "noop"
    return f"added={','.join(added)}"