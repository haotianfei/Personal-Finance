"""0017：allocation_targets 增「红档／相对」两列（三期-2 C2 双阈值，ADR-0012 D7）。

- `alert_threshold_red`：红档·绝对 pp；NULL＝代码兜底 2×黄（不伪造人设阈值）。
- `relative_threshold`：黄档·相对 %；NULL＝代码兜底 25（红＝2×）。
- 幂等：`PRAGMA table_info` 逐列守卫，已存在跳过；空表/无表 no-op（同 0004 模式）。
- **无回填、无新表/索引/触发器**：NULL 即走代码兜底（`band_service.resolve_band`）。
- 无 CLAIM＝首跑实跑；无 down，回滚后旧逻辑忽略新列（NULL 不影响）。
"""


def upgrade(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(allocation_targets)")}
    if not cols:
        return
    if "alert_threshold_red" not in cols:
        conn.execute(
            "ALTER TABLE allocation_targets ADD COLUMN alert_threshold_red "
            "DECIMAL(5, 2)"
        )
    if "relative_threshold" not in cols:
        conn.execute(
            "ALTER TABLE allocation_targets ADD COLUMN relative_threshold "
            "DECIMAL(5, 2)"
        )