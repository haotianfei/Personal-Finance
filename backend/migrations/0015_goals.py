"""0015：C1 目标（`goals` 根 + `goal_versions` 追加史）。

规格：`.scratch/phase3-goal/spec.md` v1.0 §2.1/§2.2；决策 `docs/adr/0010` §3。

- `goals`：目标聚合根（名称/范围/是否主目标）。scope 在根不在版本内——改范围＝
  换一个目标；`member` 列本期只留、API 422（四期启用）。`is_primary` 全表至多
  一条 1（部分唯一索引）。
- `goal_versions`：每次人裁决设定＝一行（金额/期限/可选年化/风险档/生命周期/
  理由/状态/生效日/判据快照）。`UNIQUE(goal_id, version_no)` 保版本号单调；
  每 Goal 至多一条 `active`（部分唯一 active 索引兜底并发）。
- **不加 append-only 触发器**：draft 需改/删；active/archived 不可变由
  service + 测试保证（ADR §8 已接受）。
- 全守卫幂等（IF NOT EXISTS）；无 CLAIM 标记＝首跑也实跑；无数据回填；
  回滚＝revert（表残留无害）。
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS goals ("
        "  id INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('family','member')),"
        "  scope_owner_id INTEGER REFERENCES attributes (id),"
        "  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),"
        "  created_at TEXT NOT NULL,"
        "  CHECK ((scope_kind='family' AND scope_owner_id IS NULL)"
        "      OR (scope_kind='member' AND scope_owner_id IS NOT NULL))"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goals_primary"
        " ON goals (is_primary) WHERE is_primary = 1"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS goal_versions ("
        "  id INTEGER PRIMARY KEY,"
        "  goal_id INTEGER NOT NULL REFERENCES goals (id),"
        "  version_no INTEGER NOT NULL CHECK (version_no > 0),"
        "  status TEXT NOT NULL CHECK (status IN ('draft','active','archived')),"
        "  target_amount NUMERIC(15,2) NOT NULL CHECK (target_amount > 0),"
        "  target_date DATE NOT NULL,"
        "  target_rate_annual NUMERIC(8,4)"
        "    CHECK (target_rate_annual IS NULL OR target_rate_annual > -1),"
        "  risk_profile TEXT CHECK (risk_profile IS NULL OR risk_profile IN"
        "    ('conservative','balanced','growth')),"
        "  life_stage TEXT CHECK (life_stage IS NULL OR life_stage IN"
        "    ('accumulation','transition','decumulation')),"
        "  reason TEXT NOT NULL,"
        "  effective_from DATE,"
        "  criterion_json TEXT,"
        "  created_at TEXT NOT NULL,"
        "  activated_at TEXT,"
        "  archived_at TEXT,"
        "  UNIQUE (goal_id, version_no),"
        "  CHECK (status <> 'active' OR effective_from IS NOT NULL),"
        "  CHECK (status <> 'active' OR criterion_json IS NOT NULL),"
        "  CHECK (effective_from IS NULL OR target_date > effective_from)"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_versions_active"
        " ON goal_versions (goal_id) WHERE status = 'active'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_goal_versions_goal"
        " ON goal_versions (goal_id, version_no)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_goal_versions_status"
        " ON goal_versions (status)"
    )