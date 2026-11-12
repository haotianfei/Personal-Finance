"""0034：`llm_providers` 四列测试状态（L2）。

规格：`.scratch/llm-provider-status/spec.md` D1（2026-11-06 冻结）。

背景：`llm_providers` 表只有 11 列（`id/label/kind/base_url/api_key/model/
is_default/enabled/sort_order/created_at/updated_at`），**没有任何测试相关列**。
`POST /api/llm-providers/{id}/test` 是薄壳——探测结果只活在前端一次 toast 里
（3 秒自动关闭），刷新、重启、换浏览器全丢，也回答不了「上次成功是什么时候」。
测试结果是供应商的客观属性，落库才有意义。

本迁移只做一件事：**加四列**，均为可空、无列级默认。

- `last_tested_at` DATETIME —— 探测完成时刻；
- `last_test_ok` BOOLEAN —— True 成功 / False 失败 / **NULL 从未测试**；
- `last_test_message` VARCHAR(300) —— 结果摘要（成功回包或错误文案）；
- `last_test_elapsed_ms` INTEGER —— 本次探测耗时（毫秒）。

**四列必须可空且无默认**：`NULL` 与 `False` 是两个语义（「没测过」vs「测了没通」），
若给 `last_test_ok` 加 `DEFAULT 0`，存量行会被无声地断言成「测试失败」。同理
`last_test_elapsed_ms` 的 `NULL` 不是 `0`（0 毫秒是一次真实探测的下界）。

加列是 SQLite 原生能力，**不做表重建**（重建要重跑唯一约束与两个索引，风险与收益
不成比例）；本迁移**不动任何数据**，也不写任何结果行——落库由端点负责。

守卫与安全：

- `llm_providers` 不存在 → `skipped`（桩库/不完整库形状）。
- 表存在但缺基准列（11 列里任一）→ `skipped`：这种表既不是本迁移的目标形状，也不是
  本迁移的活，多为测试桩表或旁路程序自建的同名表（沿用 0029/0030/0032/0033
  「形状不在预期则幂等跳过」的先例；fail-fast 只留给数据/结构与断言不一致）。
- **逐列判定**：四列均已存在（例如 `create_all` 先行建过表）→ 不加列，返回 `noop`；
  只缺一部分 → **只补缺的那些**，整体不跳（手工补过一列或上次中断的库都能前进）。
- 迁移后校验：列集合逐字（15 列）、四列均为对应类型且 `notnull=0`、无默认值 →
  `integrity_check=ok`。任一不符即抛错中止。
- 事务由 runner（`services/db_migration_service._run_script`）负责，本脚本不 commit；
  抛错 → 整个脚本回滚。
"""
import logging

log = logging.getLogger(__name__)

TABLE = "llm_providers"

# 0034 之前的 11 列（列序＝ORM 定义序＝真实 DDL 序）。缺任一列 → 不是本迁移的目标形状 → skipped
BASE_COLUMNS = [
    "id",
    "label",
    "kind",
    "base_url",
    "api_key",
    "model",
    "is_default",
    "enabled",
    "sort_order",
    "created_at",
    "updated_at",
]
# 本迁移新增的四列（顺序即 ALTER 顺序，也是 create_all 之后的物理顺序）
NEW_COLUMNS = [
    ("last_tested_at", "DATETIME"),
    ("last_test_ok", "BOOLEAN"),
    ("last_test_message", "VARCHAR(300)"),
    ("last_test_elapsed_ms", "INTEGER"),
]
EXPECTED_COLUMNS = BASE_COLUMNS + [name for name, _ in NEW_COLUMNS]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _column_def(conn, table: str, column: str):
    """(type, notnull, dflt_value) —— 用来确认「加的是可空、无默认的列」。"""
    for r in conn.execute(f'PRAGMA table_info("{table}")'):
        if r[1] == column:
            return (r[2] or "").upper(), int(r[3]), r[4]
    return None


def _verify(conn) -> None:
    cols = _columns(conn, TABLE)
    if sorted(cols) != sorted(EXPECTED_COLUMNS):
        raise RuntimeError(
            f"0034 中止：加列后列集合不符（期望 15 列 {EXPECTED_COLUMNS} / 实际 {cols}）"
        )
    for name, ddl in NEW_COLUMNS:
        got = _column_def(conn, TABLE, name)
        if got != (ddl, 0, None):
            raise RuntimeError(
                f"0034 中止：{TABLE}.{name} 的类型/可空性/默认值不符"
                f"（期望 {ddl}、可空、无默认 / 实际 {got}）"
            )

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"0034 中止：integrity_check={integrity}")


def upgrade(conn) -> str:
    # ① 表存在守卫
    if not _table_exists(conn, TABLE):
        log.warning("0034 跳过：缺少 %s 表", TABLE)
        return "skipped"

    # ② 形状守卫：缺基准列 → 不是本迁移的目标形状（桩表/旁路同名表）
    cols = _columns(conn, TABLE)
    missing = [c for c in BASE_COLUMNS if c not in cols]
    if missing:
        log.warning(
            "0034 跳过：%s 缺基准列 %s，结构不可识别（多为测试桩表或旁路自建同名表）",
            TABLE, missing)
        return "skipped"

    # ③ 逐列判定加列：全在 → 不加；只缺一部分 → 只补缺的（整体不跳）
    added: list[str] = []
    for name, ddl in NEW_COLUMNS:
        if name in cols:
            continue
        conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{name}" {ddl}')
        added.append(name)

    # ④ 迁移后校验
    _verify(conn)

    log.info("add_llm_provider_test_status: added=%s", added or "-")
    if not added:
        return "noop"
    return f"added={','.join(added)}"